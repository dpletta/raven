"""Native Zotero payload, field, preference, and bibliography tests."""

from __future__ import annotations

import io
import zipfile
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

import pytest
from lxml import etree

from raven_mcp.citations.fields import (
    build_complex_field_runs,
    find_unique_field,
    insert_complex_field,
    parse_complex_fields,
    remove_complex_field,
    replace_complex_field,
    scan_complex_fields,
)
from raven_mcp.citations.zotero import (
    BIBLIOGRAPHY_PLACEHOLDER,
    CitationManager,
    build_bibliography_instruction,
    build_citation_instruction,
    build_citation_payload,
    chunk_utf16,
    fallback_citation_text,
    parse_bibliography_instruction,
    parse_citation_instruction,
    parse_zotero_instruction,
)
from raven_mcp.config import Settings
from raven_mcp.docx.opc import OpcPackage
from raven_mcp.docx.wordml import NS, all_paragraphs, make_locator
from raven_mcp.errors import ErrorCode, RavenError
from raven_mcp.schemas import CitationItemInput, DocumentLocator, StoryKind
from raven_mcp.validation import (
    raise_for_failed_validation,
    validate_document,
    validate_semantic,
    validate_zotero,
)
from tests.conftest import (
    DEFAULT_CITATION_ID,
    DEFAULT_ITEM_KEY,
    DocxFactory,
    DocxSpec,
)

KEY_B = "WXYZ6789"


def _open(path: Path, settings: Settings) -> OpcPackage:
    return OpcPackage.open(path, settings)


def _citation_item(
    key: str = DEFAULT_ITEM_KEY,
    *,
    title: str = "Café research",
) -> CitationItemInput:
    return CitationItemInput(
        item_key=key,
        uri=f"http://zotero.org/users/1/items/{key}",
        csl_json={
            "id": key,
            "type": "article-journal",
            "title": title,
            "author": [{"family": "García", "given": "Ana"}],
            "issued": {"date-parts": [[2024]]},
        },
        locator="12",
        prefix="see ",
        suffix=", emphasis added",
        suppress_author=True,
    )


def test_payload_building_preserves_unknown_members_and_canonical_round_trip() -> None:
    existing = {
        "citationID": "existing-id",
        "citationItems": [
            {
                "id": DEFAULT_ITEM_KEY,
                "uris": ["old-uri"],
                "itemData": {"title": "Old"},
                "pluginPrivate": {"retain": True},
            }
        ],
        "properties": {"noteIndex": 4, "pluginProperty": "retain"},
        "topLevelExtension": [1, "é"],
    }

    payload = build_citation_payload(
        [_citation_item()],
        formatted="<i>(García, 2024)</i>",
        existing=existing,
    )
    instruction = build_citation_instruction(payload)

    assert payload["citationID"] == "existing-id"
    assert payload["topLevelExtension"] == [1, "é"]
    item = payload["citationItems"][0]
    assert item["pluginPrivate"] == {"retain": True}
    assert item["locator"] == "12"
    assert item["suppress-author"] is True
    assert payload["properties"]["pluginProperty"] == "retain"
    assert payload["properties"]["plainCitation"] == "(García, 2024)"
    assert parse_citation_instruction(instruction) == payload
    parsed = parse_zotero_instruction(instruction)
    assert parsed is not None
    assert parsed.kind == "citation"
    assert parsed.legacy is False


def test_payload_validation_and_fallback_text() -> None:
    assert (
        fallback_citation_text([_citation_item(), {"item_key": KEY_B}])
        == f"[Citation: {DEFAULT_ITEM_KEY}; {KEY_B}]"
    )

    with pytest.raises(RavenError) as empty:
        build_citation_payload([])
    assert empty.value.code == ErrorCode.INVALID_REQUEST

    with pytest.raises(RavenError) as key:
        build_citation_payload([{"csl_json": {}}])
    assert key.value.code == ErrorCode.INVALID_REQUEST

    with pytest.raises(RavenError) as json_error:
        build_citation_instruction({"bad": {1, 2, 3}})
    assert json_error.value.code == ErrorCode.INVALID_REQUEST


@pytest.mark.parametrize(
    "instruction",
    [
        ' ADDIN ZOTERO_ITEM {"citationID":"legacy","citationItems":[{"id":"ABCD2345"}]} ',
        (' CSL_CITATION {"citationID":"legacy-csl","citationItems":[{"id":"ABCD2345"}]} '),
    ],
)
def test_legacy_citation_instructions_are_accepted(instruction: str) -> None:
    parsed = parse_zotero_instruction(instruction)
    assert parsed is not None
    assert parsed.kind == "citation"
    assert parsed.legacy is True
    assert parse_citation_instruction(instruction) == parsed.payload


def test_legacy_and_current_bibliography_forms_are_accepted() -> None:
    payload = {"uncited": ["A"], "omitted": [], "custom": []}
    current = build_bibliography_instruction(payload)
    legacy = ' ADDIN ZOTERO_BIBL {"uncited":["A"],"omitted":[],"custom":[]} CSL_BIBLIOGRAPHY '

    assert parse_bibliography_instruction(current) == payload
    assert parse_bibliography_instruction(legacy) == payload
    assert parse_citation_instruction(current) is None


def test_field_scanner_reassembles_split_instruction_runs(
    split_zotero_docx: Path,
    raven_settings: Settings,
) -> None:
    package = _open(split_zotero_docx, raven_settings)
    root = package.read_xml("word/document.xml")

    scan = scan_complex_fields(root)

    assert scan.warnings == []
    assert len(scan.fields) == 1
    field = scan.fields[0]
    assert field.field_id == DEFAULT_CITATION_ID
    assert field.is_zotero_citation
    assert field.visible_text == "(García, 2024)"
    assert len(field.instruction_nodes) == 2
    assert parse_complex_fields(root, strict=True) == scan.fields


@pytest.mark.parametrize("malformed", ["unclosed", "unmatched-end"])
def test_field_scanner_reports_malformed_boundaries(
    malformed: Literal["unclosed", "unmatched-end"],
    docx_factory: DocxFactory,
    raven_settings: Settings,
) -> None:
    path = docx_factory(
        f"{malformed}.docx",
        DocxSpec(malformed_field=malformed),
    )
    package = _open(path, raven_settings)
    root = package.read_xml("word/document.xml")
    scan = scan_complex_fields(root)

    assert scan.warnings
    with pytest.raises(RavenError) as error:
        parse_complex_fields(root, strict=True)
    assert error.value.code == ErrorCode.PROTECTED_BOUNDARY


def test_field_surgery_replaces_and_removes_only_the_managed_runs() -> None:
    root = etree.fromstring(
        b'<w:document xmlns:w="'
        b"http://schemas.openxmlformats.org/wordprocessingml/2006/main"
        b'"><w:body><w:p><w:r><w:t>Before </w:t></w:r></w:p></w:body></w:document>'
    )
    paragraph = root.find(".//w:p", namespaces=NS)
    assert paragraph is not None
    for run in build_complex_field_runs(
        ' ADDIN ZOTERO_ITEM {"citationID":"field-1","citationItems":[{"id":"ABCD2345"}]} ',
        "(Old)",
    ):
        paragraph.append(run)
    paragraph.append(
        etree.fromstring(
            b'<w:r xmlns:w="http://schemas.openxmlformats.org/'
            b'wordprocessingml/2006/main"><w:t> After</w:t></w:r>'
        )
    )
    field = parse_complex_fields(root, strict=True)[0]

    replace_complex_field(field, visible_text="(New)")
    reparsed = parse_complex_fields(root, strict=True)[0]
    assert reparsed.visible_text == "(New)"
    assert "Before " in "".join(root.itertext())
    assert " After" in "".join(root.itertext())

    remove_complex_field(reparsed, keep_visible=True)
    assert parse_complex_fields(root) == []
    assert "".join(root.itertext()) == "Before (New) After"


def test_insert_complex_field_rejects_anchor_inside_existing_result(
    split_zotero_docx: Path,
    raven_settings: Settings,
) -> None:
    package = _open(split_zotero_docx, raven_settings)
    root = package.read_xml("word/document.xml")
    citation_record = next(record for record in all_paragraphs(package) if "García" in record.text)
    locator = make_locator(citation_record).model_copy(update={"exact_text": "García"})

    with pytest.raises(RavenError) as error:
        insert_complex_field(
            root,
            locator,
            build_citation_instruction(build_citation_payload([_citation_item(KEY_B)])),
            "(Other, 2025)",
            position="after",
        )

    assert error.value.code == ErrorCode.PROTECTED_BOUNDARY


def test_find_unique_field_detects_missing_and_duplicate_ids(
    split_zotero_docx: Path,
    raven_settings: Settings,
) -> None:
    package = _open(split_zotero_docx, raven_settings)
    fields = parse_complex_fields(package.read_xml("word/document.xml"))

    assert find_unique_field(fields, DEFAULT_CITATION_ID) is fields[0]
    with pytest.raises(RavenError) as missing:
        find_unique_field(fields, "missing")
    assert missing.value.code == ErrorCode.REFERENCE_UNRESOLVED
    with pytest.raises(RavenError) as duplicate:
        find_unique_field([fields[0], fields[0]], DEFAULT_CITATION_ID)
    assert duplicate.value.code == ErrorCode.ANCHOR_AMBIGUOUS


def test_citation_manager_insert_lists_and_writes_preferences(
    minimal_docx: Path,
    raven_settings: Settings,
) -> None:
    package = _open(minimal_docx, raven_settings)
    locator = make_locator(all_paragraphs(package)[1]).model_copy(update={"exact_text": "beta"})

    result = CitationManager(package).insert(
        locator,
        [_citation_item()],
        formatted="<i>(García, 2024)</i>",
        style="http://www.zotero.org/styles/chicago-author-date",
        locale="es-ES",
    )

    assert result["citation_id"]
    assert result["field"]["visible_text"] == "(García, 2024)"
    assert result["preferences"]["style"].endswith("chicago-author-date")
    assert result["preferences"]["locale"] == "es-ES"
    assert {
        "word/document.xml",
        "docProps/custom.xml",
        "_rels/.rels",
        "[Content_Types].xml",
    }.issubset(result["changed_parts"])
    listed = CitationManager(package).list_citations()
    assert listed["citations"][0]["citation_id"] == result["citation_id"]
    assert listed["bibliography"] is None
    assert validate_zotero(package)["valid"] is True


def test_citation_manager_update_and_remove_keep_visible_text(
    split_zotero_docx: Path,
    raven_settings: Settings,
) -> None:
    package = _open(split_zotero_docx, raven_settings)
    manager = CitationManager(package)

    updated = manager.update(
        DEFAULT_CITATION_ID,
        [_citation_item(KEY_B, title="Updated")],
        formatted="(Updated, 2025)",
    )

    assert updated["citation_id"] == DEFAULT_CITATION_ID
    assert updated["payload"]["citationItems"][0]["id"] == KEY_B
    assert updated["visible_text"] == "(Updated, 2025)"
    removed = CitationManager(package).remove(
        DEFAULT_CITATION_ID,
        keep_visible=True,
    )
    assert removed["removed"] is True
    assert removed["visible_text"] == "(Updated, 2025)"
    assert CitationManager(package).list_citations()["citations"] == []
    assert "(Updated, 2025)" in "".join(package.read_xml("word/document.xml").itertext())


def test_long_unicode_preferences_round_trip_and_can_be_rewritten(
    rich_docx: Path,
    raven_settings: Settings,
) -> None:
    package = _open(rich_docx, raven_settings)
    manager = CitationManager(package)

    before = manager.inspect_preferences()
    assert before["chunks"] > 1
    assert "漢😀é" in before["raw_xml"]
    result = manager.set_preferences(
        style="http://www.zotero.org/styles/vancouver",
        locale="ja-JP",
        session="stable-session",
    )
    after = manager.inspect_preferences()

    assert result["preferences"]["session"] == "stable-session"
    assert after["style"].endswith("vancouver")
    assert after["locale"] == "ja-JP"
    assert after["field_type"] == "Field"
    custom = package.read_xml("docProps/custom.xml")
    chunks = ["".join(prop.itertext()) for prop in custom]
    assert all(len(chunk.encode("utf-16-le")) // 2 <= 255 for chunk in chunks)
    assert validate_zotero(package)["valid"] is True


def test_preference_field_type_must_remain_native(
    minimal_docx: Path,
    raven_settings: Settings,
) -> None:
    package = _open(minimal_docx, raven_settings)
    with pytest.raises(RavenError) as error:
        CitationManager(package).set_preferences(field_type="Bookmark")
    assert error.value.code == ErrorCode.ZOTERO_INCOMPATIBLE


def test_bibliography_creation_and_legacy_synchronization(
    minimal_docx: Path,
    legacy_bibliography_docx: Path,
    raven_settings: Settings,
) -> None:
    package = _open(minimal_docx, raven_settings)
    locator = make_locator(all_paragraphs(package)[-1])

    created = CitationManager(package).sync_bibliography(
        locator,
        heading="References",
    )

    assert created["created"] is True
    assert created["field"]["visible_text"] == BIBLIOGRAPHY_PLACEHOLDER
    assert created["preferences"]["has_bibliography"] is True
    records = all_paragraphs(package)
    assert [record.text for record in records[-2:]] == [
        "References",
        BIBLIOGRAPHY_PLACEHOLDER,
    ]
    assert validate_document(package)["valid"] is True

    legacy_package = _open(legacy_bibliography_docx, raven_settings)
    synced = CitationManager(legacy_package).sync_bibliography()
    assert synced["created"] is False
    bibliography = CitationManager(legacy_package).list_citations()["bibliography"]
    assert bibliography is not None
    assert bibliography["visible_text"] == BIBLIOGRAPHY_PLACEHOLDER
    assert bibliography["payload"] == {
        "uncited": [],
        "omitted": [],
        "custom": [],
    }


def test_validation_reports_malformed_fields_payloads_and_preferences(
    docx_factory: DocxFactory,
    split_zotero_docx: Path,
    raven_settings: Settings,
) -> None:
    malformed_path = docx_factory(
        "malformed.docx",
        DocxSpec(malformed_field="unclosed"),
    )
    malformed = _open(malformed_path, raven_settings)
    semantic = validate_semantic(malformed)
    assert semantic["valid"] is False
    assert "not closed" in semantic["errors"][0]

    package = _open(split_zotero_docx, raven_settings)
    root = package.read_xml("word/document.xml")
    instruction_nodes = root.findall(".//w:instrText", namespaces=NS)
    instruction_nodes[0].text = " ADDIN ZOTERO_ITEM {malformed"
    for node in instruction_nodes[1:]:
        node.text = ""
    package.set_xml("word/document.xml", root)
    zotero = validate_zotero(package)
    assert zotero["valid"] is False
    assert "malformed JSON" in zotero["errors"][0]
    with pytest.raises(RavenError) as failed:
        raise_for_failed_validation(validate_document(package))
    assert failed.value.code == ErrorCode.VALIDATION_FAILED


def test_chunk_utf16_examples_do_not_split_supplementary_characters() -> None:
    value = "a" * 254 + "😀" + "b"
    chunks = chunk_utf16(value)
    assert "".join(chunks) == value
    assert chunks == ["a" * 254, "😀" + "b"]
    assert all(len(chunk.encode("utf-16-le")) // 2 <= 255 for chunk in chunks)
    assert chunk_utf16("") == [""]
    with pytest.raises(ValueError, match="at least 2"):
        chunk_utf16("x", limit=1)


class _FailingPackage:
    """Small package double that fails on the second staged write."""

    def __init__(self, parts: Mapping[str, bytes], settings: Settings) -> None:
        self.parts = dict(parts)
        self.settings = settings
        self.writes = 0

    def has_part(self, part: str) -> bool:
        return part.lstrip("/") in self.parts

    def read_bytes(self, part: str) -> bytes:
        return self.parts[part.lstrip("/")]

    def read_xml(self, part: str) -> etree._Element:
        return etree.fromstring(self.read_bytes(part))

    def set_bytes(self, part: str, data: bytes) -> None:
        self.writes += 1
        if self.writes == 2:
            raise OSError("simulated package write failure")
        self.parts[part.lstrip("/")] = bytes(data)

    def remove_part(self, part: str) -> None:
        self.parts.pop(part.lstrip("/"), None)


def _archive_parts(path: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(path.read_bytes())) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def test_citation_commit_restores_prior_parts_after_partial_write_failure(
    minimal_docx: Path,
    raven_settings: Settings,
) -> None:
    package = _FailingPackage(_archive_parts(minimal_docx), raven_settings)
    original = dict(package.parts)
    locator = DocumentLocator(
        story=StoryKind.BODY,
        paragraph_index=1,
        exact_text="beta",
    )

    with pytest.raises(OSError, match="simulated"):
        CitationManager(package).insert(
            locator,
            [_citation_item()],
            formatted="(García, 2024)",
        )

    assert package.parts == original
