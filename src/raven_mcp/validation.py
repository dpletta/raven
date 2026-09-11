"""Layered validation for edited Raven DOCX packages."""

from __future__ import annotations

import io
import json
import posixpath
import re
import zipfile
from collections.abc import Mapping
from typing import Any

from lxml import etree

from raven_mcp.docx.opc import CUSTOM_PROPERTIES_NS, OpcPackage
from raven_mcp.docx.wordml import (
    NS,
    W_NS,
    discover_stories,
    field_balance_errors,
    parse_complex_fields,
    qn,
)
from raven_mcp.errors import ErrorCode, RavenError

_PREFERENCE_PATTERN = re.compile(r"^ZOTERO_PREF_(\d+)$")
_CITATION_MARKERS = ("ZOTERO_ITEM", "CSL_CITATION")


def _report(
    *,
    errors: list[str],
    warnings: list[str],
    checks: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "checks": dict(checks),
    }


def _source_for_relationship_part(part_name: str) -> str | None:
    if part_name == "_rels/.rels":
        return None
    parent, leaf = posixpath.split(part_name)
    if not parent.endswith("/_rels") or not leaf.endswith(".rels"):
        return None
    return posixpath.join(parent.removesuffix("/_rels"), leaf.removesuffix(".rels"))


def validate_package(package: OpcPackage) -> dict[str, Any]:
    """Validate ZIP integrity, XML, content types, and relationship targets."""

    errors: list[str] = []
    warnings: list[str] = []
    xml_parts = 0
    relationship_count = 0
    external_count = 0

    try:
        serialized = package.to_bytes()
        with zipfile.ZipFile(io.BytesIO(serialized), mode="r") as archive:
            bad_member = archive.testzip()
            if bad_member is not None:
                errors.append(f"ZIP CRC check failed for {bad_member}.")
            names = archive.namelist()
            if len(names) != len(set(names)):
                errors.append("ZIP contains duplicate member names.")
    except (
        RavenError,
        zipfile.BadZipFile,
        RuntimeError,
        OSError,
        NotImplementedError,
    ) as exc:
        errors.append(f"ZIP integrity check failed: {exc}")

    for part_name in package.members:
        if not (
            part_name.endswith(".xml")
            or part_name.endswith(".rels")
            or part_name == "[Content_Types].xml"
        ):
            continue
        xml_parts += 1
        try:
            package.read_xml(part_name)
        except RavenError as exc:
            errors.append(f"{part_name}: {exc}")

    try:
        content_types = package.content_types
        missing_types = [
            name
            for name in package.members
            if not name.endswith("/")
            and name != "[Content_Types].xml"
            and name not in content_types
        ]
        errors.extend(
            f"No content type is declared for package part {name}."
            for name in missing_types
        )
    except RavenError as exc:
        errors.append(f"Content type validation failed: {exc}")

    for part_name in package.members:
        if part_name != "_rels/.rels" and not part_name.endswith(".rels"):
            continue
        source = _source_for_relationship_part(part_name)
        if part_name != "_rels/.rels" and source is None:
            warnings.append(f"Ignored non-standard relationships part {part_name}.")
            continue
        try:
            relationships = package.relationships(source)
        except RavenError as exc:
            errors.append(f"{part_name}: {exc}")
            continue
        relationship_count += len(relationships)
        for relationship in relationships:
            if relationship.external:
                external_count += 1
                warnings.append(
                    f"External relationship {relationship.relationship_id} in "
                    f"{part_name} was not fetched."
                )
                continue
            try:
                target = package.relationship_target(relationship)
            except RavenError as exc:
                errors.append(f"{part_name}/{relationship.relationship_id}: {exc}")
                continue
            if target is None or not package.has_part(target):
                errors.append(
                    f"Relationship {relationship.relationship_id} in {part_name} "
                    f"targets missing part {target or relationship.target}."
                )

    return _report(
        errors=errors,
        warnings=warnings,
        checks={
            "zip_integrity": not any(item.startswith("ZIP") for item in errors),
            "xml_parts": xml_parts,
            "relationships": relationship_count,
            "external_relationships": external_count,
        },
    )


def validate_semantic(package: OpcPackage) -> dict[str, Any]:
    """Validate Word field balance, comments, bookmarks, and revisions."""

    errors: list[str] = []
    warnings: list[str] = []
    field_count = 0
    comments: set[str] = set()
    comment_starts: set[str] = set()
    comment_ends: set[str] = set()
    comment_references: set[str] = set()
    bookmark_starts: set[str] = set()
    bookmark_ends: set[str] = set()
    revision_count = 0

    try:
        stories = discover_stories(package)
    except RavenError as exc:
        errors.append(f"Story discovery failed: {exc}")
        stories = []

    for story in stories:
        try:
            root = package.read_xml(story.part_name)
        except RavenError as exc:
            errors.append(f"{story.part_name}: {exc}")
            continue
        paragraphs = root.findall(".//w:p", namespaces=NS)
        fields = parse_complex_fields(paragraphs, part_name=story.part_name)
        field_count += len(fields)
        errors.extend(field_balance_errors(paragraphs, part_name=story.part_name))

        for element in root.iter():
            value = element.get(qn("id"))
            if element.tag == qn("comment"):
                if value is None:
                    errors.append(f"{story.part_name}: comment has no w:id.")
                elif value in comments:
                    errors.append(f"{story.part_name}: duplicate comment id {value}.")
                else:
                    comments.add(value)
            elif element.tag == qn("commentRangeStart") and value is not None:
                comment_starts.add(value)
            elif element.tag == qn("commentRangeEnd") and value is not None:
                comment_ends.add(value)
            elif element.tag == qn("commentReference") and value is not None:
                comment_references.add(value)
            elif element.tag == qn("bookmarkStart") and value is not None:
                bookmark_starts.add(value)
            elif element.tag == qn("bookmarkEnd") and value is not None:
                bookmark_ends.add(value)
            elif element.tag in {
                f"{{{W_NS}}}ins",
                f"{{{W_NS}}}del",
                f"{{{W_NS}}}moveFrom",
                f"{{{W_NS}}}moveTo",
            }:
                revision_count += 1
                if value is None:
                    errors.append(f"{story.part_name}: revision has no w:id.")
                if element.get(qn("author")) is None:
                    warnings.append(
                        f"{story.part_name}: revision {value or '?'} has no author."
                    )

    missing_comment_bodies = (
        comment_starts | comment_ends | comment_references
    ) - comments
    for comment_id in sorted(missing_comment_bodies):
        errors.append(f"Comment marker {comment_id} has no comment body.")
    for comment_id in sorted(comments - comment_references):
        warnings.append(f"Comment {comment_id} has no in-document reference.")
    for comment_id in sorted(comment_starts ^ comment_ends):
        errors.append(f"Comment range {comment_id} is not balanced.")
    for bookmark_id in sorted(bookmark_starts ^ bookmark_ends):
        errors.append(f"Bookmark {bookmark_id} is not balanced.")

    return _report(
        errors=errors,
        warnings=warnings,
        checks={
            "stories": len(stories),
            "complex_fields": field_count,
            "comments": len(comments),
            "revisions": revision_count,
            "bookmarks": len(bookmark_starts | bookmark_ends),
        },
    )


def validate_semantics(package: OpcPackage) -> dict[str, Any]:
    """Compatibility alias for semantic validation."""

    return validate_semantic(package)


def _json_payload(
    instruction: str, *, allowed_trailing: tuple[str, ...] = ()
) -> tuple[dict[str, Any] | None, str | None]:
    start = instruction.find("{")
    if start < 0:
        return None, "has no JSON object"
    decoder = json.JSONDecoder()
    try:
        value, end = decoder.raw_decode(instruction[start:])
    except json.JSONDecodeError as exc:
        return None, f"contains malformed JSON: {exc.msg}"
    trailing = instruction[start + end :].strip()
    if trailing and trailing not in allowed_trailing:
        return None, "has trailing content after its JSON object"
    if not isinstance(value, dict):
        return None, "JSON payload is not an object"
    return value, None


def _validate_citation_payload(payload: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    citation_items = payload.get("citationItems")
    if not isinstance(citation_items, list) or not citation_items:
        errors.append("citationItems must be a non-empty array")
        return errors
    for index, item in enumerate(citation_items):
        if not isinstance(item, dict):
            errors.append(f"citationItems[{index}] is not an object")
            continue
        if "id" not in item and not item.get("uris") and not item.get("itemData"):
            errors.append(
                f"citationItems[{index}] has no id, uris, or embedded itemData"
            )
        if "uris" in item and not isinstance(item["uris"], list):
            errors.append(f"citationItems[{index}].uris is not an array")
        if "itemData" in item and not isinstance(item["itemData"], dict):
            errors.append(f"citationItems[{index}].itemData is not an object")
    properties = payload.get("properties")
    if properties is not None and not isinstance(properties, dict):
        errors.append("properties is not an object")
    return errors


def validate_zotero(package: OpcPackage) -> dict[str, Any]:
    """Validate Zotero field JSON and chunked document preferences."""

    errors: list[str] = []
    warnings: list[str] = []
    citation_count = 0
    bibliography_count = 0
    preference_chunks: dict[int, str] = {}

    try:
        stories = discover_stories(package)
    except RavenError as exc:
        return _report(
            errors=[f"Story discovery failed: {exc}"],
            warnings=[],
            checks={
                "citations": 0,
                "bibliographies": 0,
                "preference_chunks": 0,
            },
        )

    for story in stories:
        try:
            root = package.read_xml(story.part_name)
        except RavenError as exc:
            errors.append(f"{story.part_name}: {exc}")
            continue
        fields = parse_complex_fields(
            root.findall(".//w:p", namespaces=NS),
            part_name=story.part_name,
        )
        for field in fields:
            instruction = field.instruction.strip()
            if any(marker in instruction for marker in _CITATION_MARKERS):
                citation_count += 1
                payload, issue = _json_payload(instruction)
                if issue is not None or payload is None:
                    errors.append(
                        f"{story.part_name} citation field {citation_count} {issue}."
                    )
                    continue
                for detail in _validate_citation_payload(payload):
                    errors.append(
                        f"{story.part_name} citation field {citation_count}: {detail}."
                    )
            elif "ZOTERO_BIBL" in instruction:
                bibliography_count += 1
                if "{" in instruction:
                    _, issue = _json_payload(
                        instruction, allowed_trailing=("CSL_BIBLIOGRAPHY",)
                    )
                    if issue is not None:
                        errors.append(
                            f"{story.part_name} bibliography field {issue}."
                        )

    if package.has_part("docProps/custom.xml"):
        try:
            custom = package.read_xml("docProps/custom.xml")
            for prop in custom.findall(
                f"{{{CUSTOM_PROPERTIES_NS}}}property"
            ):
                match = _PREFERENCE_PATTERN.fullmatch(prop.get("name", ""))
                if match is None:
                    continue
                index = int(match.group(1))
                if index in preference_chunks:
                    errors.append(f"Duplicate ZOTERO_PREF_{index} property.")
                preference_chunks[index] = "".join(prop.itertext())
        except RavenError as exc:
            errors.append(f"Unable to read Zotero document preferences: {exc}")

    if preference_chunks:
        indexes = sorted(preference_chunks)
        expected = list(range(1, indexes[-1] + 1))
        if indexes != expected:
            errors.append(
                "Zotero preference chunks are not contiguous from ZOTERO_PREF_1."
            )
        combined = "".join(preference_chunks[index] for index in indexes)
        try:
            preference_root = etree.fromstring(
                combined.encode("utf-8"),
                parser=etree.XMLParser(
                    resolve_entities=False,
                    no_network=True,
                    load_dtd=False,
                    recover=False,
                ),
            )
        except (UnicodeEncodeError, etree.XMLSyntaxError) as exc:
            errors.append(f"Combined Zotero preferences contain invalid XML: {exc}.")
        else:
            if etree.QName(preference_root).localname not in {"data", "document-data"}:
                errors.append("Zotero preference root must be data.")
            styles = [
                child
                for child in preference_root
                if etree.QName(child).localname == "style"
            ]
            if len(styles) != 1 or not styles[0].get("id"):
                errors.append("Zotero preferences must contain one style with an id.")
            field_types = [
                item.get("value")
                for child in preference_root
                if etree.QName(child).localname == "prefs"
                for item in child
                if etree.QName(item).localname == "pref"
                and item.get("name") == "fieldType"
            ]
            if field_types != ["Field"]:
                errors.append("Zotero preference fieldType must be Field.")
    elif citation_count:
        warnings.append("Zotero citations are present without preference fields.")

    return _report(
        errors=errors,
        warnings=warnings,
        checks={
            "citations": citation_count,
            "bibliographies": bibliography_count,
            "preference_chunks": len(preference_chunks),
        },
    )


def validate_document(package: OpcPackage) -> dict[str, Any]:
    """Run all validation layers and return one full report."""

    package_report = validate_package(package)
    semantic_report = validate_semantic(package)
    zotero_report = validate_zotero(package)
    reports = {
        "package": package_report,
        "semantic": semantic_report,
        "zotero": zotero_report,
    }
    errors = [
        f"{name}: {message}"
        for name, report in reports.items()
        for message in report["errors"]
    ]
    warnings = [
        f"{name}: {message}"
        for name, report in reports.items()
        for message in report["warnings"]
    ]
    return {
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        **reports,
    }


def validate_all(package: OpcPackage) -> dict[str, Any]:
    """Compatibility alias for full document validation."""

    return validate_document(package)


def raise_for_failed_validation(report: Mapping[str, Any]) -> None:
    """Raise the sole validation-layer RavenError for a failed full report."""

    if bool(report.get("valid")):
        return
    errors = report.get("errors")
    if isinstance(errors, list):
        detail = "; ".join(str(item) for item in errors[:5])
    else:
        detail = "Validation failed."
    raise RavenError(
        ErrorCode.VALIDATION_FAILED,
        detail or "Validation failed.",
        stage="validation",
        remediation="Inspect the validation report and repair every reported error.",
    )


def require_valid(report: Mapping[str, Any]) -> None:
    """Compatibility alias for raising on a failed full validation report."""

    raise_for_failed_validation(report)


def raise_for_validation(report: Mapping[str, Any]) -> None:
    """Compatibility alias for raising on a failed full validation report."""

    raise_for_failed_validation(report)


def ensure_valid(package: OpcPackage) -> dict[str, Any]:
    """Run full validation, raise on failure, and return the successful report."""

    report = validate_document(package)
    raise_for_failed_validation(report)
    return report


__all__ = [
    "ensure_valid",
    "raise_for_failed_validation",
    "raise_for_validation",
    "require_valid",
    "validate_all",
    "validate_document",
    "validate_package",
    "validate_semantic",
    "validate_semantics",
    "validate_zotero",
]
