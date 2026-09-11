"""Layered package, semantic, and Zotero validation tests."""

from __future__ import annotations

from pathlib import Path

from lxml import etree

from raven_mcp.config import Settings
from raven_mcp.docx.opc import OpcPackage
from raven_mcp.docx.wordml import NS, qn
from raven_mcp.validation import (
    ensure_valid,
    validate_document,
    validate_package,
    validate_semantic,
    validate_zotero,
)


def test_minimal_fixture_passes_every_validation_layer(
    minimal_docx: Path,
    raven_settings: Settings,
) -> None:
    package = OpcPackage.open(minimal_docx, raven_settings)

    package_report = validate_package(package)
    semantic_report = validate_semantic(package)
    zotero_report = validate_zotero(package)
    full_report = ensure_valid(package)

    assert package_report["valid"] is True
    assert package_report["checks"] == {
        "zip_integrity": True,
        "xml_parts": 6,
        "relationships": 3,
        "external_relationships": 0,
    }
    assert semantic_report["valid"] is True
    assert semantic_report["checks"]["stories"] == 1
    assert zotero_report["valid"] is True
    assert full_report["valid"] is True


def test_package_validation_reports_missing_relationship_targets(
    minimal_docx: Path,
    raven_settings: Settings,
) -> None:
    package = OpcPackage.open(minimal_docx, raven_settings)
    package.add_relationship(
        "word/document.xml",
        "word/missing.xml",
        "http://example.invalid/test-relationship",
    )

    report = validate_package(package)

    assert report["valid"] is False
    assert "targets missing part word/missing.xml" in report["errors"][0]


def test_semantic_validation_reports_comment_bookmark_and_revision_problems(
    minimal_docx: Path,
    raven_settings: Settings,
) -> None:
    package = OpcPackage.open(minimal_docx, raven_settings)
    root = package.read_xml("word/document.xml")
    paragraph = root.findall(".//w:p", namespaces=NS)[1]
    etree.SubElement(paragraph, qn("commentRangeStart")).set(qn("id"), "55")
    etree.SubElement(paragraph, qn("bookmarkStart")).set(qn("id"), "9")
    revision = etree.SubElement(paragraph, qn("ins"))
    revision.set(qn("id"), "12")
    run = etree.SubElement(revision, qn("r"))
    etree.SubElement(run, qn("t")).text = "revision"
    package.set_xml("word/document.xml", root)

    report = validate_semantic(package)

    assert report["valid"] is False
    assert any("Comment marker 55 has no comment body" in item for item in report["errors"])
    assert any("Comment range 55 is not balanced" in item for item in report["errors"])
    assert any("Bookmark 9 is not balanced" in item for item in report["errors"])
    assert any("revision 12 has no author" in item for item in report["warnings"])


def test_zotero_validation_rejects_noncontiguous_preference_chunks(
    rich_docx: Path,
    raven_settings: Settings,
) -> None:
    package = OpcPackage.open(rich_docx, raven_settings)
    custom = package.read_xml("docProps/custom.xml")
    properties = list(custom)
    assert len(properties) >= 3
    custom.remove(properties[1])
    package.set_xml("docProps/custom.xml", custom)

    report = validate_zotero(package)

    assert report["valid"] is False
    assert any("not contiguous" in item for item in report["errors"])


def test_full_validation_prefixes_layer_names(
    minimal_docx: Path,
    raven_settings: Settings,
) -> None:
    package = OpcPackage.open(minimal_docx, raven_settings)
    relationships = package.read_xml("word/_rels/document.xml.rels")
    relationship = etree.SubElement(
        relationships,
        "{http://schemas.openxmlformats.org/package/2006/relationships}Relationship",
    )
    relationship.set("Id", "rId99")
    relationship.set("Type", "http://example.invalid/missing")
    relationship.set("Target", "missing.xml")
    package.set_xml("word/_rels/document.xml.rels", relationships)

    report = validate_document(package)

    assert report["valid"] is False
    assert report["package"]["valid"] is False
    assert any(item.startswith("package: ") for item in report["errors"])
