"""Typed models used by Raven's DOCX core."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

from lxml import etree

from raven_mcp.schemas import DocumentLocator, DocumentSummary, ParagraphView, StoryKind


@dataclass(frozen=True, slots=True)
class StoryPart:
    """A WordprocessingML story and its package part."""

    kind: StoryKind
    part_name: str


@dataclass(frozen=True, slots=True)
class ProtectedRange:
    """A half-open text range that an ordinary edit may not cross."""

    start: int
    end: int
    kind: str

    def as_tuple(self) -> tuple[int, int, str]:
        return (self.start, self.end, self.kind)

    def overlaps(self, start: int, end: int) -> bool:
        if self.start == self.end:
            return start <= self.start <= end
        if start == end:
            return self.start <= start <= self.end
        return start < self.end and end > self.start


@dataclass(slots=True)
class TextSegment:
    """A visible piece of paragraph text and its backing XML node."""

    node: etree._Element
    start: int
    end: int
    text: str


@dataclass(slots=True)
class ParagraphRecord:
    """An addressable paragraph in one WordprocessingML story."""

    part_name: str
    story: StoryKind
    index: int
    element: etree._Element
    text: str
    style: str | None = None
    protected_ranges: list[ProtectedRange] = field(default_factory=list)
    segments: list[TextSegment] = field(default_factory=list)

    def as_view(self, locator: DocumentLocator) -> ParagraphView:
        return ParagraphView(
            locator=locator,
            text=self.text,
            style=self.style,
            protected_ranges=[item.as_tuple() for item in self.protected_ranges],
        )


@dataclass(slots=True)
class ComplexField:
    """A parsed Word complex field, possibly spanning paragraphs."""

    part_name: str
    start_paragraph: int
    end_paragraph: int | None
    instruction: str
    result: str
    start_run: int | None = None
    end_run: int | None = None
    separated: bool = False

    @property
    def balanced(self) -> bool:
        return self.end_paragraph is not None


@dataclass(slots=True)
class ResolvedLocator:
    """A locator resolved to a paragraph and optional exact text range."""

    record: ParagraphRecord
    start: int | None = None
    end: int | None = None


@dataclass(slots=True)
class DocumentInspection:
    """Pydantic-friendly aggregate returned by document inspection."""

    summary: DocumentSummary
    paragraphs: list[ParagraphView]
    metadata: dict[str, Any]

    def __iter__(
        self,
    ) -> Iterator[DocumentSummary | list[ParagraphView] | dict[str, Any]]:
        yield self.summary
        yield self.paragraphs
        yield self.metadata

    def as_dict(self) -> dict[str, Any]:
        return {
            "summary": self.summary.model_dump(mode="json"),
            "paragraphs": [item.model_dump(mode="json") for item in self.paragraphs],
            "metadata": self.metadata,
        }


__all__ = [
    "ComplexField",
    "DocumentInspection",
    "DocumentLocator",
    "DocumentSummary",
    "ParagraphRecord",
    "ParagraphView",
    "ProtectedRange",
    "ResolvedLocator",
    "StoryKind",
    "StoryPart",
    "TextSegment",
]
