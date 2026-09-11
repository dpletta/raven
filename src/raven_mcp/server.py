"""Raven MCP server entry point."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from mcp.server.mcpserver import MCPServer

from raven_mcp import __version__
from raven_mcp.config import Settings
from raven_mcp.tools import (
    register_citation_tools,
    register_document_tools,
    register_zotero_tools,
)
from raven_mcp.transactions import TransactionManager
from raven_mcp.zotero.client import ZoteroClient


def create_server(runtime_settings: Settings | None = None) -> MCPServer[Any]:
    """Create an independently configured Raven MCP server."""

    configured = runtime_settings or Settings.from_env()
    transactions = TransactionManager(configured)
    zotero = ZoteroClient(configured)

    @asynccontextmanager
    async def lifespan(_: MCPServer[Any]) -> AsyncIterator[None]:
        try:
            yield None
        finally:
            await zotero.aclose()

    server: MCPServer[Any] = MCPServer(
        "raven-academic-writing",
        title="Raven Academic Writing MCP",
        description=(
            "Safely inspect and edit Word DOCX files and manage native Zotero citations."
        ),
        instructions=(
            "Keep documents closed in Word. Inspect first, then prepare changes, review the "
            "semantic preview, and commit to a new output path. Use the returned SHA-256 and "
            "paragraph locators for optimistic concurrency. Citation and bibliography fields "
            "need Zotero Refresh in desktop Word for authoritative formatting."
        ),
        version=__version__,
        lifespan=lifespan,
    )
    register_document_tools(server, transactions)
    register_zotero_tools(server, zotero)
    register_citation_tools(server, transactions, zotero)

    @server.resource(
        "raven://capabilities",
        name="Raven capabilities",
        description="Supported formats, safety rules, and known limitations.",
        mime_type="application/json",
    )
    def capabilities() -> dict[str, Any]:
        return {
            "version": __version__,
            "transport": "stdio",
            "documents": {
                "supported": [".docx transitional OOXML"],
                "rejected": [
                    "macro-enabled",
                    "encrypted",
                    "digitally signed",
                    "Strict OOXML",
                ],
                "stories": [
                    "body",
                    "headers",
                    "footers",
                    "footnotes",
                    "endnotes",
                    "comments",
                ],
                "tracked_text_edits": True,
                "classic_comments": True,
            },
            "citations": {
                "format": "native Zotero Word fields",
                "zotero_refresh_required": True,
                "fixture_validated_only": True,
                "libreoffice_editing": False,
                "word_online_plugin": False,
            },
            "safety": {
                "two_phase_transactions": True,
                "copy_on_write_default": True,
                "optimistic_concurrency": "SHA-256",
                "path_roots": [str(root) for root in configured.allowed_roots],
            },
        }

    @server.prompt(
        name="review_section",
        title="Review a manuscript section",
        description="Guide an agent through evidence-preserving section revision.",
    )
    def review_section(document_path: str, heading: str, objective: str) -> str:
        return (
            f"Inspect {document_path!r}. Locate the section headed {heading!r}, review it for "
            f"{objective}, and propose a minimal batch of tracked changes. Preserve citation "
            "fields and comments. Call document_prepare_changes, present its semantic diff, "
            "and do not commit until the user approves the exact output path."
        )

    @server.prompt(
        name="citation_audit",
        title="Audit manuscript citations",
        description="Guide an agent through a conservative citation audit.",
    )
    def citation_audit(document_path: str) -> str:
        return (
            f"Inspect {document_path!r}, list its live Zotero citations, and scan for plain-text "
            "citations. Match candidates only to exact Zotero records; never infer a source "
            "when multiple candidates remain. Report unresolved references and bibliography "
            "status before preparing any field changes."
        )

    return server


mcp = create_server()


def main() -> None:
    """Run Raven as a local stdio MCP server."""

    mcp.run(transport="stdio")


if __name__ == "__main__":  # pragma: no cover
    main()
