"""Read-only client for Zotero's local HTTP API."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Self, TypedDict, cast
from urllib.parse import quote

import httpx

from raven_mcp.config import Settings
from raven_mcp.config import settings as default_settings
from raven_mcp.errors import ErrorCode, RavenError
from raven_mcp.schemas import ZoteroGetRequest, ZoteroItem, ZoteroSearchRequest

LibraryType = Literal["user", "group"]
ZoteroSource = Literal["local", "web"]

_ITEM_KEY_RE = re.compile(r"^[23456789ABCDEFGHIJKLMNPQRSTUVWXYZ]{8}$")
_GROUP_ID_RE = re.compile(r"^[0-9]+$")
_API_HEADERS = {
    "Accept": "application/json",
    "Zotero-API-Version": "3",
}


class ZoteroStatus(TypedDict):
    """Availability and API metadata for a Zotero source."""

    available: bool
    source: ZoteroSource
    api_version: str | None
    schema_version: str | None
    server_id: str | None


@dataclass(frozen=True, slots=True)
class SearchOptions:
    query: str
    library_type: LibraryType
    library_id: str | None
    collection_key: str | None
    item_type: str | None
    tag: str | None
    qmode: str
    sort: str
    direction: Literal["asc", "desc"]
    start: int
    limit: int


@dataclass(frozen=True, slots=True)
class GetOptions:
    item_keys: tuple[str, ...]
    library_type: LibraryType
    library_id: str | None


def _invalid_request(message: str) -> RavenError:
    return RavenError(
        ErrorCode.INVALID_REQUEST,
        message,
        stage="zotero",
        remediation="Correct the Zotero library, key, or query parameters.",
    )


def _validate_library(
    library_type: str,
    library_id: str | None,
) -> tuple[LibraryType, str | None]:
    if library_type not in {"user", "group"}:
        raise _invalid_request("library_type must be 'user' or 'group'.")
    typed_library = cast(LibraryType, library_type)
    if typed_library == "group":
        if not library_id:
            raise _invalid_request("library_id is required for a group library.")
        if _GROUP_ID_RE.fullmatch(library_id) is None:
            raise _invalid_request("A Zotero group library_id must contain only digits.")
    return typed_library, library_id


def _validate_item_keys(item_keys: Sequence[str]) -> tuple[str, ...]:
    if not item_keys:
        raise _invalid_request("At least one Zotero item key is required.")

    unique: list[str] = []
    seen: set[str] = set()
    for key in item_keys:
        if not isinstance(key, str) or _ITEM_KEY_RE.fullmatch(key) is None:
            raise _invalid_request(
                f"Invalid Zotero item key {key!r}; keys must be 8 uppercase letters or digits."
            )
        if key not in seen:
            seen.add(key)
            unique.append(key)
    return tuple(unique)


def resolve_search(
    request: ZoteroSearchRequest | str | None,
    *,
    query: str,
    library_type: LibraryType,
    library_id: str | None,
    collection: str | None,
    collection_key: str | None,
    item_type: str | None,
    tag: str | None,
    qmode: str | None,
    search_mode: str | None,
    sort: str,
    direction: Literal["asc", "desc"],
    start: int,
    limit: int,
) -> SearchOptions:
    if isinstance(request, ZoteroSearchRequest):
        query = request.query
        library_type = request.library_type
        library_id = request.library_id
        collection_key = request.collection_key
        item_type = request.item_type
        tag = request.tag
        qmode = request.search_mode
        sort = request.sort
        direction = request.direction
        start = request.start
        limit = request.limit
    elif isinstance(request, str):
        query = request

    if collection and collection_key and collection != collection_key:
        raise _invalid_request("collection and collection_key identify different collections.")
    collection_key = collection_key or collection
    if collection_key is not None and _ITEM_KEY_RE.fullmatch(collection_key) is None:
        raise _invalid_request("A Zotero collection key must be 8 uppercase letters or digits.")

    typed_library, library_id = _validate_library(library_type, library_id)
    resolved_qmode = qmode or search_mode or "titleCreatorYear"
    if resolved_qmode not in {"titleCreatorYear", "everything"}:
        raise _invalid_request("qmode must be 'titleCreatorYear' or 'everything'.")
    if direction not in {"asc", "desc"}:
        raise _invalid_request("direction must be 'asc' or 'desc'.")
    if start < 0:
        raise _invalid_request("start must be zero or greater.")
    if not 1 <= limit <= 100:
        raise _invalid_request("limit must be between 1 and 100.")

    return SearchOptions(
        query=query,
        library_type=typed_library,
        library_id=library_id,
        collection_key=collection_key,
        item_type=item_type,
        tag=tag,
        qmode=resolved_qmode,
        sort=sort,
        direction=direction,
        start=start,
        limit=limit,
    )


def resolve_get(
    request: ZoteroGetRequest | Sequence[str] | None,
    *,
    item_keys: Sequence[str] | None,
    library_type: LibraryType,
    library_id: str | None,
) -> GetOptions:
    if isinstance(request, ZoteroGetRequest):
        item_keys = request.item_keys
        library_type = request.library_type
        library_id = request.library_id
    elif request is not None:
        if isinstance(request, str):
            raise _invalid_request("item_keys must be a sequence of Zotero item keys.")
        item_keys = request

    if item_keys is None:
        raise _invalid_request("At least one Zotero item key is required.")
    typed_library, library_id = _validate_library(library_type, library_id)
    return GetOptions(
        item_keys=_validate_item_keys(item_keys),
        library_type=typed_library,
        library_id=library_id,
    )


def _records_from_payload(payload: object) -> list[tuple[str | None, Mapping[str, Any]]]:
    if isinstance(payload, list):
        if not all(isinstance(value, Mapping) for value in payload):
            raise TypeError("Zotero returned a list containing a non-object item.")
        return [(None, cast(Mapping[str, Any], value)) for value in payload]

    if not isinstance(payload, Mapping):
        raise TypeError("Zotero returned a payload that is not an object or list.")
    obj = cast(Mapping[str, Any], payload)

    for field in ("items", "results"):
        nested = obj.get(field)
        if isinstance(nested, list):
            if not all(isinstance(value, Mapping) for value in nested):
                raise TypeError("Zotero returned a non-object item.")
            return [(None, cast(Mapping[str, Any], value)) for value in nested]

    if any(field in obj for field in ("key", "itemKey", "data", "csljson", "cslJSON")):
        return [(None, obj)]

    records: list[tuple[str | None, Mapping[str, Any]]] = []
    for key, value in obj.items():
        if isinstance(value, Mapping):
            records.append((str(key), cast(Mapping[str, Any], value)))
    if not records and obj:
        raise TypeError("Zotero returned an object without item records.")
    return records


def _mapping(value: object) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(cast(Mapping[str, Any], value))
    return {}


def _item_key(raw: Mapping[str, Any], data: Mapping[str, Any], hinted_key: str | None) -> str:
    value = raw.get("key") or raw.get("itemKey") or data.get("key") or hinted_key
    if not isinstance(value, str) or _ITEM_KEY_RE.fullmatch(value) is None:
        raise TypeError("Zotero returned an item without a valid key.")
    return value


def _item_version(raw: Mapping[str, Any], data: Mapping[str, Any]) -> int | None:
    value = raw.get("version", data.get("version"))
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _link_href(value: object) -> str | None:
    if isinstance(value, str) and value:
        return value
    if isinstance(value, Mapping):
        href = value.get("href")
        if isinstance(href, str) and href:
            return href
    return None


def _item_uri(
    raw: Mapping[str, Any],
    data: Mapping[str, Any],
    library: Mapping[str, Any],
    *,
    key: str,
    library_type: LibraryType,
    library_id: str,
) -> str:
    for value in (raw.get("uri"), data.get("uri")):
        if isinstance(value, str) and value:
            return value

    links = raw.get("links")
    if isinstance(links, Mapping):
        for relation in ("self", "alternate"):
            href = _link_href(links.get(relation))
            if href is not None:
                return href

    raw_library_type = library.get("type")
    if raw_library_type in {"user", "group"}:
        library_type = cast(LibraryType, raw_library_type)
    raw_library_id = library.get("id")
    if isinstance(raw_library_id, (str, int)) and not isinstance(raw_library_id, bool):
        library_id = str(raw_library_id)
    segment = "users" if library_type == "user" else "groups"
    return f"http://zotero.org/{segment}/{quote(library_id, safe='')}/items/{quote(key, safe='')}"


def _normalize_item(
    raw: Mapping[str, Any],
    *,
    hinted_key: str | None,
    source: ZoteroSource,
    library_type: LibraryType,
    library_id: str,
) -> ZoteroItem:
    data_value = raw.get("data")
    if isinstance(data_value, Mapping):
        data = dict(cast(Mapping[str, Any], data_value))
    else:
        envelope_fields = {"library", "links", "meta", "csljson", "cslJSON", "csl"}
        data = {str(key): value for key, value in raw.items() if key not in envelope_fields}

    key = _item_key(raw, data, hinted_key)
    library = _mapping(raw.get("library"))
    if not library:
        library = {"type": library_type, "id": library_id}

    csl_json: dict[str, Any] = {}
    for field in ("csljson", "cslJSON", "csl"):
        candidate = raw.get(field)
        if isinstance(candidate, Mapping):
            csl_json = dict(cast(Mapping[str, Any], candidate))
            break
        if isinstance(candidate, str):
            try:
                decoded = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            if isinstance(decoded, Mapping):
                csl_json = dict(cast(Mapping[str, Any], decoded))
                break
            if isinstance(decoded, list) and len(decoded) == 1 and isinstance(decoded[0], Mapping):
                csl_json = dict(cast(Mapping[str, Any], decoded[0]))
                break

    return ZoteroItem(
        key=key,
        version=_item_version(raw, data),
        library=library,
        data=data,
        csl_json=csl_json,
        uri=_item_uri(
            raw,
            data,
            library,
            key=key,
            library_type=library_type,
            library_id=library_id,
        ),
        source=source,
    )


def normalize_payload(
    payload: object,
    *,
    source: ZoteroSource,
    library_type: LibraryType,
    library_id: str,
) -> list[ZoteroItem]:
    try:
        return [
            _normalize_item(
                raw,
                hinted_key=hinted_key,
                source=source,
                library_type=library_type,
                library_id=library_id,
            )
            for hinted_key, raw in _records_from_payload(payload)
        ]
    except (TypeError, ValueError) as exc:
        raise RavenError(
            ErrorCode.ZOTERO_INCOMPATIBLE,
            "Zotero returned an unsupported item payload.",
            stage="zotero",
            remediation="Upgrade Zotero or verify that API version 3 is enabled.",
            locator={"source": source},
        ) from exc


class LocalZoteroClient:
    """Read from Zotero's local API without accessing its database."""

    def __init__(
        self,
        settings: Settings = default_settings,
        *,
        client: httpx.AsyncClient | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float | httpx.Timeout = 5.0,
    ) -> None:
        if client is not None and transport is not None:
            raise ValueError("client and transport are mutually exclusive")
        self._base_url = settings.zotero_local_url.rstrip("/")
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(transport=transport, timeout=timeout)

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

    def _library_path(self, library_type: LibraryType, library_id: str | None) -> tuple[str, str]:
        if library_type == "user":
            return "users/0", "0"
        assert library_id is not None
        return f"groups/{quote(library_id, safe='')}", library_id

    async def _get(
        self,
        path: str,
        *,
        params: Mapping[str, str | int] | None = None,
    ) -> httpx.Response:
        response = await self._client.get(
            f"{self._base_url}/{path.lstrip('/')}",
            headers=_API_HEADERS,
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
                remediation="Upgrade Zotero or verify that its local API is enabled.",
                locator={"source": "local"},
            ) from exc

    async def status(self) -> ZoteroStatus:
        """Check the local API and return its advertised versions."""
        response = await self._get("")
        return {
            "available": True,
            "source": "local",
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
        """Search a user or group library."""
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
            source="local",
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
        """Get items by key without fetching linked attachments."""
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
            source="local",
            library_type=options.library_type,
            library_id=canonical_library_id,
        )
        by_key = {item.key: item for item in items}
        return [by_key[key] for key in options.item_keys if key in by_key]


LocalZoteroAdapter = LocalZoteroClient
ZoteroLocalClient = LocalZoteroClient
