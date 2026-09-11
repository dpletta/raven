"""WordprocessingML discovery, inspection, location, and edit helpers."""

from __future__ import annotations

import copy
import hashlib
import json
import posixpath
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from lxml import etree
from pydantic import ValidationError

from raven_mcp.docx.model import (
    ComplexField,
    ParagraphRecord,
    ProtectedRange,
    ResolvedLocator,
    StoryPart,
    TextSegment,
)
from raven_mcp.docx.opc import OFFICE_DOCUMENT_REL, OpcPackage
from raven_mcp.errors import ErrorCode, RavenError
from raven_mcp.schemas import DocumentLocator, DocumentSummary, ParagraphView, StoryKind

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
WP_NS = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
PIC_NS = "http://schemas.openxmlformats.org/drawingml/2006/picture"
V_NS = "urn:schemas-microsoft-com:vml"
MC_NS = "http://schemas.openxmlformats.org/markup-compatibility/2006"
W14_NS = "http://schemas.microsoft.com/office/word/2010/wordml"
W15_NS = "http://schemas.microsoft.com/office/word/2012/wordml"
XML_NS = "http://www.w3.org/XML/1998/namespace"
CP_NS = "http://schemas.openxmlformats.org/package/2006/metadata/core-properties"
DC_NS = "http://purl.org/dc/elements/1.1/"

NS = {
    "w": W_NS,
    "r": R_NS,
    "a": A_NS,
    "wp": WP_NS,
    "pic": PIC_NS,
    "v": V_NS,
    "mc": MC_NS,
    "w14": W14_NS,
    "w15": W15_NS,
    "cp": CP_NS,
    "dc": DC_NS,
}
NSMAP = NS

COMMENTS_REL = f"{R_NS}/comments"
STYLES_REL = f"{R_NS}/styles"
COMMENTS_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.comments+xml"
)

_STORY_RELATIONSHIPS: dict[str, StoryKind] = {
    f"{R_NS}/header": StoryKind.HEADER,
    f"{R_NS}/footer": StoryKind.FOOTER,
    f"{R_NS}/footnotes": StoryKind.FOOTNOTE,
    f"{R_NS}/endnotes": StoryKind.ENDNOTE,
    COMMENTS_REL: StoryKind.COMMENT,
}
_EXCLUDED_CONTAINERS = {f"{{{W_NS}}}del", f"{{{W_NS}}}moveFrom"}
_PROTECTED_CONTAINERS = {
    f"{{{W_NS}}}sdt": "content-control",
    f"{{{W_NS}}}ins": "revision",
    f"{{{W_NS}}}del": "revision",
    f"{{{W_NS}}}moveFrom": "revision",
    f"{{{W_NS}}}moveTo": "revision",
}
_REVISION_TAGS = {
    f"{{{W_NS}}}ins",
    f"{{{W_NS}}}del",
    f"{{{W_NS}}}moveFrom",
    f"{{{W_NS}}}moveTo",
}
_WORD_PATTERN = re.compile(r"\b[\w\u2019'-]+\b", re.UNICODE)


def qn(local_name: str, namespace: str = W_NS) -> str:
    """Return an expanded XML qualified name."""

    if ":" in local_name:
        prefix, local_name = local_name.split(":", 1)
        try:
            namespace = NS[prefix]
        except KeyError as exc:
            raise ValueError(f"Unknown XML namespace prefix: {prefix}") from exc
    return f"{{{namespace}}}{local_name}"


def main_document_part(package: OpcPackage) -> str:
    """Return the main document part selected by the package relationship."""

    for relationship in package.relationships(None):
        if relationship.relationship_type == OFFICE_DOCUMENT_REL:
            target = package.relationship_target(relationship)
            if target is not None:
                return target
    raise RavenError(
        ErrorCode.UNSUPPORTED_DOCUMENT,
        "The package has no main Word document part.",
        stage="wordml",
    )


def discover_stories(package: OpcPackage) -> list[StoryPart]:
    """Discover editable Word stories without following external targets."""

    document_part = main_document_part(package)
    stories = [StoryPart(StoryKind.BODY, document_part)]
    seen = {document_part}
    for relationship in package.relationships(document_part):
        kind = _STORY_RELATIONSHIPS.get(relationship.relationship_type)
        if kind is None or relationship.external:
            continue
        target = package.relationship_target(relationship)
        if target is not None and target not in seen and package.has_part(target):
            stories.append(StoryPart(kind, target))
            seen.add(target)
    return stories


def style_lookup(package: OpcPackage) -> dict[str, str]:
    """Map paragraph style IDs to display names."""

    document_part = main_document_part(package)
    styles_part: str | None = None
    for relationship in package.relationships(document_part):
        if relationship.relationship_type == STYLES_REL and not relationship.external:
            styles_part = package.relationship_target(relationship)
            break
    if styles_part is None and package.has_part("word/styles.xml"):
        styles_part = "word/styles.xml"
    if styles_part is None or not package.has_part(styles_part):
        return {}

    styles: dict[str, str] = {}
    root = package.read_xml(styles_part)
    for style in root.findall(".//w:style", namespaces=NS):
        style_id = style.get(qn("styleId"))
        name = style.find("w:name", namespaces=NS)
        if style_id:
            styles[style_id] = (name.get(qn("val")) if name is not None else None) or style_id
    return styles


def paragraph_style(
    paragraph: etree._Element,
    styles: Mapping[str, str] | None = None,
) -> str | None:
    """Return a paragraph's style ID or mapped display name."""

    style = paragraph.find("w:pPr/w:pStyle", namespaces=NS)
    if style is None:
        return None
    style_id = style.get(qn("val"))
    if style_id is None:
        return None
    return styles.get(style_id, style_id) if styles is not None else style_id


def _ancestor_has(node: etree._Element, tags: set[str]) -> bool:
    parent = node.getparent()
    while parent is not None:
        if parent.tag in tags:
            return True
        parent = parent.getparent()
    return False


def _paragraph_map(
    paragraph: etree._Element,
) -> tuple[str, list[TextSegment], dict[etree._Element, tuple[int, int]]]:
    chunks: list[str] = []
    segments: list[TextSegment] = []
    positions: dict[etree._Element, tuple[int, int]] = {}
    offset = 0

    def walk(element: etree._Element, excluded: bool = False) -> None:
        nonlocal offset
        start = offset
        hidden = excluded or element.tag in _EXCLUDED_CONTAINERS
        if element.tag in {qn("instrText"), qn("delText")}:
            hidden = True
        if element.tag == qn("t"):
            if not hidden:
                value = element.text or ""
                chunks.append(value)
                segments.append(TextSegment(element, offset, offset + len(value), value))
                offset += len(value)
        elif element.tag == qn("tab") and not hidden:
            chunks.append("\t")
            segments.append(TextSegment(element, offset, offset + 1, "\t"))
            offset += 1
        elif element.tag in {qn("br"), qn("cr")} and not hidden:
            chunks.append("\n")
            segments.append(TextSegment(element, offset, offset + 1, "\n"))
            offset += 1
        elif not hidden:
            for child in element:
                walk(child, hidden)
        positions[element] = (start, offset)

    walk(paragraph)
    return "".join(chunks), segments, positions


def paragraph_text(paragraph: etree._Element) -> str:
    """Extract visible paragraph text, including insertions but not deletions."""

    return _paragraph_map(paragraph)[0]


def paragraph_hash(text_or_paragraph: str | etree._Element) -> str:
    """Return the stable SHA-256 locator hash for visible paragraph text."""

    text = (
        paragraph_text(text_or_paragraph)
        if isinstance(text_or_paragraph, etree._Element)
        else text_or_paragraph
    )
    return hashlib.sha256(text.replace("\r\n", "\n").encode("utf-8")).hexdigest()


def make_locator(record: ParagraphRecord) -> DocumentLocator:
    """Create a locator that detects stale paragraph content."""

    return DocumentLocator(
        story=record.story,
        story_part=record.part_name,
        paragraph_index=record.index,
        paragraph_hash=paragraph_hash(record.text),
    )


def _deduplicate_ranges(ranges: list[ProtectedRange]) -> list[ProtectedRange]:
    unique = {(item.start, item.end, item.kind): item for item in ranges}
    return sorted(unique.values(), key=lambda item: (item.start, item.end, item.kind))


def _protect_span(
    records: list[ParagraphRecord],
    start: tuple[int, int],
    end: tuple[int, int],
    kind: str,
) -> None:
    start_index, start_offset = start
    end_index, end_offset = end
    for index in range(start_index, end_index + 1):
        left = start_offset if index == start_index else 0
        right = end_offset if index == end_index else len(records[index].text)
        records[index].protected_ranges.append(ProtectedRange(left, right, kind))


def _apply_story_protections(
    records: list[ParagraphRecord],
    positions: list[dict[etree._Element, tuple[int, int]]],
) -> None:
    field_stack: list[tuple[int, int]] = []
    bookmarks: dict[str, tuple[int, int]] = {}

    for index, record in enumerate(records):
        paragraph = record.element
        position_map = positions[index]

        ancestor = paragraph.getparent()
        while ancestor is not None:
            kind = _PROTECTED_CONTAINERS.get(ancestor.tag)
            if kind is not None:
                record.protected_ranges.append(ProtectedRange(0, len(record.text), kind))
            ancestor = ancestor.getparent()

        for element in paragraph.iter():
            start, end = position_map.get(element, (0, 0))
            kind = _PROTECTED_CONTAINERS.get(element.tag)
            if kind is not None:
                record.protected_ranges.append(ProtectedRange(start, end, kind))

            if element.tag == qn("fldChar"):
                field_type = element.get(qn("fldCharType"))
                record.protected_ranges.append(ProtectedRange(start, start, "field-boundary"))
                if field_type == "begin":
                    field_stack.append((index, start))
                elif field_type == "end" and field_stack:
                    field_start = field_stack.pop()
                    _protect_span(records, field_start, (index, end), "field")

            if element.tag == qn("bookmarkStart"):
                bookmark_id = element.get(qn("id"))
                record.protected_ranges.append(ProtectedRange(start, start, "bookmark-boundary"))
                if bookmark_id is not None:
                    bookmarks[bookmark_id] = (index, start)
            elif element.tag == qn("bookmarkEnd"):
                bookmark_id = element.get(qn("id"))
                record.protected_ranges.append(ProtectedRange(start, start, "bookmark-boundary"))
                if bookmark_id is not None and bookmark_id in bookmarks:
                    _protect_span(
                        records,
                        bookmarks.pop(bookmark_id),
                        (index, end),
                        "bookmark",
                    )

    final_index = len(records) - 1
    if final_index >= 0:
        for start in field_stack:
            _protect_span(
                records,
                start,
                (final_index, len(records[final_index].text)),
                "unbalanced-field",
            )
        for start in bookmarks.values():
            _protect_span(
                records,
                start,
                (final_index, len(records[final_index].text)),
                "unbalanced-bookmark",
            )
    for record in records:
        record.protected_ranges = _deduplicate_ranges(record.protected_ranges)


def story_paragraphs(
    package: OpcPackage,
    story: StoryPart,
    styles: Mapping[str, str] | None = None,
) -> list[ParagraphRecord]:
    """Return addressable paragraph records for one story."""

    root = package.read_xml(story.part_name)
    paragraphs = root.findall(".//w:p", namespaces=NS)
    if story.kind in {StoryKind.FOOTNOTE, StoryKind.ENDNOTE}:
        note_tag = qn("footnote") if story.kind == StoryKind.FOOTNOTE else qn("endnote")
        filtered: list[etree._Element] = []
        for paragraph in paragraphs:
            note = paragraph.getparent()
            while note is not None and note.tag != note_tag:
                note = note.getparent()
            note_type = note.get(qn("type")) if note is not None else None
            note_id = note.get(qn("id"), "") if note is not None else ""
            if note_type not in {"separator", "continuationSeparator"} and not (
                note_id.startswith("-")
            ):
                filtered.append(paragraph)
        paragraphs = filtered
    records: list[ParagraphRecord] = []
    positions: list[dict[etree._Element, tuple[int, int]]] = []
    for index, paragraph in enumerate(paragraphs):
        text, segments, position_map = _paragraph_map(paragraph)
        records.append(
            ParagraphRecord(
                part_name=story.part_name,
                story=story.kind,
                index=index,
                element=paragraph,
                text=text,
                style=paragraph_style(paragraph, styles),
                segments=segments,
            )
        )
        positions.append(position_map)
    _apply_story_protections(records, positions)
    return records


def all_paragraphs(package: OpcPackage) -> list[ParagraphRecord]:
    """Return paragraph records from every discovered story."""

    styles = style_lookup(package)
    return [
        record
        for story in discover_stories(package)
        for record in story_paragraphs(package, story, styles)
    ]


@dataclass(slots=True)
class _FieldBuilder:
    part_name: str
    start_paragraph: int
    start_run: int | None
    instruction: list[str] = field(default_factory=list)
    result: list[str] = field(default_factory=list)
    separated: bool = False


def parse_complex_fields(
    paragraphs: Iterable[etree._Element],
    *,
    part_name: str = "",
    require_balanced: bool = False,
) -> list[ComplexField]:
    """Parse nested complex fields across run and paragraph boundaries."""

    paragraph_list = list(paragraphs)
    stack: list[_FieldBuilder] = []
    parsed: list[ComplexField] = []
    unmatched_ends = 0

    for paragraph_index, paragraph in enumerate(paragraph_list):
        runs = {
            run: run_index
            for run_index, run in enumerate(paragraph.findall(".//w:r", namespaces=NS))
        }
        for element in paragraph.iter():
            if _ancestor_has(element, _EXCLUDED_CONTAINERS):
                continue
            run: etree._Element | None = element
            while run is not None and run.tag != qn("r"):
                run = run.getparent()
            run_index = runs.get(run) if run is not None else None

            if element.tag == qn("fldChar"):
                field_type = element.get(qn("fldCharType"))
                if field_type == "begin":
                    stack.append(_FieldBuilder(part_name, paragraph_index, run_index))
                elif field_type == "separate" and stack:
                    stack[-1].separated = True
                elif field_type == "end":
                    if not stack:
                        unmatched_ends += 1
                        continue
                    builder = stack.pop()
                    parsed.append(
                        ComplexField(
                            part_name=builder.part_name,
                            start_paragraph=builder.start_paragraph,
                            end_paragraph=paragraph_index,
                            instruction="".join(builder.instruction).strip(),
                            result="".join(builder.result),
                            start_run=builder.start_run,
                            end_run=run_index,
                            separated=builder.separated,
                        )
                    )
                continue

            if element.tag == qn("instrText") and stack:
                stack[-1].instruction.append(element.text or "")
            elif (
                element.tag == qn("t")
                and stack
                and not _ancestor_has(element, _EXCLUDED_CONTAINERS)
            ):
                value = element.text or ""
                for builder in stack:
                    if builder.separated:
                        builder.result.append(value)

    for builder in stack:
        parsed.append(
            ComplexField(
                part_name=builder.part_name,
                start_paragraph=builder.start_paragraph,
                end_paragraph=None,
                instruction="".join(builder.instruction).strip(),
                result="".join(builder.result),
                start_run=builder.start_run,
                separated=builder.separated,
            )
        )
    if require_balanced and (stack or unmatched_ends):
        raise ValueError(
            f"Unbalanced complex fields: {len(stack)} unclosed, "
            f"{unmatched_ends} unmatched end markers."
        )
    return sorted(
        parsed,
        key=lambda item: (
            item.start_paragraph,
            item.start_run if item.start_run is not None else -1,
        ),
    )


def field_balance_errors(
    paragraphs: Iterable[etree._Element],
    *,
    part_name: str = "",
) -> list[str]:
    """Report unmatched complex-field begin and end markers."""

    depth = 0
    errors: list[str] = []
    for paragraph_index, paragraph in enumerate(paragraphs):
        for element in paragraph.iter(qn("fldChar")):
            if _ancestor_has(element, _EXCLUDED_CONTAINERS):
                continue
            field_type = element.get(qn("fldCharType"))
            if field_type == "begin":
                depth += 1
            elif field_type == "end":
                if depth == 0:
                    errors.append(
                        f"{part_name}: unmatched field end in paragraph {paragraph_index}."
                    )
                else:
                    depth -= 1
    if depth:
        errors.append(f"{part_name}: {depth} complex field(s) are not closed.")
    return errors


def _story_field_data(package: OpcPackage) -> tuple[list[ComplexField], list[str]]:
    fields: list[ComplexField] = []
    errors: list[str] = []
    for story in discover_stories(package):
        root = package.read_xml(story.part_name)
        paragraphs = root.findall(".//w:p", namespaces=NS)
        fields.extend(parse_complex_fields(paragraphs, part_name=story.part_name))
        errors.extend(field_balance_errors(paragraphs, part_name=story.part_name))
    return fields, errors


def _core_metadata(package: OpcPackage) -> dict[str, str]:
    part_name = "docProps/core.xml"
    if not package.has_part(part_name):
        return {}
    root = package.read_xml(part_name)
    result: dict[str, str] = {}
    for element in root:
        local_name = etree.QName(element).localname
        if element.text:
            result[local_name] = element.text
    return result


def _note_count(root: etree._Element, note_name: str) -> int:
    count = 0
    for note in root.findall(f".//w:{note_name}", namespaces=NS):
        note_type = note.get(qn("type"))
        note_id = note.get(qn("id"), "")
        if note_type not in {"separator", "continuationSeparator"} and not note_id.startswith("-"):
            count += 1
    return count


def inspect_document(
    package: OpcPackage,
    path: str | Path,
    sha: str,
) -> tuple[DocumentSummary, list[ParagraphView], dict[str, Any]]:
    """Inspect all Word stories and return summary, paragraph views, and metadata."""

    stories = discover_stories(package)
    styles = style_lookup(package)
    records_by_story = {
        story.part_name: story_paragraphs(package, story, styles) for story in stories
    }
    records = [record for story in stories for record in records_by_story[story.part_name]]
    views = [record.as_view(make_locator(record)) for record in records]
    fields, field_errors = _story_field_data(package)

    headings = 0
    tables = 0
    figures = 0
    footnotes = 0
    endnotes = 0
    comments = 0
    revisions = 0
    for story in stories:
        root = package.read_xml(story.part_name)
        story_records = records_by_story[story.part_name]
        for record in story_records:
            style = (record.style or "").casefold().replace(" ", "")
            has_outline = record.element.find("w:pPr/w:outlineLvl", namespaces=NS) is not None
            if style.startswith("heading") or has_outline:
                headings += 1
        tables += len(root.findall(".//w:tbl", namespaces=NS))
        figures += sum(
            len(root.findall(f".//w:{name}", namespaces=NS))
            for name in ("drawing", "pict", "object")
        )
        revisions += sum(1 for element in root.iter() if element.tag in _REVISION_TAGS)
        if story.kind == StoryKind.FOOTNOTE:
            footnotes += _note_count(root, "footnote")
        elif story.kind == StoryKind.ENDNOTE:
            endnotes += _note_count(root, "endnote")
        elif story.kind == StoryKind.COMMENT:
            comments += len(root.findall(".//w:comment", namespaces=NS))

    citations = sum(
        1
        for item in fields
        if "ZOTERO_ITEM" in item.instruction or "CSL_CITATION" in item.instruction
    )
    has_bibliography = any("ZOTERO_BIBL" in item.instruction for item in fields)
    external = [
        {
            "source_part": item.source_part,
            "id": item.relationship_id,
            "type": item.relationship_type,
            "target": item.target,
        }
        for item in package.external_relationships()
    ]
    warnings = list(field_errors)
    if external:
        warnings.append(
            f"The package declares {len(external)} external relationship(s); "
            "Raven did not fetch them."
        )
    core = _core_metadata(package)
    summary = DocumentSummary(
        path=str(path),
        sha256=sha,
        title=core.get("title"),
        paragraphs=len(records),
        words=sum(len(_WORD_PATTERN.findall(record.text)) for record in records),
        headings=headings,
        tables=tables,
        figures=figures,
        footnotes=footnotes,
        endnotes=endnotes,
        comments=comments,
        revisions=revisions,
        citations=citations,
        has_bibliography=has_bibliography,
        warnings=warnings,
    )
    metadata: dict[str, Any] = {
        "core_properties": core,
        "stories": [
            {
                "kind": story.kind.value,
                "part": story.part_name,
                "paragraphs": len(records_by_story[story.part_name]),
            }
            for story in stories
        ],
        "styles": styles,
        "external_relationships": external,
        "complex_fields": [
            {
                "part": item.part_name,
                "start_paragraph": item.start_paragraph,
                "end_paragraph": item.end_paragraph,
                "instruction": item.instruction,
                "result": item.result,
                "balanced": item.balanced,
            }
            for item in fields
        ],
    }
    return summary, views, metadata


def _locator_dict(locator: DocumentLocator) -> dict[str, Any]:
    return locator.model_dump(mode="json")


def _coerce_locator(
    locator: DocumentLocator | Mapping[str, Any],
) -> DocumentLocator:
    if isinstance(locator, DocumentLocator):
        return locator
    try:
        return DocumentLocator.model_validate(locator)
    except ValidationError as exc:
        raise RavenError(
            ErrorCode.INVALID_REQUEST,
            f"Invalid document locator: {exc}",
            stage="locator",
        ) from exc


def _exact_occurrences(text: str, exact: str) -> list[int]:
    if not exact:
        return []
    result: list[int] = []
    offset = 0
    while (found := text.find(exact, offset)) >= 0:
        result.append(found)
        offset = found + max(1, len(exact))
    return result


def resolve_locator(
    package: OpcPackage,
    locator: DocumentLocator | Mapping[str, Any],
) -> ResolvedLocator:
    """Resolve and verify a paragraph locator and its exact-text occurrence."""

    value = _coerce_locator(locator)
    candidates = [
        story
        for story in discover_stories(package)
        if story.kind == value.story
        and (value.story_part is None or story.part_name == value.story_part.removeprefix("/"))
    ]
    if not candidates:
        raise RavenError(
            ErrorCode.ANCHOR_NOT_FOUND,
            "The locator's story part does not exist.",
            stage="locator",
            locator=_locator_dict(value),
        )
    if len(candidates) > 1:
        raise RavenError(
            ErrorCode.ANCHOR_AMBIGUOUS,
            "The locator matches multiple story parts; specify story_part.",
            stage="locator",
            locator=_locator_dict(value),
        )
    records = story_paragraphs(package, candidates[0], style_lookup(package))
    if value.paragraph_index >= len(records):
        raise RavenError(
            ErrorCode.ANCHOR_NOT_FOUND,
            "The locator's paragraph index does not exist.",
            stage="locator",
            locator=_locator_dict(value),
        )
    record = records[value.paragraph_index]
    if value.paragraph_hash is not None and paragraph_hash(record.text) != value.paragraph_hash:
        raise RavenError(
            ErrorCode.STALE_REVISION,
            "The paragraph hash no longer matches the document.",
            stage="locator",
            remediation="Inspect the document again and use a fresh locator.",
            locator=_locator_dict(value),
        )
    if value.exact_text is None:
        return ResolvedLocator(record)

    starts = _exact_occurrences(record.text, value.exact_text)
    contextual: list[int] = []
    for start in starts:
        end = start + len(value.exact_text)
        if value.prefix is not None and not record.text[:start].endswith(value.prefix):
            continue
        if value.suffix is not None and not record.text[end:].startswith(value.suffix):
            continue
        contextual.append(start)
    if value.occurrence > len(contextual):
        raise RavenError(
            ErrorCode.ANCHOR_NOT_FOUND,
            "The requested exact-text occurrence was not found.",
            stage="locator",
            locator=_locator_dict(value),
        )
    start = contextual[value.occurrence - 1]
    return ResolvedLocator(record, start, start + len(value.exact_text))


def _assert_editable(record: ParagraphRecord, start: int, end: int) -> None:
    conflicts = [item.kind for item in record.protected_ranges if item.overlaps(start, end)]
    if conflicts:
        kinds = ", ".join(sorted(set(conflicts)))
        raise RavenError(
            ErrorCode.PROTECTED_BOUNDARY,
            f"The edit crosses a protected {kinds} range.",
            stage="edit",
            locator=_locator_dict(make_locator(record)),
        )


def _set_text(node: etree._Element, value: str) -> None:
    node.text = value
    space = f"{{{XML_NS}}}space"
    if value[:1].isspace() or value[-1:].isspace():
        node.set(space, "preserve")
    else:
        node.attrib.pop(space, None)


def _run_for_node(node: etree._Element) -> etree._Element | None:
    current: etree._Element | None = node
    while current is not None and current.tag != qn("r"):
        current = current.getparent()
    return current


def _simple_run_text(run: etree._Element) -> str:
    for child in run:
        if child.tag not in {qn("rPr"), qn("t")}:
            raise RavenError(
                ErrorCode.PROTECTED_BOUNDARY,
                "The edit crosses unsupported run markup.",
                stage="edit",
            )
    return "".join(child.text or "" for child in run if child.tag == qn("t"))


def _set_simple_run_text(run: etree._Element, value: str) -> None:
    for child in list(run):
        if child.tag != qn("rPr"):
            run.remove(child)
    if value:
        text = etree.SubElement(run, qn("t"))
        _set_text(text, value)


def _split_simple_run(
    run: etree._Element,
    offset: int,
) -> tuple[etree._Element | None, etree._Element | None]:
    value = _simple_run_text(run)
    if offset <= 0:
        return None, run
    if offset >= len(value):
        return run, None
    parent = run.getparent()
    if parent is None:
        raise RavenError(
            ErrorCode.PROTECTED_BOUNDARY,
            "The target run is detached.",
            stage="edit",
        )
    right = copy.deepcopy(run)
    _set_simple_run_text(run, value[:offset])
    _set_simple_run_text(right, value[offset:])
    parent.insert(parent.index(run) + 1, right)
    return run, right


def _direct_run_ranges(
    record: ParagraphRecord,
) -> tuple[
    list[etree._Element],
    dict[etree._Element, tuple[int, int]],
]:
    ranges: dict[etree._Element, tuple[int, int]] = {}
    runs: list[etree._Element] = []
    for segment in record.segments:
        run = _run_for_node(segment.node)
        if run is None or run.getparent() is not record.element:
            raise RavenError(
                ErrorCode.PROTECTED_BOUNDARY,
                "The edit crosses nested run markup.",
                stage="edit",
            )
        if run not in ranges:
            runs.append(run)
            ranges[run] = (segment.start, segment.end)
        else:
            left, right = ranges[run]
            ranges[run] = (min(left, segment.start), max(right, segment.end))
    return runs, ranges


def _isolate_runs(
    record: ParagraphRecord,
    start: int,
    end: int,
) -> list[etree._Element]:
    runs, ranges = _direct_run_ranges(record)
    selected = [run for run in runs if ranges[run][0] < end and ranges[run][1] > start]
    if not selected:
        raise RavenError(
            ErrorCode.ANCHOR_NOT_FOUND,
            "The exact text is not backed by editable text runs.",
            stage="edit",
        )
    first = selected[0]
    last = selected[-1]
    first_start, _ = ranges[first]
    last_start, last_end = ranges[last]
    start_offset = start - first_start
    end_offset = end - last_start

    if first is last:
        selected_end = first
        if end_offset < last_end - last_start:
            selected_end, _ = _split_simple_run(first, end_offset)
            if selected_end is None:
                raise RavenError(
                    ErrorCode.INTERNAL_ERROR,
                    "Unable to split target run.",
                    stage="edit",
                )
        if start_offset > 0:
            _, isolated = _split_simple_run(selected_end, start_offset)
            if isolated is None:
                raise RavenError(
                    ErrorCode.INTERNAL_ERROR,
                    "Unable to isolate target text.",
                    stage="edit",
                )
            first = isolated
            last = isolated
        else:
            first = selected_end
            last = selected_end
    else:
        if end_offset < last_end - last_start:
            isolated_last, _ = _split_simple_run(last, end_offset)
            if isolated_last is not None:
                last = isolated_last
        if start_offset > 0:
            _, isolated_first = _split_simple_run(first, start_offset)
            if isolated_first is not None:
                first = isolated_first

    parent = record.element
    first_index = parent.index(first)
    last_index = parent.index(last)
    result: list[etree._Element] = []
    for child in list(parent)[first_index : last_index + 1]:
        if child.tag != qn("r"):
            raise RavenError(
                ErrorCode.PROTECTED_BOUNDARY,
                "The edit crosses non-run paragraph markup.",
                stage="edit",
            )
        _simple_run_text(child)
        result.append(child)
    return result


def _new_run(
    text: str,
    template: etree._Element | None = None,
    *,
    deleted: bool = False,
) -> etree._Element:
    run = etree.Element(qn("r"))
    if template is not None:
        properties = template.find("w:rPr", namespaces=NS)
        if properties is not None:
            run.append(copy.deepcopy(properties))
    tag = qn("delText") if deleted else qn("t")
    pieces = re.split(r"(\t|\n)", text)
    for piece in pieces:
        if not piece:
            continue
        if piece == "\t" and not deleted:
            etree.SubElement(run, qn("tab"))
        elif piece == "\n" and not deleted:
            etree.SubElement(run, qn("br"))
        else:
            node = etree.SubElement(run, tag)
            _set_text(node, piece)
    return run


def _revision_element(
    kind: str,
    author: str,
    date: datetime,
    revision_id: int,
) -> etree._Element:
    element = etree.Element(qn(kind))
    element.set(qn("id"), str(revision_id))
    element.set(qn("author"), author)
    normalized = date.astimezone(UTC) if date.tzinfo else date.replace(tzinfo=UTC)
    element.set(qn("date"), normalized.isoformat().replace("+00:00", "Z"))
    return element


def _next_revision_id(package: OpcPackage) -> int:
    maximum = -1
    for name in package.members:
        if not name.endswith(".xml"):
            continue
        root = package.read_xml(name)
        for element in root.iter():
            if element.tag not in _REVISION_TAGS:
                continue
            value = element.get(qn("id"), "")
            if value.isdigit():
                maximum = max(maximum, int(value))
    return maximum + 1


def _untracked_replace(
    record: ParagraphRecord,
    start: int,
    end: int,
    replacement: str,
) -> None:
    affected = [
        segment for segment in record.segments if segment.start < end and segment.end > start
    ]
    if not affected or any(segment.node.tag != qn("t") for segment in affected):
        raise RavenError(
            ErrorCode.PROTECTED_BOUNDARY,
            "The edit crosses a tab, break, or unsupported text node.",
            stage="edit",
        )
    first = affected[0]
    last = affected[-1]
    first_left = max(0, start - first.start)
    last_right = max(0, end - last.start)
    if first is last:
        _set_text(
            first.node,
            first.text[:first_left] + replacement + first.text[last_right:],
        )
        return
    _set_text(first.node, first.text[:first_left] + replacement)
    for segment in affected[1:-1]:
        _set_text(segment.node, "")
    _set_text(last.node, last.text[last_right:])


def _tracked_replace(
    package: OpcPackage,
    record: ParagraphRecord,
    start: int,
    end: int,
    replacement: str,
    author: str,
    date: datetime,
) -> None:
    runs = _isolate_runs(record, start, end)
    parent = record.element
    insertion_index = parent.index(runs[0])
    deleted_runs = [_new_run(_simple_run_text(run), run, deleted=True) for run in runs]
    template = runs[0]
    for run in runs:
        parent.remove(run)

    revision_id = _next_revision_id(package)
    deleted = _revision_element("del", author, date, revision_id)
    for run in deleted_runs:
        deleted.append(run)
    parent.insert(insertion_index, deleted)
    insertion_index += 1
    if replacement:
        inserted = _revision_element("ins", author, date, revision_id + 1)
        inserted.append(_new_run(replacement, template))
        parent.insert(insertion_index, inserted)


def _insertion_point(
    record: ParagraphRecord,
    position: int,
) -> tuple[int, etree._Element | None]:
    paragraph = record.element
    if not record.segments:
        paragraph_properties = paragraph.find("w:pPr", namespaces=NS)
        return (1 if paragraph_properties is not None else 0), None
    runs, ranges = _direct_run_ranges(record)
    for run in runs:
        start, end = ranges[run]
        if start <= position <= end:
            _simple_run_text(run)
            offset = position - start
            if offset == 0:
                return paragraph.index(run), run
            if offset == end - start:
                return paragraph.index(run) + 1, run
            left, right = _split_simple_run(run, offset)
            template = left or right
            if right is None or template is None:
                raise RavenError(
                    ErrorCode.INTERNAL_ERROR,
                    "Unable to split insertion run.",
                    stage="edit",
                )
            return paragraph.index(right), template
    return len(paragraph), runs[-1] if runs else None


def insert_text(
    package: OpcPackage,
    locator: DocumentLocator | Mapping[str, Any],
    text: str,
    *,
    position: str = "end",
    tracked: bool = True,
    author: str = "Raven",
    date: datetime | None = None,
) -> None:
    """Insert text at a paragraph or exact-text boundary."""

    resolved = resolve_locator(package, locator)
    if position == "start":
        offset = 0
    elif position == "end":
        offset = len(resolved.record.text)
    elif position == "before" and resolved.start is not None:
        offset = resolved.start
    elif position == "after" and resolved.end is not None:
        offset = resolved.end
    else:
        raise RavenError(
            ErrorCode.INVALID_REQUEST,
            "before/after insertion requires locator.exact_text.",
            stage="edit",
        )
    _assert_editable(resolved.record, offset, offset)
    insertion_index, template = _insertion_point(resolved.record, offset)
    if tracked:
        revision = _revision_element(
            "ins",
            author,
            date or datetime.now(UTC),
            _next_revision_id(package),
        )
        revision.append(_new_run(text, template))
        resolved.record.element.insert(insertion_index, revision)
    else:
        resolved.record.element.insert(insertion_index, _new_run(text, template))
    package.set_xml(resolved.record.part_name, resolved.record.element.getroottree())


def replace_text(
    package: OpcPackage,
    locator: DocumentLocator | Mapping[str, Any],
    text: str,
    replacement: str,
    *,
    occurrence: int = 1,
    tracked: bool = True,
    author: str = "Raven",
    date: datetime | None = None,
) -> None:
    """Replace one verified visible-text occurrence."""

    if occurrence < 1:
        raise RavenError(
            ErrorCode.INVALID_REQUEST,
            "Text occurrence must be at least 1.",
            stage="edit",
        )
    value = _coerce_locator(locator).model_copy(
        update={"exact_text": text, "occurrence": occurrence}
    )
    resolved = resolve_locator(package, value)
    if resolved.start is None or resolved.end is None:
        raise RavenError(
            ErrorCode.ANCHOR_NOT_FOUND,
            "Replacement text was not found.",
            stage="edit",
        )
    _assert_editable(resolved.record, resolved.start, resolved.end)
    if tracked:
        _tracked_replace(
            package,
            resolved.record,
            resolved.start,
            resolved.end,
            replacement,
            author,
            date or datetime.now(UTC),
        )
    else:
        _untracked_replace(
            resolved.record,
            resolved.start,
            resolved.end,
            replacement,
        )
    package.set_xml(resolved.record.part_name, resolved.record.element.getroottree())


def delete_text(
    package: OpcPackage,
    locator: DocumentLocator | Mapping[str, Any],
    text: str,
    *,
    occurrence: int = 1,
    tracked: bool = True,
    author: str = "Raven",
    date: datetime | None = None,
) -> None:
    """Delete one verified visible-text occurrence."""

    replace_text(
        package,
        locator,
        text,
        "",
        occurrence=occurrence,
        tracked=tracked,
        author=author,
        date=date,
    )


def insert_paragraph(
    package: OpcPackage,
    locator: DocumentLocator | Mapping[str, Any],
    text: str,
    *,
    position: str = "after",
    style: str | None = None,
    tracked: bool = True,
    author: str = "Raven",
    date: datetime | None = None,
) -> None:
    """Insert a sibling paragraph before or after a located paragraph."""

    if position not in {"before", "after"}:
        raise RavenError(
            ErrorCode.INVALID_REQUEST,
            "Paragraph position must be 'before' or 'after'.",
            stage="edit",
        )
    resolved = resolve_locator(package, locator)
    record = resolved.record
    _assert_editable(record, 0, len(record.text))
    parent = record.element.getparent()
    if parent is None or parent.tag in _PROTECTED_CONTAINERS:
        raise RavenError(
            ErrorCode.PROTECTED_BOUNDARY,
            "A paragraph cannot be inserted at this structural boundary.",
            stage="edit",
        )
    paragraph = etree.Element(qn("p"))
    existing_properties = record.element.find("w:pPr", namespaces=NS)
    if existing_properties is not None and style is None:
        paragraph.append(copy.deepcopy(existing_properties))
    elif style is not None:
        properties = etree.SubElement(paragraph, qn("pPr"))
        style_element = etree.SubElement(properties, qn("pStyle"))
        style_element.set(qn("val"), style)
    run = _new_run(text)
    if tracked:
        revision = _revision_element(
            "ins",
            author,
            date or datetime.now(UTC),
            _next_revision_id(package),
        )
        revision.append(run)
        paragraph.append(revision)
    else:
        paragraph.append(run)
    target_index = parent.index(record.element) + (1 if position == "after" else 0)
    parent.insert(target_index, paragraph)
    package.set_xml(record.part_name, record.element.getroottree())


def set_alt_text(
    package: OpcPackage,
    locator: DocumentLocator | Mapping[str, Any],
    description: str,
    *,
    title: str | None = None,
) -> None:
    """Set accessible title and description on the first located drawing."""

    resolved = resolve_locator(package, locator)
    _assert_editable(resolved.record, 0, len(resolved.record.text))
    candidates = resolved.record.element.xpath(
        ".//wp:docPr | .//pic:cNvPr | .//v:shape",
        namespaces=NS,
    )
    if not candidates:
        raise RavenError(
            ErrorCode.ANCHOR_NOT_FOUND,
            "The located paragraph contains no drawing with alternative text.",
            stage="edit",
        )
    element = candidates[0]
    if not isinstance(element, etree._Element):
        raise RavenError(
            ErrorCode.INTERNAL_ERROR,
            "Unexpected drawing node.",
            stage="edit",
        )
    if _ancestor_has(element, set(_PROTECTED_CONTAINERS)):
        raise RavenError(
            ErrorCode.PROTECTED_BOUNDARY,
            "The drawing is inside protected markup.",
            stage="edit",
        )
    if element.tag == f"{{{V_NS}}}shape":
        element.set("alt", description)
        if title is not None:
            element.set("title", title)
    else:
        element.set("descr", description)
        if title is not None:
            element.set("title", title)
        elif "title" in element.attrib:
            del element.attrib["title"]
    package.set_xml(
        resolved.record.part_name,
        resolved.record.element.getroottree(),
    )


def _comments_part(package: OpcPackage) -> str:
    document_part = main_document_part(package)
    for relationship in package.relationships(document_part):
        if relationship.relationship_type == COMMENTS_REL and not relationship.external:
            target = package.relationship_target(relationship)
            if target is not None:
                return target
    return posixpath.join(posixpath.dirname(document_part), "comments.xml")


def add_comment(
    package: OpcPackage,
    locator: DocumentLocator | Mapping[str, Any],
    text: str,
    comment: str,
    *,
    author: str | None = "Raven",
    initials: str | None = None,
    date: datetime | None = None,
) -> int:
    """Anchor a Word comment to one exact visible-text occurrence."""

    value = _coerce_locator(locator).model_copy(update={"exact_text": text})
    resolved = resolve_locator(package, value)
    if resolved.start is None or resolved.end is None:
        raise RavenError(
            ErrorCode.ANCHOR_NOT_FOUND,
            "Comment anchor text was not found.",
            stage="edit",
        )
    _assert_editable(resolved.record, resolved.start, resolved.end)
    runs = _isolate_runs(resolved.record, resolved.start, resolved.end)

    comments_part = _comments_part(package)
    if package.has_part(comments_part):
        comments_root = package.read_xml(comments_part)
    else:
        comments_root = etree.Element(
            qn("comments"),
            nsmap={"w": W_NS, "r": R_NS},
        )
    ids = [
        int(comment_id_value)
        for element in comments_root.findall(".//w:comment", namespaces=NS)
        if (comment_id_value := element.get(qn("id"), "")).isdigit()
    ]
    comment_id = max(ids, default=-1) + 1
    timestamp_value = date or datetime.now(UTC)
    timestamp = (
        timestamp_value.astimezone(UTC)
        if timestamp_value.tzinfo
        else timestamp_value.replace(tzinfo=UTC)
    )

    comment_element = etree.SubElement(comments_root, qn("comment"))
    comment_element.set(qn("id"), str(comment_id))
    comment_element.set(qn("author"), author or "Raven")
    comment_element.set(qn("date"), timestamp.isoformat().replace("+00:00", "Z"))
    if initials is not None:
        comment_element.set(qn("initials"), initials)
    comment_paragraph = etree.SubElement(comment_element, qn("p"))
    comment_paragraph.append(_new_run(comment))

    paragraph = resolved.record.element
    start_index = paragraph.index(runs[0])
    end_index = paragraph.index(runs[-1])
    range_start = etree.Element(qn("commentRangeStart"))
    range_start.set(qn("id"), str(comment_id))
    paragraph.insert(start_index, range_start)
    range_end = etree.Element(qn("commentRangeEnd"))
    range_end.set(qn("id"), str(comment_id))
    paragraph.insert(end_index + 2, range_end)
    reference_run = etree.Element(qn("r"))
    properties = etree.SubElement(reference_run, qn("rPr"))
    reference_style = etree.SubElement(properties, qn("rStyle"))
    reference_style.set(qn("val"), "CommentReference")
    reference = etree.SubElement(reference_run, qn("commentReference"))
    reference.set(qn("id"), str(comment_id))
    paragraph.insert(end_index + 3, reference_run)

    package.set_xml(
        resolved.record.part_name,
        resolved.record.element.getroottree(),
    )
    package.set_xml(comments_part, comments_root)
    package.set_content_type_override(comments_part, COMMENTS_CONTENT_TYPE)
    package.add_relationship(
        main_document_part(package),
        comments_part,
        COMMENTS_REL,
    )
    return comment_id


def zotero_field_json(field: ComplexField) -> dict[str, Any] | None:
    """Decode the JSON object embedded in a Zotero field instruction."""

    start = field.instruction.find("{")
    if start < 0:
        return None
    try:
        value = json.loads(field.instruction[start:])
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


__all__ = [
    "A_NS",
    "COMMENTS_CONTENT_TYPE",
    "COMMENTS_REL",
    "MC_NS",
    "NS",
    "NSMAP",
    "PIC_NS",
    "R_NS",
    "V_NS",
    "W14_NS",
    "W15_NS",
    "WP_NS",
    "W_NS",
    "add_comment",
    "all_paragraphs",
    "delete_text",
    "discover_stories",
    "field_balance_errors",
    "insert_paragraph",
    "insert_text",
    "inspect_document",
    "main_document_part",
    "make_locator",
    "paragraph_hash",
    "paragraph_style",
    "paragraph_text",
    "parse_complex_fields",
    "qn",
    "replace_text",
    "resolve_locator",
    "set_alt_text",
    "story_paragraphs",
    "style_lookup",
    "zotero_field_json",
]
