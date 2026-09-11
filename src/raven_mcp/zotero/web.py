"""Read-only client for the authenticated Zotero Web API."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Literal, Self, cast
from urllib.parse import quote

import httpx

from raven_mcp.config import Settings, settings as default_settings
from raven_mcp.errors import ErrorCode, RavenError
from raven_mcp.schemas import ZoteroGetRequest, ZoteroItem, ZoteroSearchRequest
from raven_mcp.zotero.local import (
    LibraryType,
    ZoteroStatus,
    normalize_payload,
    resolve_get,
    resolve_search,
)

_USER_ID_RE = re.compile(r"^[0-9]+$")


class WebZoteroClient:
    """Read from the Zotero Web API using header authentication."""

    def __init__(
        self,
        settings: Settings = default_settings,
        *,
        client: httpx.AsyncClient | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float | httpx.Timeout = 10.0,
    ) -> None:
        if client is not None and transport is not None:
            raise ValueError("client and transport are mutually exclusive")
        self._base_url = settings.zotero_web_url.rstrip("/")
        self._api_key = settings.zotero_api_key
        self._user_id = settings.zotero_user_id
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(transport=transport, timeout=timeout)

    @property
    def enabled(self) -> bool:
        """Return whether both required Web API credentials exist."""
        return bool(self._api_key and self._user_id)

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
        """Close the internally created HTTP client."""
        if self._owns_client:
            await self._client.aclose()

    def _credentials(self) -> tuple[str, str]:
        if not self._api_key or not self._user_id:
            raise RavenError(
                ErrorCode.ZOTERO_UNAVAILABLE,
                "The Zotero Web API is not configured.",
                stage="zotero",
                remediation="Set both ZOTERO_API_KEY and ZOTERO_USER_ID.",
                locator={"source": "web"},
            )
        if _USER_ID_RE.fullmatch(self._user_id) is None:
            raise RavenError(
                ErrorCode.INVALID_REQUEST,
                "The configured Zotero user ID must contain only digits.",
                stage="zotero",
                remediation="Set ZOTERO_USER_ID to the numeric Zotero user ID.",
                locator={"source": "web"},
            )
        return self._api_key, self._user_id

    def _library_path(
        self,
        library_type: LibraryType,
        library_id: str | None,
    ) -> tuple[str, str]:
        _, user_id = self._credentials()
        if library_type == "user":
            return f"users/{quote(user_id, safe='')}", user_id
        assert library_id is not None
        return f"groups/{quote(library_id, safe='')}", library_id

    async def _get(
        self,
        path: str,
        *,
        params: Mapping[str, str | int] | None = None,
    ) -> httpx.Response:
        api_key, _ = self._credentials()
        response = await self._client.get(
            f"{self._base_url}/{path.lstrip('/')}",
            headers={
                "Accept": "application/json",
                "Zotero-API-Key": api_key,
                "Zotero-API-Version": "3",
            },
            params=params,
        )
        response.raise_for_status()
        return response

    @staticmethod
    def _json(response: httpx.Response) -> object:
        try:
            return cast(object, response.json())
        except ValueError as exc:
            raise RavenError(
                ErrorCode.ZOTERO_INCOMPATIBLE,
                "Zotero returned invalid JSON.",
                stage="zotero",
                remediation="Verify the Zotero Web API configuration.",
                locator={"source": "web"},
            ) from exc

    async def status(self) -> ZoteroStatus:
        """Check Web API access without exposing credentials."""
        _, user_id = self._credentials()
        response = await self._get(
            f"users/{quote(user_id, safe='')}/items",
            params={"format": "json", "limit": 1},
        )
        return {
            "available": True,
            "source": "web",
            "api_version": response.headers.get("Zotero-API-Version"),
            "schema_version": response.headers.get("Zotero-Schema-Version"),
            "server_id": response.headers.get("Zotero-Server-ID"),
        }

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
        """Search an accessible user or group library."""
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
        library_path, canonical_library_id = self._library_path(
            options.library_type, options.library_id
        )
        path = f"{library_path}/items"
        if options.collection_key is not None:
            path = f"{library_path}/collections/{quote(options.collection_key, safe='')}/items"

        params: dict[str, str | int] = {
            "q": options.query,
            "qmode": options.qmode,
            "sort": options.sort,
            "direction": options.direction,
            "start": options.start,
            "limit": options.limit,
            "format": "json",
            "include": "data,csljson",
        }
        if options.item_type is not None:
            params["itemType"] = options.item_type
        if options.tag is not None:
            params["tag"] = options.tag

        response = await self._get(path, params=params)
        return normalize_payload(
            self._json(response),
            source="web",
            library_type=options.library_type,
            library_id=canonical_library_id,
        )

    async def get_items(
        self,
        request: ZoteroGetRequest | Sequence[str] | None = None,
        *,
        item_keys: Sequence[str] | None = None,
        library_type: LibraryType = "user",
        library_id: str | None = None,
    ) -> list[ZoteroItem]:
        """Get Web API items by key without following attachment links."""
        options = resolve_get(
            request,
            item_keys=item_keys,
            library_type=library_type,
            library_id=library_id,
        )
        library_path, canonical_library_id = self._library_path(
            options.library_type, options.library_id
        )
        response = await self._get(
            f"{library_path}/items",
            params={
                "itemKey": ",".join(options.item_keys),
                "format": "json",
                "include": "data,csljson",
            },
        )
        items = normalize_payload(
            self._json(response),
            source="web",
            library_type=options.library_type,
            library_id=canonical_library_id,
        )
        by_key = {item.key: item for item in items}
        return [by_key[key] for key in options.item_keys if key in by_key]


WebZoteroAdapter = WebZoteroClient
ZoteroWebClient = WebZoteroClient

