"""OOXML complex-field parsing and safe field surgery."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from lxml import etree

from raven_mcp.errors import ErrorCode, RavenError
from raven_mcp.schemas import DocumentLocator, StoryKind

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
XML_NS = "http://www.w3.org/XML/1998/namespace"
W = f"{{{W_NS}}}"
XML_SPACE = f"{{{XML_NS}}}space"
NSMAP = {"w": W_NS}

BODY_PART = "word/document.xml"
FOOTNOTES_PART = "word/footnotes.xml"
ENDNOTES_PART = "word/endnotes.xml"
SUPPORTED_STORY_PARTS: dict[StoryKind, str] = {
    StoryKind.BODY: BODY_PART,
    StoryKind.FOOTNOTE: FOOTNOTES_PART,
    StoryKind.ENDNOTE: ENDNOTES_PART,
}

Position = Literal["start", "end", "before", "after"]


def qn(local_name: str) -> str:
    """Return a Clark-notation WordprocessingML name."""

    return f"{W}{local_name}"


def _ancestor(element: etree._Element, name: str) -> etree._Element | None:
    current: etree._Element | None = element
    tag = qn(name)
    while current is not None:
        if current.tag == tag:
            return current
        current = current.getparent()
    return None


def _field_type(element: etree._Element) -> str:
    return element.get(qn("fldCharType"), element.get("fldCharType", "")).lower()


def _token_text(element: etree._Element) -> str | None:
    if element.tag == qn("t"):
        return element.text or ""
    if element.tag == qn("tab"):
        return "\t"
    if element.tag in {qn("br"), qn("cr")}:
        return "\n"
    return None


def _in_deleted_content(element: etree._Element) -> bool:
    current = element.getparent()
    excluded = {qn("del"), qn("moveFrom")}
    while current is not None:
        if current.tag in excluded:
            return True
        current = current.getparent()
    return False


def _set_preserve_space(element: etree._Element, value: str) -> None:
    if value[:1].isspace() or value[-1:].isspace():
        element.set(XML_SPACE, "preserve")
    else:
        element.attrib.pop(XML_SPACE, None)


@dataclass(slots=True)
class FieldBoundary:
    """One begin, separator, or end marker and its containing nodes."""

    element: etree._Element
    run: etree._Element | None
    paragraph: etree._Element | None


@dataclass(slots=True)
class ComplexField:
    """A parsed OOXML complex field spanning any number of runs or paragraphs."""

    field_id: str
    story: StoryKind
    part: str
    instruction: str
    visible_text: str
    begin: FieldBoundary
    separator: FieldBoundary | None
    end: FieldBoundary
    instruction_nodes: tuple[etree._Element, ...] = ()
    result_nodes: tuple[etree._Element, ...] = ()
    runs: tuple[etree._Element, ...] = ()
    paragraphs: tuple[etree._Element, ...] = ()
    ordinal: int = 0
    depth: int = 0

    @property
    def is_zotero_citation(self) -> bool:
        normalized = " ".join(self.instruction.upper().split())
        return (
            "ZOTERO_ITEM" in normalized or "CSL_CITATION" in normalized
        ) and "ZOTERO_BIBL" not in normalized

    @property
    def is_zotero_bibliography(self) -> bool:
        return "ZOTERO_BIBL" in self.instruction.upper()

    def as_dict(self) -> dict[str, Any]:
        """Return the stable, serializable field metadata."""

        kind = (
            "citation"
            if self.is_zotero_citation
            else "bibliography"
            if self.is_zotero_bibliography
            else "field"
        )
        return {
            "field_id": self.field_id,
            "story": self.story.value,
            "part": self.part,
            "instruction": self.instruction,
            "visible_text": self.visible_text,
            "kind": kind,
            "ordinal": self.ordinal,
        }


@dataclass(slots=True)
class FieldScan:
    """Fields plus recoverable malformed-field warnings."""

    fields: list[ComplexField] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass(slots=True)
class _OpenField:
    begin: FieldBoundary
    ordinal: int
    depth: int
    separator: FieldBoundary | None = None
    instruction_nodes: list[etree._Element] = field(default_factory=list)
    result_nodes: list[etree._Element] = field(default_factory=list)
    runs: list[etree._Element] = field(default_factory=list)
    paragraphs: list[etree._Element] = field(default_factory=list)


def _append_identity(items: list[etree._Element], value: etree._Element | None) -> None:
    if value is not None and not any(item is value for item in items):
        items.append(value)


def _citation_id(instruction: str) -> str | None:
    normalized = instruction.upper()
    if "ZOTERO_ITEM" not in normalized and "CSL_CITATION" not in normalized:
        return None
    start = instruction.find("{")
    if start < 0:
        return None
    try:
        payload, _ = json.JSONDecoder().raw_decode(instruction[start:])
    except (json.JSONDecodeError, TypeError):
        match = re.search(r'"citationID"\s*:\s*"([^"]+)"', instruction)
        return match.group(1) if match else None
    if isinstance(payload, dict):
        value = payload.get("citationID")
        if isinstance(value, (str, int)):
            return str(value)
    return None


def _field_identifier(instruction: str, story: StoryKind, ordinal: int) -> str:
    citation_id = _citation_id(instruction)
    if citation_id:
        return citation_id
    if "ZOTERO_BIBL" in instruction.upper():
        return "bibliography"
    return f"{story.value}:{ordinal}"


def scan_complex_fields(
    root: etree._Element,
    *,
    part: str = BODY_PART,
    story: StoryKind = StoryKind.BODY,
) -> FieldScan:
    """Parse nested complex fields as one document-order stream."""

    result = FieldScan()
    stack: list[_OpenField] = []
    ordinal = 0

    for element in root.iter():
        run = _ancestor(element, "r")
        paragraph = _ancestor(element, "p")
        for opened in stack:
            _append_identity(opened.runs, run)
            _append_identity(opened.paragraphs, paragraph)

        if element.tag == qn("fldChar"):
            if _in_deleted_content(element):
                continue
            kind = _field_type(element)
            boundary = FieldBoundary(element, run, paragraph)
            if kind == "begin":
                ordinal += 1
                opened = _OpenField(boundary, ordinal, len(stack))
                _append_identity(opened.runs, run)
                _append_identity(opened.paragraphs, paragraph)
                stack.append(opened)
            elif kind == "separate":
                if not stack:
                    result.warnings.append(f"Ignored unmatched field separator in {part}.")
                elif stack[-1].separator is not None:
                    result.warnings.append(
                        f"Ignored duplicate field separator in {part}, field {stack[-1].ordinal}."
                    )
                else:
                    stack[-1].separator = boundary
            elif kind == "end":
                if not stack:
                    result.warnings.append(f"Ignored unmatched field end in {part}.")
                    continue
                opened = stack.pop()
                instruction = "".join(node.text or "" for node in opened.instruction_nodes)
                visible_text = "".join(
                    token
                    for node in opened.result_nodes
                    if (token := _token_text(node)) is not None
                )
                result.fields.append(
                    ComplexField(
                        field_id=_field_identifier(instruction, story, opened.ordinal),
                        story=story,
                        part=part,
                        instruction=instruction,
                        visible_text=visible_text,
                        begin=opened.begin,
                        separator=opened.separator,
                        end=boundary,
                        instruction_nodes=tuple(opened.instruction_nodes),
                        result_nodes=tuple(opened.result_nodes),
                        runs=tuple(opened.runs),
                        paragraphs=tuple(opened.paragraphs),
                        ordinal=opened.ordinal,
                        depth=opened.depth,
                    )
                )
            continue

        if element.tag == qn("instrText"):
            if _in_deleted_content(element):
                continue
            if stack and stack[-1].separator is None:
                stack[-1].instruction_nodes.append(element)
            continue

        token = _token_text(element)
        if token is not None and not _in_deleted_content(element):
            for opened in stack:
                if opened.separator is not None:
                    opened.result_nodes.append(element)

    for opened in stack:
        result.warnings.append(f"Ignored unclosed field in {part}, field {opened.ordinal}.")
    result.fields.sort(key=lambda item: item.ordinal)
    return result


def parse_complex_fields(
    root: etree._Element,
    *,
    part: str = BODY_PART,
    story: StoryKind = StoryKind.BODY,
    strict: bool = False,
) -> list[ComplexField]:
    """Return complex fields, optionally rejecting malformed boundaries."""

    scan = scan_complex_fields(root, part=part, story=story)
    if strict and scan.warnings:
        raise RavenError(
            ErrorCode.PROTECTED_BOUNDARY,
            scan.warnings[0],
            stage="citation.fields.parse",
            remediation="Repair or refresh malformed Word fields before editing citations.",
        )
    return scan.fields


def resolve_story_part(locator: DocumentLocator) -> tuple[StoryKind, str]:
    """Resolve a supported locator story to its OOXML part."""

    story = locator.story
    default = SUPPORTED_STORY_PARTS.get(story)
    if default is None:
        raise RavenError(
            ErrorCode.INVALID_REQUEST,
            f"Citations are not supported in the {story.value} story.",
            stage="citation.locate",
            remediation="Use the body, footnote, or endnote story.",
            locator=locator.model_dump(mode="json"),
        )
    if not locator.story_part:
        return story, default

    requested = locator.story_part.lstrip("/")
    if "/" not in requested:
        requested = f"word/{requested}"
    if requested != default:
        raise RavenError(
            ErrorCode.INVALID_REQUEST,
            f"Story part {locator.story_part!r} does not match {story.value}.",
            stage="citation.locate",
            remediation=f"Use {default!r} or omit story_part.",
            locator=locator.model_dump(mode="json"),
        )
    return story, default


def _paragraph_maps(
    root: etree._Element,
) -> tuple[
    list[etree._Element],
    dict[int, str],
    dict[int, list[tuple[int, int, etree._Element]]],
]:
    paragraphs = list(root.iter(qn("p")))
    texts: dict[int, list[str]] = {id(paragraph): [] for paragraph in paragraphs}
    segments: dict[int, list[tuple[int, int, etree._Element]]] = {
        id(paragraph): [] for paragraph in paragraphs
    }
    stack: list[bool] = []

    for element in root.iter():
        if element.tag == qn("fldChar"):
            if _in_deleted_content(element):
                continue
            kind = _field_type(element)
            if kind == "begin":
                stack.append(False)
            elif kind == "separate" and stack:
                stack[-1] = True
            elif kind == "end" and stack:
                stack.pop()
            continue
        if element.tag == qn("instrText"):
            continue
        token = _token_text(element)
        if token is None or _in_deleted_content(element) or (stack and not all(stack)):
            continue
        paragraph = _ancestor(element, "p")
        if paragraph is None:
            continue
        key = id(paragraph)
        start = sum(len(value) for value in texts[key])
        texts[key].append(token)
        segments[key].append((start, start + len(token), element))

    return (
        paragraphs,
        {key: "".join(values) for key, values in texts.items()},
        segments,
    )


def paragraph_text(root: etree._Element, paragraph: etree._Element) -> str:
    """Return visible paragraph text with field instructions suppressed."""

    _, texts, _ = _paragraph_maps(root)
    return texts.get(id(paragraph), "")


def _nth_span(text: str, needle: str, occurrence: int) -> tuple[int, int] | None:
    if not needle:
        return None
    cursor = 0
    for _ in range(occurrence):
        found = text.find(needle, cursor)
        if found < 0:
            return None
        cursor = found + len(needle)
    return found, found + len(needle)


def _locate_paragraph(
    root: etree._Element,
    locator: DocumentLocator,
) -> tuple[
    etree._Element,
    str,
    list[tuple[int, int, etree._Element]],
]:
    paragraphs, texts, segments = _paragraph_maps(root)
    if locator.paragraph_index >= len(paragraphs):
        raise RavenError(
            ErrorCode.ANCHOR_NOT_FOUND,
            f"Paragraph {locator.paragraph_index} does not exist.",
            stage="citation.locate",
            remediation="Refresh the document view and use a current paragraph locator.",
            locator=locator.model_dump(mode="json"),
        )
    paragraph = paragraphs[locator.paragraph_index]
    text = texts[id(paragraph)]
    if locator.paragraph_hash:
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if locator.paragraph_hash.lower() not in {digest, digest[:16]}:
            raise RavenError(
                ErrorCode.STALE_REVISION,
                "The target paragraph no longer matches its locator hash.",
                stage="citation.locate",
                remediation="Refresh the document view and retry with a current locator.",
                locator=locator.model_dump(mode="json"),
            )
    return paragraph, text, segments[id(paragraph)]


def _anchor_span(
    text: str,
    locator: DocumentLocator,
    position: Position,
) -> tuple[int, int, int]:
    if position not in {"start", "end", "before", "after"}:
        raise RavenError(
            ErrorCode.INVALID_REQUEST,
            f"Unsupported citation insertion position: {position!r}.",
            stage="citation.locate",
            remediation="Use start, end, before, or after.",
            locator=locator.model_dump(mode="json"),
        )
    if position == "start":
        return 0, 0, 0
    if position == "end":
        return len(text), len(text), len(text)
    if not locator.exact_text:
        raise RavenError(
            ErrorCode.INVALID_REQUEST,
            f"exact_text is required for position={position!r}.",
            stage="citation.locate",
            remediation="Provide exact_text or use start/end.",
            locator=locator.model_dump(mode="json"),
        )
    span = _nth_span(text, locator.exact_text, locator.occurrence)
    if span is None:
        raise RavenError(
            ErrorCode.ANCHOR_NOT_FOUND,
            f"Text occurrence {locator.occurrence} was not found in the paragraph.",
            stage="citation.locate",
            remediation="Use exact visible paragraph text and a valid occurrence.",
            locator=locator.model_dump(mode="json"),
        )
    start, end = span
    if locator.prefix is not None and not text[:start].endswith(locator.prefix):
        raise RavenError(
            ErrorCode.ANCHOR_NOT_FOUND,
            "The anchor prefix does not match.",
            stage="citation.locate",
            remediation="Refresh the locator context and retry.",
            locator=locator.model_dump(mode="json"),
        )
    if locator.suffix is not None and not text[end:].startswith(locator.suffix):
        raise RavenError(
            ErrorCode.ANCHOR_NOT_FOUND,
            "The anchor suffix does not match.",
            stage="citation.locate",
            remediation="Refresh the locator context and retry.",
            locator=locator.model_dump(mode="json"),
        )
    return start, end, start if position == "before" else end


def _field_spans(
    fields: Iterable[ComplexField],
    paragraph: etree._Element,
    segments: Sequence[tuple[int, int, etree._Element]],
) -> list[tuple[int, int, ComplexField]]:
    by_node = {id(node): (start, end) for start, end, node in segments}
    spans: list[tuple[int, int, ComplexField]] = []
    for item in fields:
        offsets = [
            by_node[id(node)]
            for node in item.result_nodes
            if id(node) in by_node and _ancestor(node, "p") is paragraph
        ]
        if offsets:
            spans.append(
                (
                    min(start for start, _ in offsets),
                    max(end for _, end in offsets),
                    item,
                )
            )
    return spans


def _protected_error(locator: DocumentLocator, detail: str) -> RavenError:
    return RavenError(
        ErrorCode.PROTECTED_BOUNDARY,
        detail,
        stage="citation.fields.edit",
        remediation="Place the anchor wholly before or after the managed field.",
        locator=locator.model_dump(mode="json"),
    )


def _new_run_with_text(text: str) -> etree._Element:
    run = etree.Element(qn("r"))
    text_node = etree.SubElement(run, qn("t"))
    text_node.text = text
    _set_preserve_space(text_node, text)
    return run


def _instruction_chunks(instruction: str, size: int = 240) -> Iterable[str]:
    if not instruction:
        yield ""
        return
    for offset in range(0, len(instruction), size):
        yield instruction[offset : offset + size]


def build_complex_field_runs(
    instruction: str,
    visible_text: str,
    *,
    dirty: bool = True,
) -> list[etree._Element]:
    """Build a standards-compliant begin/instruction/separator/result/end field."""

    begin_run = etree.Element(qn("r"))
    begin = etree.SubElement(begin_run, qn("fldChar"))
    begin.set(qn("fldCharType"), "begin")
    if dirty:
        begin.set(qn("dirty"), "true")

    runs = [begin_run]
    for chunk in _instruction_chunks(instruction):
        instruction_run = etree.Element(qn("r"))
        instruction_node = etree.SubElement(instruction_run, qn("instrText"))
        instruction_node.set(XML_SPACE, "preserve")
        instruction_node.text = chunk
        runs.append(instruction_run)

    separator_run = etree.Element(qn("r"))
    separator = etree.SubElement(separator_run, qn("fldChar"))
    separator.set(qn("fldCharType"), "separate")
    runs.append(separator_run)
    runs.append(_new_run_with_text(visible_text))

    end_run = etree.Element(qn("r"))
    end = etree.SubElement(end_run, qn("fldChar"))
    end.set(qn("fldCharType"), "end")
    runs.append(end_run)
    return runs


def _in_revision(element: etree._Element) -> bool:
    current = element.getparent()
    revision_tags = {qn("ins"), qn("del"), qn("moveFrom"), qn("moveTo")}
    while current is not None:
        if current.tag in revision_tags:
            return True
        if current.tag == qn("p"):
            return False
        current = current.getparent()
    return False


def _copy_run_shell(run: etree._Element) -> etree._Element:
    clone = etree.Element(run.tag, dict(run.attrib), nsmap=run.nsmap)
    run_properties = run.find(qn("rPr"))
    if run_properties is not None:
        clone.append(copy.deepcopy(run_properties))
    return clone


def _has_run_content(run: etree._Element) -> bool:
    return any(child.tag != qn("rPr") for child in run)


def _split_run_and_insert(
    node: etree._Element,
    node_offset: int,
    new_runs: Sequence[etree._Element],
) -> None:
    run = _ancestor(node, "r")
    if run is None or run.getparent() is None:
        raise RavenError(
            ErrorCode.ANCHOR_NOT_FOUND,
            "The text anchor is not contained in an editable run.",
            stage="citation.fields.insert",
        )
    if _in_revision(run):
        raise RavenError(
            ErrorCode.UNSUPPORTED_REVISION,
            "The citation anchor is inside tracked revision markup.",
            stage="citation.fields.insert",
            remediation="Accept or reject the revision, then retry.",
        )
    parent = run.getparent()
    if parent.tag != qn("p"):
        raise RavenError(
            ErrorCode.PROTECTED_BOUNDARY,
            "The citation anchor is inside a structured inline container.",
            stage="citation.fields.insert",
            remediation="Use an anchor in ordinary paragraph text.",
        )

    before = _copy_run_shell(run)
    after = _copy_run_shell(run)
    seen = False
    for child in run:
        if child.tag == qn("rPr"):
            continue
        if child is node:
            seen = True
            token = _token_text(child)
            if token is None:
                if node_offset == 0:
                    after.append(copy.deepcopy(child))
                else:
                    before.append(copy.deepcopy(child))
            else:
                left, right = token[:node_offset], token[node_offset:]
                if left:
                    left_node = copy.deepcopy(child)
                    left_node.text = left
                    _set_preserve_space(left_node, left)
                    before.append(left_node)
                if right:
                    right_node = copy.deepcopy(child)
                    right_node.text = right
                    _set_preserve_space(right_node, right)
                    after.append(right_node)
        elif not seen:
            before.append(copy.deepcopy(child))
        else:
            after.append(copy.deepcopy(child))

    index = parent.index(run)
    parent.remove(run)
    additions: list[etree._Element] = []
    if _has_run_content(before):
        additions.append(before)
    additions.extend(new_runs)
    if _has_run_content(after):
        additions.append(after)
    for offset, addition in enumerate(additions):
        parent.insert(index + offset, addition)


def _insert_next_to_boundary(
    boundary: FieldBoundary,
    new_runs: Sequence[etree._Element],
    *,
    after: bool,
) -> None:
    run = boundary.run
    paragraph = boundary.paragraph
    if run is None or paragraph is None or run.getparent() is not paragraph:
        raise RavenError(
            ErrorCode.PROTECTED_BOUNDARY,
            "The managed field boundary is inside an unsupported container.",
            stage="citation.fields.insert",
            remediation="Refresh the field in Word before editing near it.",
        )
    index = paragraph.index(run) + int(after)
    for offset, addition in enumerate(new_runs):
        paragraph.insert(index + offset, addition)


def insert_complex_field(
    root: etree._Element,
    locator: DocumentLocator,
    instruction: str,
    visible_text: str,
    *,
    position: Position = "after",
) -> ComplexField:
    """Insert a field at an exact, boundary-safe visible-text location."""

    story, part = resolve_story_part(locator)
    paragraph, text, segments = _locate_paragraph(root, locator)
    anchor_start, anchor_end, offset = _anchor_span(text, locator, position)
    fields = parse_complex_fields(root, part=part, story=story)
    spans = _field_spans(fields, paragraph, segments)

    boundary_target: tuple[FieldBoundary, bool] | None = None
    for field_start, field_end, item in spans:
        overlaps = anchor_start < field_end and anchor_end > field_start
        exact_field = anchor_start == field_start and anchor_end == field_end
        if overlaps and not exact_field:
            raise _protected_error(locator, "The text anchor overlaps a managed field result.")
        if field_start < offset < field_end:
            raise _protected_error(locator, "The insertion point is inside a managed field.")
        if offset == field_start:
            boundary_target = (item.begin, False)
        elif offset == field_end:
            boundary_target = (item.end, True)

    new_runs = build_complex_field_runs(instruction, visible_text)
    if boundary_target is not None:
        boundary, after_boundary = boundary_target
        _insert_next_to_boundary(boundary, new_runs, after=after_boundary)
    elif not segments:
        index = 1 if len(paragraph) and paragraph[0].tag == qn("pPr") else 0
        for addition in reversed(new_runs):
            paragraph.insert(index, addition)
    else:
        target: tuple[int, int, etree._Element] | None = None
        for segment in segments:
            start, end, _ = segment
            if start <= offset <= end:
                target = segment
                if start < offset < end:
                    break
                if offset == start:
                    break
        if target is None:
            target = segments[-1]
        start, end, node = target
        node_offset = max(0, min(offset - start, end - start))
        _split_run_and_insert(node, node_offset, new_runs)

    reparsed = parse_complex_fields(root, part=part, story=story)
    inserted = [
        item
        for item in reparsed
        if item.instruction == instruction and item.visible_text == visible_text
    ]
    if not inserted:
        raise RavenError(
            ErrorCode.VALIDATION_FAILED,
            "The inserted citation field could not be validated.",
            stage="citation.fields.insert",
        )
    return inserted[-1]


def _editable_field(field: ComplexField) -> tuple[etree._Element, int]:
    if field.separator is None:
        raise RavenError(
            ErrorCode.PROTECTED_BOUNDARY,
            f"Field {field.field_id!r} has no result separator.",
            stage="citation.fields.edit",
            remediation="Refresh or repair the field in Word before editing it.",
        )
    if field.depth:
        raise RavenError(
            ErrorCode.PROTECTED_BOUNDARY,
            f"Nested field {field.field_id!r} cannot be rewritten safely.",
            stage="citation.fields.edit",
            remediation="Unnest or refresh the field in Word before editing it.",
        )
    paragraph = field.begin.paragraph
    begin_run = field.begin.run
    if paragraph is None or begin_run is None or begin_run.getparent() is not paragraph:
        raise RavenError(
            ErrorCode.PROTECTED_BOUNDARY,
            f"Field {field.field_id!r} spans unsupported containers.",
            stage="citation.fields.edit",
            remediation="Refresh the field in Word so its runs share one paragraph.",
        )
    if any(run.getparent() is not paragraph for run in field.runs):
        raise RavenError(
            ErrorCode.PROTECTED_BOUNDARY,
            f"Field {field.field_id!r} crosses a paragraph boundary.",
            stage="citation.fields.edit",
            remediation="Refresh the field in Word before editing it.",
        )
    field_run_ids = {id(run) for run in field.runs}
    if id(begin_run) not in field_run_ids or field.end.run is None:
        raise RavenError(
            ErrorCode.PROTECTED_BOUNDARY,
            f"Field {field.field_id!r} has incomplete run boundaries.",
            stage="citation.fields.edit",
        )
    for boundary in (field.begin, field.separator, field.end):
        if boundary is None or boundary.run is None:
            continue
        unrelated = [
            child
            for child in boundary.run
            if child.tag != qn("rPr") and child is not boundary.element
        ]
        if unrelated:
            raise RavenError(
                ErrorCode.PROTECTED_BOUNDARY,
                f"Field {field.field_id!r} shares a boundary run with other content.",
                stage="citation.fields.edit",
                remediation="Refresh the field in Word before editing it.",
            )
    return paragraph, paragraph.index(begin_run)


def _remove_field_runs(field: ComplexField) -> tuple[etree._Element, int]:
    paragraph, index = _editable_field(field)
    for run in field.runs:
        if run.getparent() is paragraph:
            paragraph.remove(run)
    return paragraph, index


def replace_complex_field(
    field: ComplexField,
    *,
    instruction: str | None = None,
    visible_text: str | None = None,
) -> None:
    """Replace field code and/or result while preserving surrounding content."""

    replacement_instruction = field.instruction if instruction is None else instruction
    replacement_text = field.visible_text if visible_text is None else visible_text
    paragraph, index = _remove_field_runs(field)
    for offset, run in enumerate(
        build_complex_field_runs(replacement_instruction, replacement_text)
    ):
        paragraph.insert(index + offset, run)


def replace_field_instruction(field: ComplexField, instruction: str) -> None:
    """Replace a field instruction and mark the field dirty."""

    replace_complex_field(field, instruction=instruction)


def replace_field_result(field: ComplexField, visible_text: str) -> None:
    """Replace a field result and mark the field dirty."""

    replace_complex_field(field, visible_text=visible_text)


def remove_complex_field(field: ComplexField, *, keep_visible: bool = False) -> None:
    """Remove an entire field, optionally retaining its visible result as text."""

    paragraph, index = _remove_field_runs(field)
    if keep_visible and field.visible_text:
        paragraph.insert(index, _new_run_with_text(field.visible_text))


def append_field_paragraphs(
    root: etree._Element,
    locator: DocumentLocator,
    instruction: str,
    visible_text: str,
    *,
    heading: str | None = None,
) -> ComplexField:
    """Append standalone heading and field paragraphs after a located paragraph."""

    story, part = resolve_story_part(locator)
    target, target_text, _ = _locate_paragraph(root, locator)
    if locator.exact_text is not None:
        _anchor_span(target_text, locator, "after")
    parent = target.getparent()
    if parent is None or parent.tag in {
        qn("ins"),
        qn("del"),
        qn("moveFrom"),
        qn("moveTo"),
    }:
        raise RavenError(
            ErrorCode.PROTECTED_BOUNDARY,
            "The target paragraph is inside a protected structural boundary.",
            stage="citation.fields.insert",
            remediation="Use an ordinary, untracked paragraph as the bibliography anchor.",
        )
    index = parent.index(target) + 1
    if heading:
        heading_paragraph = etree.Element(qn("p"))
        properties = etree.SubElement(heading_paragraph, qn("pPr"))
        style = etree.SubElement(properties, qn("pStyle"))
        style.set(qn("val"), "Heading1")
        heading_paragraph.append(_new_run_with_text(heading))
        parent.insert(index, heading_paragraph)
        index += 1

    field_paragraph = etree.Element(qn("p"))
    for run in build_complex_field_runs(instruction, visible_text):
        field_paragraph.append(run)
    parent.insert(index, field_paragraph)

    fields = parse_complex_fields(root, part=part, story=story)
    for item in reversed(fields):
        if item.instruction == instruction and item.begin.paragraph is field_paragraph:
            return item
    raise RavenError(
        ErrorCode.VALIDATION_FAILED,
        "The bibliography field could not be validated after insertion.",
        stage="citation.fields.insert",
    )


def find_unique_field(
    fields: Iterable[ComplexField],
    field_id: str,
    *,
    citations_only: bool = False,
) -> ComplexField:
    """Resolve exactly one field by ID."""

    matches = [
        item
        for item in fields
        if item.field_id == field_id and (not citations_only or item.is_zotero_citation)
    ]
    if not matches:
        raise RavenError(
            ErrorCode.REFERENCE_UNRESOLVED,
            f"Citation field {field_id!r} was not found.",
            stage="citation.fields.resolve",
            remediation="Call list_citations() and retry with a current citation ID.",
        )
    if len(matches) > 1:
        raise RavenError(
            ErrorCode.ANCHOR_AMBIGUOUS,
            f"Citation ID {field_id!r} occurs {len(matches)} times.",
            stage="citation.fields.resolve",
            remediation="Refresh duplicate Zotero fields in Word before editing.",
        )
    return matches[0]
