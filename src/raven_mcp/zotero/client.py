"""Unified, local-first Zotero client."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from typing import Literal, Self, TypeVar

import httpx

from raven_mcp.config import Settings
from raven_mcp.config import settings as default_settings
from raven_mcp.errors import ErrorCode, RavenError
from raven_mcp.schemas import ZoteroGetRequest, ZoteroItem, ZoteroSearchRequest
from raven_mcp.zotero.local import (
    LibraryType,
    LocalZoteroClient,
    ZoteroSource,
    ZoteroStatus,
    resolve_get,
    resolve_search,
)
from raven_mcp.zotero.web import WebZoteroClient

_ResultT = TypeVar("_ResultT")
_FALLBACK_STATUS_CODES = frozenset({403, 404, 405, 501})


def _can_fallback(exc: Exception) -> bool:
    if isinstance(exc, httpx.TransportError):
        return True
    return (
        isinstance(exc, httpx.HTTPStatusError)
        and exc.response.status_code in _FALLBACK_STATUS_CODES
    )


def _map_error(exc: Exception, *, source: ZoteroSource, operation: str) -> RavenError:
    if isinstance(exc, RavenError):
        return exc

    locator: dict[str, str | int] = {"source": source, "operation": operation}
    if isinstance(exc, httpx.TransportError):
        return RavenError(
            ErrorCode.ZOTERO_UNAVAILABLE,
            f"The {source} Zotero API could not be reached.",
            stage="zotero",
            retryable=True,
            remediation=(
                "Start Zotero and enable its local API, or configure Web API credentials."
                if source == "local"
                else "Check network access and the Zotero Web API service."
            ),
            locator=locator,
        )

    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        locator["status"] = status
        if status in {401, 403}:
            return RavenError(
                ErrorCode.ZOTERO_UNAVAILABLE,
                f"The {source} Zotero API rejected access.",
                stage="zotero",
                remediation=(
                    "Allow local API access in Zotero, or configure Web API credentials."
                    if source == "local"
                    else "Verify ZOTERO_API_KEY permissions and ZOTERO_USER_ID."
                ),
                locator=locator,
            )
        if status in {405, 501} or (source == "local" and status == 404):
            return RavenError(
                ErrorCode.ZOTERO_INCOMPATIBLE,
                f"The {source} Zotero API does not support this read endpoint.",
                stage="zotero",
                remediation="Upgrade Zotero and ensure API version 3 is available.",
                locator=locator,
            )
        if status == 429 or status >= 500:
            return RavenError(
                ErrorCode.ZOTERO_UNAVAILABLE,
                f"The {source} Zotero API is temporarily unavailable.",
                stage="zotero",
                retryable=True,
                remediation="Try the Zotero request again later.",
                locator=locator,
            )
        return RavenError(
            ErrorCode.INVALID_REQUEST,
            f"The {source} Zotero API rejected the request.",
            stage="zotero",
            remediation="Check the library ID, collection, item keys, and query parameters.",
            locator=locator,
        )

    if isinstance(exc, httpx.HTTPError):
        return RavenError(
            ErrorCode.ZOTERO_UNAVAILABLE,
            f"The {source} Zotero API request failed.",
            stage="zotero",
            retryable=True,
            remediation="Check Zotero availability and network access.",
            locator=locator,
        )

    return RavenError(
        ErrorCode.INTERNAL_ERROR,
        "An unexpected Zotero adapter error occurred.",
        stage="zotero",
        remediation="Review the Zotero adapter configuration.",
        locator=locator,
    )


class ZoteroClient:
    """Use Zotero locally first, with a narrowly scoped Web API fallback."""

    def __init__(
        self,
        settings: Settings = default_settings,
        *,
        local: LocalZoteroClient | None = None,
        web: WebZoteroClient | None = None,
        local_http_client: httpx.AsyncClient | None = None,
        web_http_client: httpx.AsyncClient | None = None,
        local_transport: httpx.AsyncBaseTransport | None = None,
        web_transport: httpx.AsyncBaseTransport | None = None,
        local_timeout: float | httpx.Timeout = 5.0,
        web_timeout: float | httpx.Timeout = 10.0,
    ) -> None:
        if local is not None and (local_http_client is not None or local_transport is not None):
            raise ValueError("local cannot be combined with local HTTP injection")
        if web is not None and (web_http_client is not None or web_transport is not None):
            raise ValueError("web cannot be combined with web HTTP injection")

        self._owns_local = local is None
        self._owns_web = web is None
        self._local = local or LocalZoteroClient(
            settings,
            client=local_http_client,
            transport=local_transport,
            timeout=local_timeout,
        )
        self._web = web or WebZoteroClient(
            settings,
            client=web_http_client,
            transport=web_transport,
            timeout=web_timeout,
        )

    @property
    def local(self) -> LocalZoteroClient:
        """Return the configured local adapter."""
        return self._local

    @property
    def web(self) -> WebZoteroClient:
        """Return the configured Web API adapter."""
        return self._web

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close adapters created by this client."""
        try:
            if self._owns_local:
                await self._local.aclose()
        finally:
            if self._owns_web:
                await self._web.aclose()

    async def _run(
        self,
        operation: str,
        local_call: Callable[[], Awaitable[_ResultT]],
        web_call: Callable[[], Awaitable[_ResultT]],
    ) -> _ResultT:
        try:
            return await local_call()
        except Exception as local_exc:
            if not _can_fallback(local_exc) or not self._web.enabled:
                mapped = _map_error(local_exc, source="local", operation=operation)
                if mapped is local_exc:
                    raise
                raise mapped from local_exc

        try:
            return await web_call()
        except Exception as web_exc:
            mapped = _map_error(web_exc, source="web", operation=operation)
            if mapped is web_exc:
                raise
            raise mapped from web_exc

    async def status(self) -> ZoteroStatus:
        """Report the first available Zotero source."""
        return await self._run("status", self._local.status, self._web.status)

    async def search(
        self,
        request: ZoteroSearchRequest | str | None = None,
        *,
        query: str = "",
        library_type: LibraryType = "user",
        library_id: str | None = None,
        collection: str | None = None,
        collection_key: str | None = None,
        item_type: str | None = None,
        tag: str | None = None,
        qmode: str | None = None,
        search_mode: str | None = None,
        sort: str = "dateModified",
        direction: Literal["asc", "desc"] = "desc",
        start: int = 0,
        limit: int = 20,
    ) -> list[ZoteroItem]:
        """Search Zotero through the first usable source."""
        options = resolve_search(
            request,
            query=query,
            library_type=library_type,
            library_id=library_id,
            collection=collection,
            collection_key=collection_key,
            item_type=item_type,
            tag=tag,
            qmode=qmode,
            search_mode=search_mode,
            sort=sort,
            direction=direction,
            start=start,
            limit=limit,
        )

        async def search_local() -> list[ZoteroItem]:
            return await self._local.search(
                query=options.query,
                library_type=options.library_type,
                library_id=options.library_id,
                collection_key=options.collection_key,
                item_type=options.item_type,
                tag=options.tag,
                qmode=options.qmode,
                sort=options.sort,
                direction=options.direction,
                start=options.start,
                limit=options.limit,
            )

        async def search_web() -> list[ZoteroItem]:
            return await self._web.search(
                query=options.query,
                library_type=options.library_type,
                library_id=options.library_id,
                collection_key=options.collection_key,
                item_type=options.item_type,
                tag=options.tag,
                qmode=options.qmode,
                sort=options.sort,
                direction=options.direction,
                start=options.start,
                limit=options.limit,
            )

        return await self._run("search", search_local, search_web)

    async def get_items(
        self,
        request: ZoteroGetRequest | Sequence[str] | None = None,
        *,
        item_keys: Sequence[str] | None = None,
        library_type: LibraryType = "user",
        library_id: str | None = None,
    ) -> list[ZoteroItem]:
        """Get deduplicated Zotero item keys through the first usable source."""
        options = resolve_get(
            request,
            item_keys=item_keys,
            library_type=library_type,
            library_id=library_id,
        )

        async def get_local() -> list[ZoteroItem]:
            return await self._local.get_items(
                item_keys=options.item_keys,
                library_type=options.library_type,
                library_id=options.library_id,
            )

        async def get_web() -> list[ZoteroItem]:
            return await self._web.get_items(
                item_keys=options.item_keys,
                library_type=options.library_type,
                library_id=options.library_id,
            )

        return await self._run("get_items", get_local, get_web)
