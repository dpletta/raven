"""Pydantic schemas shared by the MCP surface and domain services."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class StoryKind(StrEnum):
    BODY = "body"
    HEADER = "header"
    FOOTER = "footer"
    FOOTNOTE = "footnote"
    ENDNOTE = "endnote"
    COMMENT = "comment"


class DocumentLocator(StrictModel):
    story: StoryKind = StoryKind.BODY
    story_part: str | None = None
    paragraph_index: int = Field(ge=0)
    paragraph_hash: str | None = None
    exact_text: str | None = None
    occurrence: int = Field(default=1, ge=1)
    prefix: str | None = None
    suffix: str | None = None


class InsertText(StrictModel):
    type: Literal["insert_text"]
    locator: DocumentLocator
    text: str
    position: Literal["start", "end", "before", "after"] = "end"
    tracked: bool = True


class ReplaceText(StrictModel):
    type: Literal["replace_text"]
    locator: DocumentLocator
    text: str
    replacement: str
    occurrence: int = Field(default=1, ge=1)
    tracked: bool = True


class DeleteText(StrictModel):
    type: Literal["delete_text"]
    locator: DocumentLocator
    text: str
    occurrence: int = Field(default=1, ge=1)
    tracked: bool = True


class InsertParagraph(StrictModel):
    type: Literal["insert_paragraph"]
    locator: DocumentLocator
    text: str
    position: Literal["before", "after"] = "after"
    style: str | None = None
    tracked: bool = True


class AddComment(StrictModel):
    type: Literal["add_comment"]
    locator: DocumentLocator
    text: str
    comment: str
    author: str | None = None
    initials: str | None = None


class SetAltText(StrictModel):
    type: Literal["set_alt_text"]
    locator: DocumentLocator
    title: str | None = None
    description: str


DocumentOperation = Annotated[
    InsertText | ReplaceText | DeleteText | InsertParagraph | AddComment | SetAltText,
    Field(discriminator="type"),
]


class CitationItemInput(StrictModel):
    item_key: str
    uri: str | None = None
    csl_json: dict[str, Any] | None = None
    locator: str | None = None
    label: str = "page"
    prefix: str | None = None
    suffix: str | None = None
    suppress_author: bool = False
    author_only: bool = False


class CitationInsertRequest(StrictModel):
    document_path: str
    locator: DocumentLocator
    items: list[CitationItemInput] = Field(min_length=1)
    expected_sha256: str | None = None
    formatted_citation: str | None = None
    author: str = "Raven"
    intent: str = "Insert Zotero citation"
    style: str | None = None
    locale: str | None = None


class CitationUpdateRequest(StrictModel):
    document_path: str
    citation_id: str
    items: list[CitationItemInput] | None = None
    formatted_citation: str | None = None
    expected_sha256: str | None = None
    author: str = "Raven"
    intent: str = "Update Zotero citation"


class CitationRemoveRequest(StrictModel):
    document_path: str
    citation_id: str
    expected_sha256: str | None = None
    keep_visible_text: bool = False
    author: str = "Raven"
    intent: str = "Remove Zotero citation"


class BibliographySyncRequest(StrictModel):
    document_path: str
    expected_sha256: str | None = None
    locator: DocumentLocator | None = None
    heading: str | None = "References"
    style: str | None = None
    locale: str | None = None
    author: str = "Raven"
    intent: str = "Synchronize Zotero bibliography"


class PrepareChangesRequest(StrictModel):
    document_path: str
    operations: list[DocumentOperation] = Field(min_length=1)
    expected_sha256: str | None = None
    author: str = "Raven"
    intent: str
    idempotency_key: str | None = None


class CommitRequest(StrictModel):
    transaction_id: str
    confirmation_token: str
    output_path: str
    overwrite: bool = False


class DocumentSummary(StrictModel):
    path: str
    sha256: str
    title: str | None = None
    paragraphs: int
    words: int
    headings: int
    tables: int
    figures: int
    footnotes: int
    endnotes: int
    comments: int
    revisions: int
    citations: int
    has_bibliography: bool
    warnings: list[str] = Field(default_factory=list)


class ParagraphView(StrictModel):
    locator: DocumentLocator
    text: str
    style: str | None = None
    kind: str = "paragraph"
    protected_ranges: list[tuple[int, int, str]] = Field(
        default_factory=lambda: list[tuple[int, int, str]]()
    )


class TransactionPreview(StrictModel):
    transaction_id: str
    confirmation_token: str
    source_path: str
    source_sha256: str
    created_at: datetime
    expires_at: datetime
    operations: list[dict[str, Any]]
    semantic_diff: list[dict[str, Any]]
    warnings: list[str]
    validation: dict[str, Any]


class CommitResult(StrictModel):
    transaction_id: str
    output_path: str
    source_sha256: str
    output_sha256: str
    changed_parts: list[str]
    audit_path: str
    backup_path: str | None = None


class ZoteroSearchRequest(StrictModel):
    query: str = ""
    library_type: Literal["user", "group"] = "user"
    library_id: str | None = None
    collection_key: str | None = None
    item_type: str | None = None
    tag: str | None = None
    limit: int = Field(default=20, ge=1, le=100)
    start: int = Field(default=0, ge=0)
    sort: str = "dateModified"
    direction: Literal["asc", "desc"] = "desc"
    search_mode: Literal["titleCreatorYear", "everything"] = "titleCreatorYear"

    @model_validator(mode="after")
    def require_group_id(self) -> ZoteroSearchRequest:
        if self.library_type == "group" and not self.library_id:
            raise ValueError("library_id is required for group libraries")
        return self


class ZoteroGetRequest(StrictModel):
    item_keys: list[str] = Field(min_length=1, max_length=100)
    library_type: Literal["user", "group"] = "user"
    library_id: str | None = None

    @model_validator(mode="after")
    def require_group_id(self) -> ZoteroGetRequest:
        if self.library_type == "group" and not self.library_id:
            raise ValueError("library_id is required for group libraries")
        return self


class ZoteroItem(StrictModel):
    key: str
    version: int | None = None
    library: dict[str, Any] = Field(default_factory=dict)
    data: dict[str, Any] = Field(default_factory=dict)
    csl_json: dict[str, Any] = Field(default_factory=dict)
    uri: str | None = None
    source: Literal["local", "web"]
