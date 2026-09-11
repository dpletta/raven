"""Open Packaging Convention preservation and security tests."""

from __future__ import annotations

import io
import zipfile
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
from lxml import etree

from raven_mcp.config import Settings
from raven_mcp.docx.opc import (
    CUSTOM_PROPERTIES_CONTENT_TYPE,
    CONTENT_TYPES_NS,
    OpcPackage,
    resolve_relationship_target,
)
from raven_mcp.errors import ErrorCode, RavenError
from tests.conftest import CUSTOM_REL, DocxFactory, DocxSpec


def _open(path: Path, settings: Settings) -> OpcPackage:
    return OpcPackage.open(path, settings)


def test_open_noop_returns_the_exact_original_archive(
    minimal_docx: Path,
    raven_settings: Settings,
) -> None:
    original = minimal_docx.read_bytes()
    package = _open(minimal_docx, raven_settings)

    assert package.to_bytes() == original
    assert package.changed_parts == frozenset()
    assert package.read_bytes("word/document.xml").startswith(b"<?xml")
    assert package.content_type_for("word/document.xml") is not None
    assert package.member_info("word/document.xml").date_time == (2024, 1, 2, 3, 4, 6)

    mutable = cast(dict[str, bytes], package.members)
    with pytest.raises(TypeError):
        mutable["word/document.xml"] = b"changed"


def test_clone_is_independent_and_serialization_preserves_member_metadata(
    minimal_docx: Path,
    raven_settings: Settings,
) -> None:
    package = _open(minimal_docx, raven_settings)
    original_info = package.member_info("docProps/core.xml")
    clone = package.clone()
    styles = clone.read_xml("word/styles.xml")
    styles.set("fixture-change", "yes")
    clone.set_xml("word/styles.xml", styles)

    assert package.changed_parts == frozenset()
    assert clone.changed_parts == frozenset({"word/styles.xml"})
    assert package.read_xml("word/styles.xml").get("fixture-change") is None

    with zipfile.ZipFile(io.BytesIO(clone.to_bytes())) as archive:
        assert archive.comment == b"raven-fixture"
        untouched = archive.getinfo("docProps/core.xml")
        assert untouched.date_time == original_info.date_time
        assert untouched.compress_type == original_info.compress_type
        assert untouched.external_attr == original_info.external_attr
        assert archive.read("docProps/core.xml") == package.read_bytes(
            "docProps/core.xml"
        )


def test_add_remove_parts_content_types_and_relationships(
    minimal_docx: Path,
    raven_settings: Settings,
) -> None:
    package = _open(minimal_docx, raven_settings)
    custom = etree.Element(
        "{http://schemas.openxmlformats.org/officeDocument/2006/"
        "custom-properties}Properties"
    )
    package.set_xml("docProps/custom.xml", custom)
    package.set_content_type_override(
        "docProps/custom.xml",
        CUSTOM_PROPERTIES_CONTENT_TYPE,
    )
    relationship_id = package.add_relationship(
        None,
        "docProps/custom.xml",
        CUSTOM_REL,
    )

    assert relationship_id == "rId3"
    assert package.content_type_for("docProps/custom.xml") == CUSTOM_PROPERTIES_CONTENT_TYPE
    relationship = next(
        item
        for item in package.relationships(None)
        if item.relationship_type == CUSTOM_REL
    )
    assert package.relationship_target(relationship) == "docProps/custom.xml"

    package.remove_part("docProps/custom.xml")
    assert not package.has_part("docProps/custom.xml")
    assert "docProps/custom.xml" not in package.members
    with pytest.raises(RavenError, match="does not exist"):
        package.read_bytes("docProps/custom.xml")


def test_custom_properties_support_standard_scalar_types(
    minimal_docx: Path,
    raven_settings: Settings,
) -> None:
    package = _open(minimal_docx, raven_settings)
    package.set_custom_property("Text", "café")
    package.set_custom_property("Count", 7)
    package.set_custom_property("Enabled", True)
    package.set_custom_property("When", datetime(2024, 1, 2, 3, 4, tzinfo=UTC))
    package.set_custom_property("Count", 9)

    custom = package.read_xml("docProps/custom.xml")
    values = {
        node.get("name"): "".join(node.itertext())
        for node in custom
    }
    assert values == {
        "Text": "café",
        "Count": "9",
        "Enabled": "true",
        "When": "2024-01-02T03:04:00Z",
    }
    assert package.content_type_for("docProps/custom.xml") == CUSTOM_PROPERTIES_CONTENT_TYPE
    assert sum(item.relationship_type == CUSTOM_REL for item in package.relationships()) == 1


def test_external_relationships_are_reported_but_never_resolved(
    docx_factory: DocxFactory,
    raven_settings: Settings,
) -> None:
    path = docx_factory(
        "external.docx",
        DocxSpec(external_relationship=True),
    )
    package = _open(path, raven_settings)
    external = package.external_relationships()

    assert len(external) == 1
    assert external[0].target == "https://example.invalid/reference"
    assert external[0].external
    assert package.relationship_target(external[0]) is None


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        ("styles.xml", "word/styles.xml"),
        ("../docProps/core.xml", "docProps/core.xml"),
        ("/word/document.xml", "word/document.xml"),
        ("media/image%201.png#fragment", "word/media/image 1.png"),
    ],
)
def test_relationship_target_resolution(
    target: str,
    expected: str,
) -> None:
    assert resolve_relationship_target("word/document.xml", target) == expected


@pytest.mark.parametrize(
    "target",
    [
        "../../../outside.xml",
        r"..\outside.xml",
        "file:///etc/passwd",
        "https://example.invalid/part.xml",
        "%2e%2e/%2e%2e/outside.xml",
    ],
)
def test_relationship_target_resolution_rejects_escapes(target: str) -> None:
    with pytest.raises(RavenError) as error:
        resolve_relationship_target("word/document.xml", target)
    assert error.value.code == ErrorCode.UNSAFE_PACKAGE


@pytest.mark.parametrize(
    ("name", "spec", "code"),
    [
        (
            "traversal.docx",
            DocxSpec(traversal_member="../escape.xml"),
            ErrorCode.UNSAFE_PACKAGE,
        ),
        ("doctype.docx", DocxSpec(doctype=True), ErrorCode.UNSAFE_PACKAGE),
        ("macro.docx", DocxSpec(macro=True), ErrorCode.UNSUPPORTED_DOCUMENT),
    ],
)
def test_package_rejects_traversal_doctype_and_macros(
    name: str,
    spec: DocxSpec,
    code: ErrorCode,
    docx_factory: DocxFactory,
    raven_settings: Settings,
) -> None:
    path = docx_factory(name, spec)
    with pytest.raises(RavenError) as error:
        _open(path, raven_settings)
    assert error.value.code == code


def test_set_bytes_rejects_active_content_strict_xml_and_dtd(
    minimal_docx: Path,
    raven_settings: Settings,
) -> None:
    package = _open(minimal_docx, raven_settings)

    with pytest.raises(RavenError) as macro:
        package.set_bytes("word/vbaProject.bin", b"macro")
    assert macro.value.code == ErrorCode.UNSUPPORTED_DOCUMENT

    with pytest.raises(RavenError) as strict:
        package.set_bytes(
            "word/strict.xml",
            b'<x xmlns="http://purl.oclc.org/ooxml/wordprocessingml/main"/>',
        )
    assert strict.value.code == ErrorCode.UNSUPPORTED_DOCUMENT

    with pytest.raises(RavenError) as doctype:
        package.set_bytes("word/dtd.xml", b"<!DOCTYPE x><x/>")
    assert doctype.value.code == ErrorCode.UNSAFE_PACKAGE


def test_open_enforces_document_member_and_uncompressed_limits(
    minimal_docx: Path,
    raven_settings: Settings,
) -> None:
    raw_size = minimal_docx.stat().st_size
    with zipfile.ZipFile(minimal_docx) as archive:
        member_count = len(archive.infolist())
        uncompressed = sum(item.file_size for item in archive.infolist())

    cases = [
        replace(raven_settings, max_document_bytes=raw_size - 1),
        replace(raven_settings, max_zip_members=member_count - 1),
        replace(raven_settings, max_uncompressed_bytes=uncompressed - 1),
    ]
    for settings in cases:
        with pytest.raises(RavenError) as error:
            _open(minimal_docx, settings)
        assert error.value.code == ErrorCode.RESOURCE_LIMIT


def test_edit_and_serialization_limits_are_enforced(
    minimal_docx: Path,
    raven_settings: Settings,
) -> None:
    with zipfile.ZipFile(minimal_docx) as archive:
        uncompressed = sum(item.file_size for item in archive.infolist())

    member_limited = _open(
        minimal_docx,
        replace(raven_settings, max_zip_members=6),
    )
    with pytest.raises(RavenError) as member_error:
        member_limited.set_bytes("word/new.bin", b"x")
    assert member_error.value.code == ErrorCode.RESOURCE_LIMIT

    size_limited = _open(
        minimal_docx,
        replace(raven_settings, max_uncompressed_bytes=uncompressed + 1),
    )
    with pytest.raises(RavenError) as size_error:
        size_limited.set_bytes("word/new.bin", b"xx")
    assert size_error.value.code == ErrorCode.RESOURCE_LIMIT

    serialized_limited = _open(
        minimal_docx,
        replace(
            raven_settings,
            max_document_bytes=minimal_docx.stat().st_size,
            max_uncompressed_bytes=uncompressed + 10_000,
        ),
    )
    serialized_limited.set_bytes("word/random.bin", bytes(range(256)) * 16)
    with pytest.raises(RavenError) as serialized_error:
        serialized_limited.to_bytes()
    assert serialized_error.value.code == ErrorCode.RESOURCE_LIMIT


def test_write_creates_an_atomic_reopenable_copy(
    minimal_docx: Path,
    raven_settings: Settings,
    tmp_path: Path,
) -> None:
    package = _open(minimal_docx, raven_settings)
    package.set_custom_property("Fixture", "written")
    output = tmp_path / "nested" / "copy.docx"

    package.write(output)

    reopened = _open(output, raven_settings)
    assert reopened.read_xml("docProps/custom.xml") is not None
    assert not list(output.parent.glob(f".{output.name}.*.tmp"))


def test_invalid_content_types_root_and_duplicate_defaults_are_rejected(
    minimal_docx: Path,
    raven_settings: Settings,
) -> None:
    package = _open(minimal_docx, raven_settings)
    package.set_bytes("[Content_Types].xml", b"<wrong/>")
    with pytest.raises(RavenError, match="invalid root"):
        _ = package.content_types

    root = etree.Element(f"{{{CONTENT_TYPES_NS}}}Types")
    for value in ("xml", "XML"):
        item = etree.SubElement(root, f"{{{CONTENT_TYPES_NS}}}Default")
        item.set("Extension", value)
        item.set("ContentType", "application/xml")
    package.set_xml("[Content_Types].xml", root)
    with pytest.raises(RavenError, match="Duplicate default"):
        _ = package.content_types
