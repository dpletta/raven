"""WordprocessingML inspection, locator, and edit tests."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from lxml import etree

from raven_mcp.config import Settings
from raven_mcp.docx.opc import OpcPackage
from raven_mcp.docx.wordml import (
    NS,
    add_comment,
    all_paragraphs,
    delete_text,
    discover_stories,
    field_balance_errors,
    insert_paragraph,
    insert_text,
    inspect_document,
    make_locator,
    paragraph_hash,
    paragraph_text,
    parse_complex_fields,
    qn,
    replace_text,
    resolve_locator,
    set_alt_text,
)
from raven_mcp.errors import ErrorCode, RavenError
from raven_mcp.schemas import DocumentLocator, StoryKind
from raven_mcp.validation import validate_semantic
from tests.conftest import DocxFactory, DocxSpec, W_NS


def _open(path: Path, settings: Settings) -> OpcPackage:
    return OpcPackage.open(path, settings)


def test_inspection_discovers_rich_document_structure(
    rich_docx: Path,
    raven_settings: Settings,
) -> None:
    package = _open(rich_docx, raven_settings)
    summary, paragraphs, metadata = inspect_document(
        package,
        rich_docx,
        "a" * 64,
    )

    assert summary.model_dump(exclude={"warnings"}) == {
        "path": str(rich_docx),
        "sha256": "a" * 64,
        "title": "Raven fixture",
        "paragraphs": 10,
        "words": summary.words,
        "headings": 1,
        "tables": 1,
        "figures": 1,
        "footnotes": 1,
        "endnotes": 1,
        "comments": 1,
        "revisions": 2,
        "citations": 0,
        "has_bibliography": False,
    }
    assert len(paragraphs) == 10
    assert {story["kind"] for story in metadata["stories"]} == {
        "body",
        "footnote",
        "endnote",
        "comment",
    }
    assert metadata["core_properties"]["title"] == "Raven fixture"
    assert metadata["styles"]["Heading1"] == "Heading 1"
    assert len(metadata["external_relationships"]) == 1
    assert "external relationship" in summary.warnings[0].lower()


def test_split_zotero_field_is_inspected_and_protected(
    split_zotero_docx: Path,
    raven_settings: Settings,
) -> None:
    package = _open(split_zotero_docx, raven_settings)
    summary, paragraphs, metadata = inspect_document(
        package,
        split_zotero_docx,
        "b" * 64,
    )

    assert summary.citations == 1
    assert metadata["complex_fields"][0]["balanced"] is True
    assert "ZOTERO_ITEM" in metadata["complex_fields"][0]["instruction"]
    citation_paragraph = next(item for item in paragraphs if "García" in item.text)
    assert (0, len(citation_paragraph.text), "field") in citation_paragraph.protected_ranges


def test_locator_resolves_context_and_occurrence(
    minimal_docx: Path,
    raven_settings: Settings,
) -> None:
    package = _open(minimal_docx, raven_settings)
    record = all_paragraphs(package)[1]
    locator = make_locator(record).model_copy(
        update={
            "exact_text": "alpha",
            "occurrence": 1,
            "prefix": "beta ",
            "suffix": ".",
        }
    )

    resolved = resolve_locator(package, locator)

    assert resolved.record.text == "Alpha beta alpha."
    assert resolved.start == 11
    assert resolved.end == 16
    assert paragraph_hash(record.element) == paragraph_hash(record.text)
    second_letter = resolve_locator(
        package,
        make_locator(record).model_copy(
            update={"exact_text": "a", "occurrence": 2}
        ),
    )
    assert (second_letter.start, second_letter.end) == (9, 10)


def test_locator_rejects_stale_missing_and_ambiguous_context(
    minimal_docx: Path,
    raven_settings: Settings,
) -> None:
    package = _open(minimal_docx, raven_settings)

    with pytest.raises(RavenError) as stale:
        resolve_locator(
            package,
            DocumentLocator(paragraph_index=0, paragraph_hash="0" * 64),
        )
    assert stale.value.code == ErrorCode.STALE_REVISION

    with pytest.raises(RavenError) as missing:
        resolve_locator(package, DocumentLocator(paragraph_index=99))
    assert missing.value.code == ErrorCode.ANCHOR_NOT_FOUND

    with pytest.raises(RavenError) as wrong_context:
        resolve_locator(
            package,
            DocumentLocator(
                paragraph_index=1,
                exact_text="alpha",
                occurrence=1,
                prefix="wrong",
            ),
        )
    assert wrong_context.value.code == ErrorCode.ANCHOR_NOT_FOUND


def test_tracked_insert_replace_and_delete_create_revisions(
    minimal_docx: Path,
    raven_settings: Settings,
) -> None:
    package = _open(minimal_docx, raven_settings)
    when = datetime(2024, 2, 3, 4, 5, tzinfo=UTC)
    locator = make_locator(all_paragraphs(package)[1])

    replace_text(
        package,
        locator,
        "beta",
        "gamma",
        tracked=True,
        author="Test Author",
        date=when,
    )

    root = package.read_xml("word/document.xml")
    paragraph = root.findall(".//w:p", namespaces=NS)[1]
    assert paragraph_text(paragraph) == "Alpha gamma alpha."
    insertion = paragraph.find("w:ins", namespaces=NS)
    deletion = paragraph.find("w:del", namespaces=NS)
    assert insertion is not None
    assert deletion is not None
    assert insertion.get(qn("author")) == "Test Author"
    assert insertion.get(qn("date")) == "2024-02-03T04:05:00Z"
    deleted_text = deletion.find(".//w:delText", namespaces=NS)
    assert deleted_text is not None
    assert deleted_text.text == "beta"

    deletion_package = _open(minimal_docx, raven_settings)
    fresh = make_locator(all_paragraphs(deletion_package)[1])
    delete_text(
        deletion_package,
        fresh,
        "beta",
        tracked=True,
        author="Test Author",
        date=when,
    )
    assert all_paragraphs(deletion_package)[1].text == "Alpha  alpha."

    insertion_package = _open(minimal_docx, raven_settings)
    latest = make_locator(all_paragraphs(insertion_package)[1])
    insert_text(
        insertion_package,
        latest,
        "Start: ",
        position="start",
        tracked=True,
        author="Test Author",
        date=when,
    )
    assert all_paragraphs(insertion_package)[1].text == "Start: Alpha beta alpha."


def test_untracked_edit_handles_multiple_runs_tabs_and_breaks(
    minimal_docx: Path,
    raven_settings: Settings,
) -> None:
    package = _open(minimal_docx, raven_settings)
    root = package.read_xml("word/document.xml")
    paragraph = root.findall(".//w:p", namespaces=NS)[1]
    run = paragraph.find("w:r", namespaces=NS)
    assert run is not None
    paragraph.remove(run)
    for value in ("Alpha be", "ta alpha."):
        replacement_run = etree.SubElement(paragraph, qn("r"))
        etree.SubElement(replacement_run, qn("t")).text = value
    package.set_xml("word/document.xml", root)

    locator = make_locator(all_paragraphs(package)[1])
    replace_text(package, locator, "beta", "B", tracked=False)
    assert all_paragraphs(package)[1].text == "Alpha B alpha."

    fresh = make_locator(all_paragraphs(package)[1])
    insert_text(package, fresh, "\tline\nnext", position="end", tracked=False)
    edited = package.read_xml("word/document.xml").findall(".//w:p", namespaces=NS)[1]
    assert edited.find(".//w:tab", namespaces=NS) is not None
    assert edited.find(".//w:br", namespaces=NS) is not None
    assert paragraph_text(edited) == "Alpha B alpha.\tline\nnext"


def test_insert_paragraph_copies_or_sets_style(
    minimal_docx: Path,
    raven_settings: Settings,
) -> None:
    package = _open(minimal_docx, raven_settings)
    locator = make_locator(all_paragraphs(package)[0])

    insert_paragraph(
        package,
        locator,
        "New section",
        position="after",
        style="Heading1",
        tracked=False,
    )

    records = all_paragraphs(package)
    assert [record.text for record in records[:3]] == [
        "Introduction",
        "New section",
        "Alpha beta alpha.",
    ]
    assert records[1].style == "Heading 1"


def test_comments_create_part_markers_relationship_and_content_type(
    minimal_docx: Path,
    raven_settings: Settings,
) -> None:
    package = _open(minimal_docx, raven_settings)
    locator = make_locator(all_paragraphs(package)[1])
    comment_id = add_comment(
        package,
        locator,
        "beta",
        "Check this claim.",
        author="Reviewer",
        initials="RV",
        date=datetime(2024, 1, 1, tzinfo=UTC),
    )

    assert comment_id == 0
    comments = package.read_xml("word/comments.xml")
    comment = comments.find(".//w:comment", namespaces=NS)
    assert comment is not None
    assert comment.get(qn("author")) == "Reviewer"
    comment_paragraph = comment.find("w:p", namespaces=NS)
    assert comment_paragraph is not None
    assert paragraph_text(comment_paragraph) == "Check this claim."
    report = validate_semantic(package)
    assert report["valid"] is True
    assert report["checks"]["comments"] == 1


def test_set_alt_text_updates_drawing_properties(
    docx_factory: DocxFactory,
    raven_settings: Settings,
) -> None:
    path = docx_factory("drawing.docx", DocxSpec(drawing=True))
    package = _open(path, raven_settings)
    drawing_record = all_paragraphs(package)[-1]

    set_alt_text(
        package,
        make_locator(drawing_record),
        "A scatterplot of the results",
        title="Figure 1",
    )

    root = package.read_xml("word/document.xml")
    properties = root.find(".//wp:docPr", namespaces=NS)
    assert properties is not None
    assert properties.get("descr") == "A scatterplot of the results"
    assert properties.get("title") == "Figure 1"


def test_protected_fields_revisions_and_content_controls_block_edits(
    split_zotero_docx: Path,
    rich_docx: Path,
    raven_settings: Settings,
) -> None:
    citation_package = _open(split_zotero_docx, raven_settings)
    citation = next(
        record for record in all_paragraphs(citation_package) if "García" in record.text
    )
    with pytest.raises(RavenError) as field_error:
        replace_text(
            citation_package,
            make_locator(citation),
            "García",
            "Garcia",
        )
    assert field_error.value.code == ErrorCode.PROTECTED_BOUNDARY

    revision_package = _open(rich_docx, raven_settings)
    revision = next(
        record for record in all_paragraphs(revision_package) if "inserted" in record.text
    )
    with pytest.raises(RavenError) as revision_error:
        replace_text(
            revision_package,
            make_locator(revision),
            "inserted",
            "changed",
        )
    assert revision_error.value.code == ErrorCode.PROTECTED_BOUNDARY

    root = revision_package.read_xml("word/document.xml")
    body = root.find("w:body", namespaces=NS)
    assert body is not None
    control = etree.Element(qn("sdt"))
    content = etree.SubElement(control, qn("sdtContent"))
    paragraph = etree.SubElement(content, qn("p"))
    run = etree.SubElement(paragraph, qn("r"))
    etree.SubElement(run, qn("t")).text = "Controlled"
    body.insert(0, control)
    revision_package.set_xml("word/document.xml", root)
    controlled = all_paragraphs(revision_package)[0]
    with pytest.raises(RavenError) as control_error:
        insert_text(revision_package, make_locator(controlled), "x")
    assert control_error.value.code == ErrorCode.PROTECTED_BOUNDARY


def test_complex_fields_parse_across_paragraphs_and_report_malformed_boundaries() -> None:
    root = etree.Element(qn("document"), nsmap={"w": W_NS})
    body = etree.SubElement(root, qn("body"))
    first = etree.SubElement(body, qn("p"))
    begin_run = etree.SubElement(first, qn("r"))
    etree.SubElement(begin_run, qn("fldChar")).set(qn("fldCharType"), "begin")
    instruction_run = etree.SubElement(first, qn("r"))
    etree.SubElement(instruction_run, qn("instrText")).text = " TEST "
    second = etree.SubElement(body, qn("p"))
    separator_run = etree.SubElement(second, qn("r"))
    etree.SubElement(separator_run, qn("fldChar")).set(
        qn("fldCharType"),
        "separate",
    )
    second.append(etree.fromstring(f'<w:r xmlns:w="{W_NS}"><w:t>result</w:t></w:r>'))
    end_run = etree.SubElement(second, qn("r"))
    etree.SubElement(end_run, qn("fldChar")).set(qn("fldCharType"), "end")
    paragraphs = root.findall(".//w:p", namespaces=NS)

    fields = parse_complex_fields(paragraphs, part_name="word/document.xml")

    assert len(fields) == 1
    assert fields[0].start_paragraph == 0
    assert fields[0].end_paragraph == 1
    assert fields[0].instruction == "TEST"
    assert fields[0].result == "result"
    assert field_balance_errors(paragraphs, part_name="word/document.xml") == []

    second.remove(end_run)
    with pytest.raises(ValueError, match="Unbalanced"):
        parse_complex_fields(paragraphs, require_balanced=True)
    assert "not closed" in field_balance_errors(paragraphs)[0]


def test_discover_stories_ignores_missing_and_external_story_targets(
    minimal_docx: Path,
    raven_settings: Settings,
) -> None:
    package = _open(minimal_docx, raven_settings)
    relationships = package.read_xml("word/_rels/document.xml.rels")
    for relationship_id, target, mode in (
        ("rId9", "missing-header.xml", None),
        ("rId10", "https://example.invalid/header.xml", "External"),
    ):
        relationship = etree.SubElement(
            relationships,
            "{http://schemas.openxmlformats.org/package/2006/relationships}Relationship",
        )
        relationship.set("Id", relationship_id)
        relationship.set(
            "Type",
            "http://schemas.openxmlformats.org/officeDocument/2006/"
            "relationships/header",
        )
        relationship.set("Target", target)
        if mode:
            relationship.set("TargetMode", mode)
    package.set_xml("word/_rels/document.xml.rels", relationships)

    assert [(story.kind, story.part_name) for story in discover_stories(package)] == [
        (StoryKind.BODY, "word/document.xml")
    ]
