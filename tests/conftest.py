"""Deterministic, hand-built Transitional OOXML fixtures for Raven tests."""

from __future__ import annotations

import io
import json
import zipfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import pytest
from lxml import etree

from raven_mcp.config import Settings

CONTENT_TYPES_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
OFFICE_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
WP_NS = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
PIC_NS = "http://schemas.openxmlformats.org/drawingml/2006/picture"
CP_NS = "http://schemas.openxmlformats.org/package/2006/metadata/core-properties"
DC_NS = "http://purl.org/dc/elements/1.1/"
CUSTOM_NS = "http://schemas.openxmlformats.org/officeDocument/2006/custom-properties"
VT_NS = "http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes"

DOCUMENT_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
STYLES_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"
CORE_TYPE = "application/vnd.openxmlformats-package.core-properties+xml"
CUSTOM_TYPE = "application/vnd.openxmlformats-officedocument.custom-properties+xml"
COMMENTS_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.comments+xml"
FOOTNOTES_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.footnotes+xml"
ENDNOTES_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.endnotes+xml"

OFFICE_DOCUMENT_REL = f"{OFFICE_REL_NS}/officeDocument"
CORE_REL = "http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties"
CUSTOM_REL = f"{OFFICE_REL_NS}/custom-properties"
STYLES_REL = f"{OFFICE_REL_NS}/styles"
COMMENTS_REL = f"{OFFICE_REL_NS}/comments"
FOOTNOTES_REL = f"{OFFICE_REL_NS}/footnotes"
ENDNOTES_REL = f"{OFFICE_REL_NS}/endnotes"
HYPERLINK_REL = f"{OFFICE_REL_NS}/hyperlink"
IMAGE_REL = f"{OFFICE_REL_NS}/image"

DEFAULT_CITATION_ID = "citation-split-1"
DEFAULT_ITEM_KEY = "ABCD2345"


def qn(namespace: str, local_name: str) -> str:
    return f"{{{namespace}}}{local_name}"


def _text_run(value: str) -> etree._Element:
    run = etree.Element(qn(W_NS, "r"))
    text = etree.SubElement(run, qn(W_NS, "t"))
    text.text = value
    if value[:1].isspace() or value[-1:].isspace():
        text.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
    return run


def _paragraph(value: str, *, style: str | None = None) -> etree._Element:
    paragraph = etree.Element(qn(W_NS, "p"))
    if style is not None:
        properties = etree.SubElement(paragraph, qn(W_NS, "pPr"))
        style_node = etree.SubElement(properties, qn(W_NS, "pStyle"))
        style_node.set(qn(W_NS, "val"), style)
    paragraph.append(_text_run(value))
    return paragraph


def _field_runs(
    instruction: str,
    visible: str,
    *,
    split_instruction: bool = False,
) -> list[etree._Element]:
    begin_run = etree.Element(qn(W_NS, "r"))
    begin = etree.SubElement(begin_run, qn(W_NS, "fldChar"))
    begin.set(qn(W_NS, "fldCharType"), "begin")
    begin.set(qn(W_NS, "dirty"), "true")
    chunks = (
        [instruction[: len(instruction) // 2], instruction[len(instruction) // 2 :]]
        if split_instruction
        else [instruction]
    )
    runs = [begin_run]
    for chunk in chunks:
        run = etree.Element(qn(W_NS, "r"))
        node = etree.SubElement(run, qn(W_NS, "instrText"))
        node.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
        node.text = chunk
        runs.append(run)
    separator_run = etree.Element(qn(W_NS, "r"))
    separator = etree.SubElement(separator_run, qn(W_NS, "fldChar"))
    separator.set(qn(W_NS, "fldCharType"), "separate")
    end_run = etree.Element(qn(W_NS, "r"))
    end = etree.SubElement(end_run, qn(W_NS, "fldChar"))
    end.set(qn(W_NS, "fldCharType"), "end")
    return [*runs, separator_run, _text_run(visible), end_run]


def _citation_instruction() -> str:
    payload = {
        "citationID": DEFAULT_CITATION_ID,
        "citationItems": [
            {
                "id": DEFAULT_ITEM_KEY,
                "uris": [f"http://zotero.org/users/1/items/{DEFAULT_ITEM_KEY}"],
                "itemData": {"id": DEFAULT_ITEM_KEY, "title": "Café research"},
            }
        ],
        "properties": {
            "formattedCitation": "(García, 2024)",
            "plainCitation": "(García, 2024)",
            "noteIndex": 0,
        },
    }
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return f" ADDIN ZOTERO_ITEM CSL_CITATION {encoded} "


def _bibliography_instruction(*, legacy: bool) -> str:
    payload = json.dumps(
        {"uncited": [], "omitted": [], "custom": []},
        separators=(",", ":"),
    )
    suffix = " CSL_BIBLIOGRAPHY " if legacy else " "
    return f" ADDIN ZOTERO_BIBL {payload}{suffix}"


def _utf16_chunks(value: str, limit: int = 255) -> list[str]:
    chunks: list[str] = []
    current = ""
    current_units = 0
    for character in value:
        units = 2 if ord(character) > 0xFFFF else 1
        if current and current_units + units > limit:
            chunks.append(current)
            current = ""
            current_units = 0
        current += character
        current_units += units
    if current or not chunks:
        chunks.append(current)
    return chunks


def _xml_bytes(root: etree._Element) -> bytes:
    return etree.tostring(
        root,
        encoding="UTF-8",
        xml_declaration=True,
        standalone=True,
    )


def _relationships(
    relationships: Sequence[tuple[str, str, str, str | None]],
) -> etree._Element:
    root = etree.Element(qn(REL_NS, "Relationships"), nsmap={None: REL_NS})
    for relationship_id, relationship_type, target, target_mode in relationships:
        node = etree.SubElement(root, qn(REL_NS, "Relationship"))
        node.set("Id", relationship_id)
        node.set("Type", relationship_type)
        node.set("Target", target)
        if target_mode is not None:
            node.set("TargetMode", target_mode)
    return root


@dataclass(frozen=True, slots=True)
class DocxSpec:
    """Options for a minimal, deterministic DOCX package."""

    paragraphs: tuple[str, ...] = ("Introduction", "Alpha beta alpha.")
    title: str = "Raven fixture"
    table: bool = False
    notes: bool = False
    comments_revisions: bool = False
    split_zotero: bool = False
    unicode_long_prefs: bool = False
    malformed_field: Literal["unclosed", "unmatched-end"] | None = None
    legacy_bibliography: bool = False
    drawing: bool = False
    external_relationship: bool = False
    traversal_member: str | None = None
    doctype: bool = False
    macro: bool = False
    extra_parts: Mapping[str, bytes] | None = None
    archive_comment: bytes = b"raven-fixture"


class DocxFactory:
    """Build fixture packages without relying on Word or python-docx."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def __call__(self, name: str = "fixture.docx", spec: DocxSpec | None = None) -> Path:
        path = self.directory / name
        path.write_bytes(self.build(spec or DocxSpec()))
        return path

    def build(self, spec: DocxSpec) -> bytes:
        parts = self._parts(spec)
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            archive.comment = spec.archive_comment
            for name, data in parts.items():
                info = zipfile.ZipInfo(name, date_time=(2024, 1, 2, 3, 4, 6))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o600 << 16
                archive.writestr(info, data)
            if spec.traversal_member is not None:
                archive.writestr(spec.traversal_member, b"unsafe")
        return output.getvalue()

    def _parts(self, spec: DocxSpec) -> dict[str, bytes]:
        document, document_relationships = self._document(spec)
        parts: dict[str, bytes] = {
            "word/document.xml": _xml_bytes(document),
            "word/styles.xml": _xml_bytes(self._styles()),
            "word/_rels/document.xml.rels": _xml_bytes(_relationships(document_relationships)),
            "docProps/core.xml": _xml_bytes(self._core(spec.title)),
        }

        root_relationships = [
            ("rId1", OFFICE_DOCUMENT_REL, "word/document.xml", None),
            ("rId2", CORE_REL, "docProps/core.xml", None),
        ]
        overrides = [
            ("/word/document.xml", DOCUMENT_TYPE),
            ("/word/styles.xml", STYLES_TYPE),
            ("/docProps/core.xml", CORE_TYPE),
        ]

        if spec.notes:
            parts["word/footnotes.xml"] = _xml_bytes(self._notes("footnote"))
            parts["word/endnotes.xml"] = _xml_bytes(self._notes("endnote"))
            overrides.extend(
                [
                    ("/word/footnotes.xml", FOOTNOTES_TYPE),
                    ("/word/endnotes.xml", ENDNOTES_TYPE),
                ]
            )
        if spec.comments_revisions:
            parts["word/comments.xml"] = _xml_bytes(self._comments())
            overrides.append(("/word/comments.xml", COMMENTS_TYPE))
        if spec.unicode_long_prefs:
            parts["docProps/custom.xml"] = _xml_bytes(self._custom_properties())
            overrides.append(("/docProps/custom.xml", CUSTOM_TYPE))
            root_relationships.append(("rId3", CUSTOM_REL, "docProps/custom.xml", None))
        if spec.macro:
            parts["word/vbaProject.bin"] = b"not-a-real-macro"
        if spec.drawing:
            parts["word/media/pixel.png"] = bytes.fromhex(
                "89504e470d0a1a0a0000000d494844520000000100000001"
                "08060000001f15c4890000000d4944415408d763f8cfc0f0"
                "1f00050001ff89993d1d0000000049454e44ae426082"
            )
        if spec.extra_parts:
            parts.update(spec.extra_parts)
        if spec.doctype:
            parts["word/document.xml"] = (
                b'<?xml version="1.0"?><!DOCTYPE w:document [<!ENTITY x "boom">]>'
                + parts["word/document.xml"].split(b"?>", maxsplit=1)[-1]
            )

        parts["_rels/.rels"] = _xml_bytes(_relationships(root_relationships))
        parts["[Content_Types].xml"] = _xml_bytes(
            self._content_types(
                overrides,
                macro=spec.macro,
                drawing=spec.drawing,
            )
        )
        return {
            "[Content_Types].xml": parts.pop("[Content_Types].xml"),
            "_rels/.rels": parts.pop("_rels/.rels"),
            **parts,
        }

    def _document(
        self,
        spec: DocxSpec,
    ) -> tuple[etree._Element, list[tuple[str, str, str, str | None]]]:
        root = etree.Element(
            qn(W_NS, "document"),
            nsmap={
                "w": W_NS,
                "r": OFFICE_REL_NS,
                "wp": WP_NS,
                "a": A_NS,
                "pic": PIC_NS,
            },
        )
        body = etree.SubElement(root, qn(W_NS, "body"))
        for index, value in enumerate(spec.paragraphs):
            body.append(_paragraph(value, style="Heading1" if index == 0 else None))

        relationships: list[tuple[str, str, str, str | None]] = [
            ("rId1", STYLES_REL, "styles.xml", None)
        ]
        next_id = 2

        if spec.table:
            table = etree.SubElement(body, qn(W_NS, "tbl"))
            row = etree.SubElement(table, qn(W_NS, "tr"))
            for value in ("Cell one", "Cell two"):
                cell = etree.SubElement(row, qn(W_NS, "tc"))
                cell.append(_paragraph(value))

        if spec.notes:
            relationships.extend(
                [
                    (f"rId{next_id}", FOOTNOTES_REL, "footnotes.xml", None),
                    (f"rId{next_id + 1}", ENDNOTES_REL, "endnotes.xml", None),
                ]
            )
            next_id += 2

        if spec.comments_revisions:
            paragraph = etree.SubElement(body, qn(W_NS, "p"))
            start = etree.SubElement(paragraph, qn(W_NS, "commentRangeStart"))
            start.set(qn(W_NS, "id"), "0")
            paragraph.append(_text_run("Reviewed"))
            end = etree.SubElement(paragraph, qn(W_NS, "commentRangeEnd"))
            end.set(qn(W_NS, "id"), "0")
            reference_run = etree.SubElement(paragraph, qn(W_NS, "r"))
            reference = etree.SubElement(reference_run, qn(W_NS, "commentReference"))
            reference.set(qn(W_NS, "id"), "0")
            insertion = etree.SubElement(paragraph, qn(W_NS, "ins"))
            insertion.set(qn(W_NS, "id"), "7")
            insertion.set(qn(W_NS, "author"), "Reviewer")
            insertion.append(_text_run(" inserted"))
            deletion = etree.SubElement(paragraph, qn(W_NS, "del"))
            deletion.set(qn(W_NS, "id"), "8")
            deletion.set(qn(W_NS, "author"), "Reviewer")
            deleted_run = etree.SubElement(deletion, qn(W_NS, "r"))
            deleted_text = etree.SubElement(deleted_run, qn(W_NS, "delText"))
            deleted_text.text = " deleted"
            relationships.append((f"rId{next_id}", COMMENTS_REL, "comments.xml", None))
            next_id += 1

        if spec.split_zotero:
            paragraph = etree.SubElement(body, qn(W_NS, "p"))
            for run in _field_runs(
                _citation_instruction(),
                "(García, 2024)",
                split_instruction=True,
            ):
                paragraph.append(run)

        if spec.malformed_field is not None:
            paragraph = etree.SubElement(body, qn(W_NS, "p"))
            marker_run = etree.SubElement(paragraph, qn(W_NS, "r"))
            marker = etree.SubElement(marker_run, qn(W_NS, "fldChar"))
            marker.set(
                qn(W_NS, "fldCharType"),
                "begin" if spec.malformed_field == "unclosed" else "end",
            )
            if spec.malformed_field == "unclosed":
                instruction_run = etree.SubElement(paragraph, qn(W_NS, "r"))
                instruction = etree.SubElement(
                    instruction_run,
                    qn(W_NS, "instrText"),
                )
                instruction.text = " ADDIN ZOTERO_ITEM {bad"

        if spec.legacy_bibliography:
            paragraph = etree.SubElement(body, qn(W_NS, "p"))
            for run in _field_runs(
                _bibliography_instruction(legacy=True),
                "Legacy bibliography",
            ):
                paragraph.append(run)

        if spec.unicode_long_prefs:
            body.append(_paragraph("naïve café — 東京 😀"))

        if spec.drawing:
            paragraph = etree.SubElement(body, qn(W_NS, "p"))
            run = etree.SubElement(paragraph, qn(W_NS, "r"))
            drawing = etree.SubElement(run, qn(W_NS, "drawing"))
            inline = etree.SubElement(drawing, qn(WP_NS, "inline"))
            for name in ("distT", "distB", "distL", "distR"):
                inline.set(name, "0")
            extent = etree.SubElement(inline, qn(WP_NS, "extent"))
            extent.set("cx", "9525")
            extent.set("cy", "9525")
            effect = etree.SubElement(inline, qn(WP_NS, "effectExtent"))
            for name in ("l", "t", "r", "b"):
                effect.set(name, "0")
            properties = etree.SubElement(inline, qn(WP_NS, "docPr"))
            properties.set("id", "1")
            properties.set("name", "Figure 1")
            properties.set("descr", "Old description")
            frame = etree.SubElement(inline, qn(WP_NS, "cNvGraphicFramePr"))
            etree.SubElement(frame, qn(A_NS, "graphicFrameLocks")).set(
                "noChangeAspect",
                "1",
            )
            graphic = etree.SubElement(inline, qn(A_NS, "graphic"))
            graphic_data = etree.SubElement(graphic, qn(A_NS, "graphicData"))
            graphic_data.set(
                "uri",
                "http://schemas.openxmlformats.org/drawingml/2006/picture",
            )
            picture = etree.SubElement(graphic_data, qn(PIC_NS, "pic"))
            non_visual = etree.SubElement(picture, qn(PIC_NS, "nvPicPr"))
            picture_properties = etree.SubElement(
                non_visual,
                qn(PIC_NS, "cNvPr"),
            )
            picture_properties.set("id", "0")
            picture_properties.set("name", "pixel.png")
            etree.SubElement(non_visual, qn(PIC_NS, "cNvPicPr"))
            fill = etree.SubElement(picture, qn(PIC_NS, "blipFill"))
            blip = etree.SubElement(fill, qn(A_NS, "blip"))
            blip.set(qn(OFFICE_REL_NS, "embed"), f"rId{next_id}")
            stretch = etree.SubElement(fill, qn(A_NS, "stretch"))
            etree.SubElement(stretch, qn(A_NS, "fillRect"))
            shape = etree.SubElement(picture, qn(PIC_NS, "spPr"))
            transform = etree.SubElement(shape, qn(A_NS, "xfrm"))
            offset = etree.SubElement(transform, qn(A_NS, "off"))
            offset.set("x", "0")
            offset.set("y", "0")
            shape_extent = etree.SubElement(transform, qn(A_NS, "ext"))
            shape_extent.set("cx", "9525")
            shape_extent.set("cy", "9525")
            geometry = etree.SubElement(shape, qn(A_NS, "prstGeom"))
            geometry.set("prst", "rect")
            etree.SubElement(geometry, qn(A_NS, "avLst"))
            relationships.append((f"rId{next_id}", IMAGE_REL, "media/pixel.png", None))
            next_id += 1

        if spec.external_relationship:
            relationships.append(
                (
                    f"rId{next_id}",
                    HYPERLINK_REL,
                    "https://example.invalid/reference",
                    "External",
                )
            )

        etree.SubElement(body, qn(W_NS, "sectPr"))
        return root, relationships

    @staticmethod
    def _styles() -> etree._Element:
        root = etree.Element(qn(W_NS, "styles"), nsmap={"w": W_NS})
        normal = etree.SubElement(root, qn(W_NS, "style"))
        normal.set(qn(W_NS, "type"), "paragraph")
        normal.set(qn(W_NS, "styleId"), "Normal")
        etree.SubElement(normal, qn(W_NS, "name")).set(qn(W_NS, "val"), "Normal")
        heading = etree.SubElement(root, qn(W_NS, "style"))
        heading.set(qn(W_NS, "type"), "paragraph")
        heading.set(qn(W_NS, "styleId"), "Heading1")
        etree.SubElement(heading, qn(W_NS, "name")).set(
            qn(W_NS, "val"),
            "Heading 1",
        )
        return root

    @staticmethod
    def _core(title: str) -> etree._Element:
        root = etree.Element(
            qn(CP_NS, "coreProperties"),
            nsmap={"cp": CP_NS, "dc": DC_NS},
        )
        etree.SubElement(root, qn(DC_NS, "title")).text = title
        etree.SubElement(root, qn(CP_NS, "lastModifiedBy")).text = "Raven tests"
        return root

    @staticmethod
    def _notes(kind: Literal["footnote", "endnote"]) -> etree._Element:
        root = etree.Element(qn(W_NS, f"{kind}s"), nsmap={"w": W_NS})
        separator = etree.SubElement(root, qn(W_NS, kind))
        separator.set(qn(W_NS, "id"), "-1")
        separator.set(qn(W_NS, "type"), "separator")
        separator.append(_paragraph("separator"))
        note = etree.SubElement(root, qn(W_NS, kind))
        note.set(qn(W_NS, "id"), "1")
        note.append(_paragraph(f"{kind.title()} text"))
        return root

    @staticmethod
    def _comments() -> etree._Element:
        root = etree.Element(qn(W_NS, "comments"), nsmap={"w": W_NS})
        comment = etree.SubElement(root, qn(W_NS, "comment"))
        comment.set(qn(W_NS, "id"), "0")
        comment.set(qn(W_NS, "author"), "Reviewer")
        comment.set(qn(W_NS, "initials"), "RV")
        comment.append(_paragraph("A useful comment"))
        return root

    @staticmethod
    def _custom_properties() -> etree._Element:
        preference = etree.Element("data")
        preference.set("data-version", "3")
        etree.SubElement(preference, "session").set("id", "fixture-session")
        style = etree.SubElement(preference, "style")
        style.set("id", "http://www.zotero.org/styles/apa")
        style.set("locale", "en-US")
        style.set("hasBibliography", "0")
        prefs = etree.SubElement(preference, "prefs")
        field_type = etree.SubElement(prefs, "pref")
        field_type.set("name", "fieldType")
        field_type.set("value", "Field")
        padding = etree.SubElement(prefs, "pref")
        padding.set("name", "unicodeFixture")
        padding.set("value", "漢😀é" * 180)
        preference_xml = etree.tostring(preference, encoding="unicode")

        root = etree.Element(
            qn(CUSTOM_NS, "Properties"),
            nsmap={None: CUSTOM_NS, "vt": VT_NS},
        )
        for index, chunk in enumerate(_utf16_chunks(preference_xml), start=1):
            prop = etree.SubElement(root, qn(CUSTOM_NS, "property"))
            prop.set("fmtid", "{D5CDD505-2E9C-101B-9397-08002B2CF9AE}")
            prop.set("pid", str(index + 1))
            prop.set("name", f"ZOTERO_PREF_{index}")
            etree.SubElement(prop, qn(VT_NS, "lpwstr")).text = chunk
        return root

    @staticmethod
    def _content_types(
        overrides: Sequence[tuple[str, str]],
        *,
        macro: bool,
        drawing: bool,
    ) -> etree._Element:
        root = etree.Element(
            qn(CONTENT_TYPES_NS, "Types"),
            nsmap={None: CONTENT_TYPES_NS},
        )
        for extension, content_type in (
            ("rels", "application/vnd.openxmlformats-package.relationships+xml"),
            ("xml", "application/xml"),
        ):
            default = etree.SubElement(root, qn(CONTENT_TYPES_NS, "Default"))
            default.set("Extension", extension)
            default.set("ContentType", content_type)
        if macro:
            default = etree.SubElement(root, qn(CONTENT_TYPES_NS, "Default"))
            default.set("Extension", "bin")
            default.set("ContentType", "application/vnd.ms-office.vbaProject")
        if drawing:
            default = etree.SubElement(root, qn(CONTENT_TYPES_NS, "Default"))
            default.set("Extension", "png")
            default.set("ContentType", "image/png")
        for part_name, content_type in overrides:
            override = etree.SubElement(root, qn(CONTENT_TYPES_NS, "Override"))
            override.set("PartName", part_name)
            override.set("ContentType", content_type)
        return root


@pytest.fixture
def docx_factory(tmp_path: Path) -> DocxFactory:
    return DocxFactory(tmp_path)


@pytest.fixture
def raven_settings(tmp_path: Path) -> Settings:
    return Settings(allowed_roots=(tmp_path,))


@pytest.fixture
def anyio_backend() -> str:
    """Use one deterministic async backend for HTTP and MCP integration tests."""

    return "asyncio"


@pytest.fixture
def minimal_docx(docx_factory: DocxFactory) -> Path:
    return docx_factory()


@pytest.fixture
def rich_docx(docx_factory: DocxFactory) -> Path:
    return docx_factory(
        "rich.docx",
        DocxSpec(
            table=True,
            notes=True,
            comments_revisions=True,
            unicode_long_prefs=True,
            drawing=True,
            external_relationship=True,
        ),
    )


@pytest.fixture
def split_zotero_docx(docx_factory: DocxFactory) -> Path:
    return docx_factory(
        "split-zotero.docx",
        DocxSpec(split_zotero=True),
    )


@pytest.fixture
def legacy_bibliography_docx(docx_factory: DocxFactory) -> Path:
    return docx_factory(
        "legacy-bibliography.docx",
        DocxSpec(legacy_bibliography=True),
    )
