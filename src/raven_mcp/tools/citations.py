"""Native Zotero citation and bibliography MCP tools."""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer

from raven_mcp.errors import ErrorCode, RavenError
from raven_mcp.schemas import (
    BibliographySyncRequest,
    CitationInsertRequest,
    CitationItemInput,
    CitationRemoveRequest,
    CitationUpdateRequest,
    DocumentLocator,
)
from raven_mcp.tools.common import as_tool_error, guarded
from raven_mcp.transactions import TransactionManager
from raven_mcp.zotero.client import ZoteroClient


async def _hydrate_items(
    items: list[CitationItemInput], zotero: ZoteroClient
) -> list[CitationItemInput]:
    missing = [item.item_key for item in items if not item.csl_json or not item.uri]
    if not missing:
        return items
    resolved = await zotero.get_items(missing)
    by_key = {item.key: item for item in resolved}
    unresolved = [key for key in missing if key not in by_key]
    if unresolved:
        raise RavenError(
            ErrorCode.REFERENCE_UNRESOLVED,
            f"Zotero items were not found: {', '.join(unresolved)}",
            stage="citation.resolve",
            remediation="Search Zotero again and use current item keys.",
        )
    return [
        item.model_copy(
            update={
                "csl_json": item.csl_json or by_key[item.item_key].csl_json,
                "uri": item.uri or by_key[item.item_key].uri,
            }
        )
        if item.item_key in by_key
        else item
        for item in items
    ]


def register_citation_tools(
    server: MCPServer[Any],
    transactions: TransactionManager,
    zotero: ZoteroClient,
) -> None:
    """Register citation transactions and audit helpers."""

    @server.tool(
        description=(
            "List live Zotero citation fields, embedded item payloads, visible results, "
            "document preferences, and bibliography status in a closed .docx."
        )
    )
    def citation_list(document_path: str) -> dict[str, Any]:
        return guarded(lambda: transactions.list_citations(document_path))

    @server.tool(
        description=(
            "Stage insertion of one native Zotero Word citation. Missing CSL item data "
            "and URIs are resolved from Zotero. The field remains editable by Zotero and "
            "requires Zotero Refresh for authoritative style, numbering, and disambiguation."
        )
    )
    async def citation_prepare_insert(
        document_path: str,
        locator: DocumentLocator,
        items: list[CitationItemInput],
        expected_sha256: str | None = None,
        formatted_citation: str | None = None,
        author: str = "Raven",
        intent: str = "Insert Zotero citation",
        style: str | None = None,
        locale: str | None = None,
    ) -> dict[str, Any]:
        try:
            hydrated = await _hydrate_items(items, zotero)
            request = CitationInsertRequest(
                document_path=document_path,
                locator=locator,
                items=hydrated,
                expected_sha256=expected_sha256,
                formatted_citation=formatted_citation,
                author=author,
                intent=intent,
                style=style,
                locale=locale,
            )
            return transactions.prepare_citation_insert(request).model_dump(mode="json")
        except RavenError as error:
            raise as_tool_error(error) from error

    @server.tool(
        description=(
            "Stage an update to one live citation selected by citationID. Unknown Zotero "
            "payload properties are preserved. Omit items to retain sources and only update "
            "the visible fallback."
        )
    )
    async def citation_prepare_update(
        document_path: str,
        citation_id: str,
        items: list[CitationItemInput] | None = None,
        formatted_citation: str | None = None,
        expected_sha256: str | None = None,
        author: str = "Raven",
        intent: str = "Update Zotero citation",
    ) -> dict[str, Any]:
        try:
            hydrated = await _hydrate_items(items, zotero) if items is not None else None
            request = CitationUpdateRequest(
                document_path=document_path,
                citation_id=citation_id,
                items=hydrated,
                formatted_citation=formatted_citation,
                expected_sha256=expected_sha256,
                author=author,
                intent=intent,
            )
            return transactions.prepare_citation_update(request).model_dump(mode="json")
        except RavenError as error:
            raise as_tool_error(error) from error

    @server.tool(
        description=(
            "Stage removal of one native Zotero citation. This atomic field operation is "
            "copy-on-write but intentionally not represented as a tracked deletion because "
            "Word tracked deletions can invalidate field instructions."
        )
    )
    def citation_prepare_remove(
        document_path: str,
        citation_id: str,
        expected_sha256: str | None = None,
        keep_visible_text: bool = False,
        author: str = "Raven",
        intent: str = "Remove Zotero citation",
    ) -> dict[str, Any]:
        request = CitationRemoveRequest(
            document_path=document_path,
            citation_id=citation_id,
            expected_sha256=expected_sha256,
            keep_visible_text=keep_visible_text,
            author=author,
            intent=intent,
        )
        return guarded(
            lambda: transactions.prepare_citation_remove(request).model_dump(mode="json")
        )

    @server.tool(
        description=(
            "Stage creation or synchronization of the document's single native Zotero "
            "bibliography field. A locator is required only when creating it. Zotero Refresh "
            "in desktop Word generates the authoritative entries."
        )
    )
    def bibliography_prepare_sync(
        document_path: str,
        expected_sha256: str | None = None,
        locator: DocumentLocator | None = None,
        heading: str | None = "References",
        style: str | None = None,
        locale: str | None = None,
        author: str = "Raven",
        intent: str = "Synchronize Zotero bibliography",
    ) -> dict[str, Any]:
        request = BibliographySyncRequest(
            document_path=document_path,
            expected_sha256=expected_sha256,
            locator=locator,
            heading=heading,
            style=style,
            locale=locale,
            author=author,
            intent=intent,
        )
        return guarded(lambda: transactions.prepare_bibliography(request).model_dump(mode="json"))

    @server.tool(
        description=(
            "Find likely plain-text author-year, narrative, and numeric citations while "
            "ignoring field instructions. This is a review aid only: resolve every match "
            "to an exact Zotero item before converting it."
        )
    )
    def citation_scan_plain(document_path: str) -> dict[str, Any]:
        return guarded(lambda: transactions.scan_plain_citations(document_path))
