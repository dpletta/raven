"""Document inspection, editing, commit, and validation tools."""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer

from raven_mcp.schemas import (
    CommitRequest,
    DocumentOperation,
    PrepareChangesRequest,
    StoryKind,
)
from raven_mcp.tools.common import guarded
from raven_mcp.transactions import TransactionManager


def register_document_tools(
    server: MCPServer[Any], transactions: TransactionManager
) -> None:
    """Register preservation-first DOCX tools."""

    @server.tool(
        description=(
            "Inspect a closed .docx before editing. Returns its SHA-256, summary, stable "
            "paragraph locators, protected ranges, stories, styles, fields, and warnings. "
            "Use returned locators and hash in prepare tools. Results are paginated."
        )
    )
    def document_inspect(
        document_path: str,
        story: StoryKind | None = None,
        offset: int = 0,
        limit: int = 200,
        include_metadata: bool = True,
    ) -> dict[str, Any]:
        if offset < 0 or not 1 <= limit <= 1000:
            raise ValueError("offset must be non-negative and limit must be 1-1000")
        result = guarded(lambda: transactions.inspect(document_path))
        paragraphs = result["paragraphs"]
        if story is not None:
            paragraphs = [
                paragraph
                for paragraph in paragraphs
                if paragraph["locator"]["story"] == story.value
            ]
        page = paragraphs[offset : offset + limit]
        return {
            "summary": result["summary"],
            "paragraphs": page,
            "pagination": {
                "offset": offset,
                "limit": limit,
                "returned": len(page),
                "total": len(paragraphs),
                "next_offset": offset + len(page)
                if offset + len(page) < len(paragraphs)
                else None,
            },
            "metadata": result["metadata"] if include_metadata else None,
        }

    @server.tool(
        description=(
            "Stage an atomic batch of tracked Word edits without writing a file. Supports "
            "text insertion/replacement/deletion, paragraphs, comments, and image alt text. "
            "Returns a semantic preview plus confirmation token for document_commit."
        )
    )
    def document_prepare_changes(
        document_path: str,
        operations: list[DocumentOperation],
        intent: str,
        expected_sha256: str | None = None,
        author: str = "Raven",
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        request = PrepareChangesRequest(
            document_path=document_path,
            operations=operations,
            expected_sha256=expected_sha256,
            author=author,
            intent=intent,
            idempotency_key=idempotency_key,
        )
        return guarded(
            lambda: transactions.prepare_changes(request).model_dump(mode="json")
        )

    @server.tool(
        description=(
            "Commit one previously prepared transaction after reviewing its preview. "
            "Rechecks the source hash, writes atomically, validates the package, and emits "
            "an audit JSON file. Existing outputs require overwrite=true and are backed up."
        )
    )
    def document_commit(
        transaction_id: str,
        confirmation_token: str,
        output_path: str,
        overwrite: bool = False,
    ) -> dict[str, Any]:
        request = CommitRequest(
            transaction_id=transaction_id,
            confirmation_token=confirmation_token,
            output_path=output_path,
            overwrite=overwrite,
        )
        return guarded(lambda: transactions.commit(request).model_dump(mode="json"))

    @server.tool(
        description="Abort a staged Raven transaction. No document file is changed."
    )
    def document_abort(transaction_id: str) -> dict[str, Any]:
        return transactions.abort(transaction_id)

    @server.tool(
        description=(
            "Validate a closed .docx without changing it. Profiles are package, semantic, "
            "zotero, or full. Reports malformed ZIP/XML, relationships, fields, comments, "
            "revisions, and Zotero payload/preferences."
        )
    )
    def document_validate(
        document_path: str, profile: str = "full"
    ) -> dict[str, Any]:
        return guarded(lambda: transactions.validate(document_path, profile))
