"""Read-only Zotero library MCP tools."""

from __future__ import annotations

from typing import Any, Literal

from mcp.server.mcpserver import MCPServer

from raven_mcp.schemas import ZoteroGetRequest, ZoteroSearchRequest
from raven_mcp.tools.common import guarded_async
from raven_mcp.zotero.client import ZoteroClient


def register_zotero_tools(server: MCPServer[Any], zotero: ZoteroClient) -> None:
    """Register local-first reference discovery tools."""

    @server.tool(
        description=(
            "Check whether Zotero is available. Raven tries the local desktop API first "
            "and uses the Web API only when credentials are configured and local access "
            "is unavailable."
        )
    )
    async def zotero_status() -> dict[str, Any]:
        return dict(await guarded_async(zotero.status))

    @server.tool(
        description=(
            "Search Zotero references without modifying the library. Returns complete "
            "CSL-JSON snapshots and canonical item URIs suitable for citation insertion. "
            "Local desktop Zotero is preferred; optional Web API credentials are a fallback."
        )
    )
    async def zotero_search(
        query: str = "",
        library_type: Literal["user", "group"] = "user",
        library_id: str | None = None,
        collection_key: str | None = None,
        item_type: str | None = None,
        tag: str | None = None,
        search_mode: Literal["titleCreatorYear", "everything"] = "titleCreatorYear",
        limit: int = 20,
        start: int = 0,
        sort: str = "dateModified",
        direction: Literal["asc", "desc"] = "desc",
    ) -> dict[str, Any]:
        request = ZoteroSearchRequest(
            query=query,
            library_type=library_type,
            library_id=library_id,
            collection_key=collection_key,
            item_type=item_type,
            tag=tag,
            search_mode=search_mode,
            limit=limit,
            start=start,
            sort=sort,
            direction=direction,
        )
        items = await guarded_async(lambda: zotero.search(request))
        return {
            "items": [item.model_dump(mode="json") for item in items],
            "returned": len(items),
            "start": start,
            "limit": limit,
        }

    @server.tool(
        description=(
            "Fetch exact Zotero items by their eight-character keys. Returns results in "
            "requested order with CSL-JSON and canonical URIs; missing keys are reported."
        )
    )
    async def zotero_get_items(
        item_keys: list[str],
        library_type: Literal["user", "group"] = "user",
        library_id: str | None = None,
    ) -> dict[str, Any]:
        request = ZoteroGetRequest(
            item_keys=item_keys,
            library_type=library_type,
            library_id=library_id,
        )
        items = await guarded_async(lambda: zotero.get_items(request))
        found = {item.key for item in items}
        return {
            "items": [item.model_dump(mode="json") for item in items],
            "missing_keys": [key for key in item_keys if key not in found],
        }
