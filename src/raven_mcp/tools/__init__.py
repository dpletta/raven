"""MCP tool registration for Raven."""

from raven_mcp.tools.citations import register_citation_tools
from raven_mcp.tools.documents import register_document_tools
from raven_mcp.tools.zotero import register_zotero_tools

__all__ = [
    "register_citation_tools",
    "register_document_tools",
    "register_zotero_tools",
]
