"""Native Zotero field payloads, document preferences, and citation management."""

from __future__ import annotations

import copy
import html
import json
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, cast

from lxml import etree

from raven_mcp.config import Settings, settings
from raven_mcp.errors import ErrorCode, RavenError
from raven_mcp.schemas import CitationItemInput, DocumentLocator, StoryKind

from .fields import (
    BODY_PART,
    ENDNOTES_PART,
    FOOTNOTES_PART,
    ComplexField,
    Position,
    append_field_paragraphs,
    find_unique_field,
    insert_complex_field,
    remove_complex_field,
    replace_complex_field,
    resolve_story_part,
    scan_complex_fields,
)

ZOTERO_CITATION_MARKER = "ADDIN ZOTERO_ITEM"
ZOTERO_CITATION_TYPE = "CSL_CITATION"
ZOTERO_BIBLIOGRAPHY_MARKER = "ADDIN ZOTERO_BIBL"
ZOTERO_CITATION_PREFIX = f"{ZOTERO_CITATION_MARKER} {ZOTERO_CITATION_TYPE}"
ZOTERO_BIBLIOGRAPHY_PREFIX = ZOTERO_BIBLIOGRAPHY_MARKER
CSL_CITATION_SCHEMA = (
    "https://github.com/citation-style-language/schema/raw/master/csl-citation.json"
)
BIBLIOGRAPHY_PLACEHOLDER = "[Bibliography: refresh with Zotero]"
PREFERENCE_PREFIX = "ZOTERO_PREF_"
PREFERENCE_DATA_VERSION = "3"

CUSTOM_PART = "docProps/custom.xml"
ROOT_RELS_PART = "_rels/.rels"
CONTENT_TYPES_PART = "[Content_Types].xml"

CUSTOM_NS = "http://schemas.openxmlformats.org/officeDocument/2006/custom-properties"
VT_NS = "http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes"
REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
CONTENT_TYPES_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
CUSTOM_REL_TYPE = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/"
    "custom-properties"
)
CUSTOM_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument.custom-properties+xml"
)
CUSTOM_FMTID = "{D5CDD505-2E9C-101B-9397-08002B2CF9AE}"

_PREF_RE = re.compile(r"^ZOTERO_PREF_(\d+)$")
_ITEM_RE = re.compile(r"\bZOTERO_ITEM\b", re.IGNORECASE)
_BIB_RE = re.compile(r"\bZOTERO_BIBL\b", re.IGNORECASE)
_CSL_RE = re.compile(r"\bCSL_CITATION\b", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]*>")


class OpcPackageLike(Protocol):
    """Structural subset used from the DOCX OPC package."""

    def read_bytes(self, part: str) -> bytes: ...

    def read_xml(self, part: str) -> etree._Element: ...

    def set_bytes(self, part: str, data: bytes) -> None: ...

    def set_xml(self, part: str, root: etree._Element) -> None: ...


@dataclass(slots=True)
class ParsedZoteroInstruction:
    """Tolerantly parsed Zotero field code."""

    kind: str
    payload: dict[str, Any]
    legacy: bool = False


@dataclass(slots=True)
class _StoryDocument:
    story: StoryKind
    part: str
    root: etree._Element
    fields: list[ComplexField]
    warnings: list[str]


def _local_name(element: etree._Element) -> str:
    return etree.QName(element).localname


def _json_object_after(instruction: str, start: int) -> dict[str, Any] | None:
    object_start = instruction.find("{", start)
    if object_start < 0:
        return None
    try:
        value, _ = json.JSONDecoder().raw_decode(instruction[object_start:])
    except json.JSONDecodeError:
        return None
    return cast(dict[str, Any], value) if isinstance(value, dict) else None


def parse_zotero_instruction(instruction: str) -> ParsedZoteroInstruction | None:
    """Parse current and legacy Zotero citation or bibliography instructions."""

    bibliography = _BIB_RE.search(instruction)
    if bibliography:
        payload = _json_object_after(instruction, bibliography.end()) or {}
        return ParsedZoteroInstruction("bibliography", payload, legacy=False)

    item = _ITEM_RE.search(instruction)
    csl = _CSL_RE.search(instruction)
    marker = item or csl
    if marker is None:
        return None
    payload = _json_object_after(instruction, marker.end())
    if payload is None:
        return None
    current_type = (
        _CSL_RE.search(
            instruction,
            item.end(),
            instruction.find("{", item.end()),
        )
        if item is not None
        else None
    )
    return ParsedZoteroInstruction(
        "citation",
        payload,
        legacy=current_type is None,
    )


def parse_citation_instruction(instruction: str) -> dict[str, Any] | None:
    """Return a citation payload, accepting Zotero's legacy field prefix."""

    parsed = parse_zotero_instruction(instruction)
    return parsed.payload if parsed is not None and parsed.kind == "citation" else None


def parse_bibliography_instruction(instruction: str) -> dict[str, Any] | None:
    """Return bibliography metadata from a Zotero bibliography field."""

    parsed = parse_zotero_instruction(instruction)
    return parsed.payload if parsed is not None and parsed.kind == "bibliography" else None


def _plain_formatted(value: str) -> str:
    return html.unescape(_TAG_RE.sub("", value)).strip()


def fallback_citation_text(items: Sequence[CitationItemInput | Mapping[str, Any]]) -> str:
    """Return a deterministic placeholder, not an asserted CSL rendering."""

    keys: list[str] = []
    for item in items:
        if isinstance(item, CitationItemInput):
            keys.append(item.item_key)
        else:
            value = item.get("item_key", item.get("id", item.get("key", "?")))
            keys.append(str(value))
    return f"[Citation: {'; '.join(keys)}]"


def _item_values(item: CitationItemInput | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(item, CitationItemInput):
        return {
            "item_key": item.item_key,
            "uri": item.uri,
            "csl_json": item.csl_json,
            "locator": item.locator,
            "label": item.label,
            "prefix": item.prefix,
            "suffix": item.suffix,
            "suppress_author": item.suppress_author,
            "author_only": item.author_only,
        }
    return dict(item)


def _extended_item(
    item: CitationItemInput | Mapping[str, Any],
    previous: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    values = _item_values(item)
    key_value = values.get("item_key", values.get("id", values.get("key")))
    if key_value is None or str(key_value) == "":
        raise RavenError(
            ErrorCode.INVALID_REQUEST,
            "Each citation item requires item_key.",
            stage="citation.payload",
            remediation="Provide a Zotero item key for every citation item.",
        )
    key = str(key_value)
    result = copy.deepcopy(dict(previous)) if previous is not None else {}
    result["id"] = key

    uri = values.get("uri")
    if uri is None and "uris" in values:
        raw_uris = values.get("uris")
        result["uris"] = (
            [str(value) for value in raw_uris]
            if isinstance(raw_uris, Sequence) and not isinstance(raw_uris, (str, bytes))
            else []
        )
    else:
        result["uris"] = [str(uri)] if uri else []

    item_data = values.get("csl_json", values.get("itemData"))
    result["itemData"] = copy.deepcopy(item_data) if isinstance(item_data, Mapping) else {}

    optional_strings = {
        "locator": values.get("locator"),
        "label": values.get("label"),
        "prefix": values.get("prefix"),
        "suffix": values.get("suffix"),
    }
    for name, value in optional_strings.items():
        if value is not None and str(value) != "":
            result[name] = str(value)
        else:
            result.pop(name, None)

    boolean_values = {
        "suppress-author": values.get(
            "suppress_author", values.get("suppress-author", False)
        ),
        "author-only": values.get("author_only", values.get("author-only", False)),
    }
    for name, value in boolean_values.items():
        if bool(value):
            result[name] = True
        else:
            result.pop(name, None)
    return result


def build_citation_payload(
    items: Sequence[CitationItemInput | Mapping[str, Any]],
    *,
    citation_id: str | None = None,
    formatted: str | None = None,
    existing: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build extended CSL JSON while preserving unknown existing members."""

    if not items:
        raise RavenError(
            ErrorCode.INVALID_REQUEST,
            "At least one citation item is required.",
            stage="citation.payload",
        )
    payload = copy.deepcopy(dict(existing)) if existing is not None else {}
    existing_items = payload.get("citationItems")
    previous_by_id: dict[str, Mapping[str, Any]] = {}
    if isinstance(existing_items, list):
        previous_by_id = {
            str(value.get("id")): value
            for value in existing_items
            if isinstance(value, Mapping) and value.get("id") is not None
        }
    new_items: list[dict[str, Any]] = []
    for item in items:
        values = _item_values(item)
        item_id = values.get("item_key", values.get("id", values.get("key")))
        previous = previous_by_id.get(str(item_id))
        new_items.append(_extended_item(item, previous))

    payload["citationID"] = citation_id or str(payload.get("citationID") or uuid.uuid4().hex)
    payload["citationItems"] = new_items
    payload["schema"] = CSL_CITATION_SCHEMA
    properties = payload.get("properties")
    properties = copy.deepcopy(dict(properties)) if isinstance(properties, Mapping) else {}
    rendered = formatted if formatted is not None else fallback_citation_text(items)
    properties["formattedCitation"] = rendered
    properties["plainCitation"] = _plain_formatted(rendered)
    properties.setdefault("noteIndex", 0)
    payload["properties"] = properties
    return payload


def build_citation_instruction(payload: Mapping[str, Any]) -> str:
    """Serialize a canonical current Zotero citation instruction."""

    try:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=False,
        )
    except (TypeError, ValueError) as exc:
        raise RavenError(
            ErrorCode.INVALID_REQUEST,
            "Citation itemData must contain JSON-serializable values.",
            stage="citation.payload",
        ) from exc
    return f" {ZOTERO_CITATION_PREFIX} {encoded} "


def build_bibliography_instruction(
    existing: Mapping[str, Any] | None = None,
) -> str:
    """Serialize a canonical Zotero bibliography field instruction."""

    payload: dict[str, Any] = (
        copy.deepcopy(dict(existing))
        if existing is not None
        else {"uncited": [], "omitted": [], "custom": []}
    )
    payload.setdefault("uncited", [])
    payload.setdefault("omitted", [])
    payload.setdefault("custom", [])
    try:
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise RavenError(
            ErrorCode.ZOTERO_INCOMPATIBLE,
            "Bibliography field metadata is not JSON-serializable.",
            stage="citation.bibliography",
            remediation="Refresh the bibliography in Zotero and retry.",
        ) from exc
    return f" {ZOTERO_BIBLIOGRAPHY_PREFIX} {encoded} CSL_BIBLIOGRAPHY "


def chunk_utf16(value: str, limit: int = 255) -> list[str]:
    """Split text by UTF-16 code units without splitting supplementary characters."""

    if limit < 2:
        raise ValueError("UTF-16 chunk limit must be at least 2")
    if not value:
        return [""]
    chunks: list[str] = []
    current: list[str] = []
    units = 0
    index = 0
    while index < len(value):
        character = value[index]
        codepoint = ord(character)
        if 0xD800 <= codepoint <= 0xDBFF and index + 1 < len(value):
            following = value[index + 1]
            if 0xDC00 <= ord(following) <= 0xDFFF:
                token = character + following
                token_units = 2
                index += 2
            else:
                token = character
                token_units = 1
                index += 1
        else:
            token = character
            token_units = 2 if codepoint > 0xFFFF else 1
            index += 1
        if current and units + token_units > limit:
            chunks.append("".join(current))
            current = []
            units = 0
        current.append(token)
        units += token_units
    if current:
        chunks.append("".join(current))
    return chunks


class _PackageAdapter:
    def __init__(self, package: OpcPackageLike | Any) -> None:
        self.package = package

    def _has_part(self, part: str) -> bool | None:
        checker = getattr(self.package, "has_part", None)
        if not callable(checker):
            return None
        return bool(checker(part))

    @staticmethod
    def _missing_part_error(error: RavenError) -> bool:
        message = error.message.lower()
        return error.code == ErrorCode.FILE_NOT_FOUND or (
            error.code == ErrorCode.UNSUPPORTED_DOCUMENT
            and ("does not exist" in message or "is missing" in message)
        )

    @staticmethod
    def _root(value: Any) -> etree._Element:
        if isinstance(value, etree._ElementTree):
            return value.getroot()
        if isinstance(value, etree._Element):
            return value
        if isinstance(value, (bytes, bytearray, memoryview, str)):
            return etree.fromstring(
                bytes(value) if not isinstance(value, str) else value.encode(),
                parser=etree.XMLParser(resolve_entities=False, no_network=True),
            )
        getroot = getattr(value, "getroot", None)
        if callable(getroot):
            root = getroot()
            if isinstance(root, etree._Element):
                return root
        raise TypeError(f"Unsupported XML value: {type(value).__name__}")

    def read_optional_bytes(self, part: str) -> bytes | None:
        if self._has_part(part) is False:
            return None
        reader = getattr(self.package, "read_bytes", None)
        if callable(reader):
            for candidate in (part, f"/{part}"):
                try:
                    value = reader(candidate)
                except (KeyError, FileNotFoundError):
                    continue
                except RavenError as exc:
                    if self._missing_part_error(exc):
                        continue
                    raise
                if isinstance(value, str):
                    return value.encode()
                return bytes(value)
            return None
        xml_reader = getattr(self.package, "read_xml", None)
        if callable(xml_reader):
            try:
                root = self._root(xml_reader(part))
            except (KeyError, FileNotFoundError):
                return None
            except RavenError as exc:
                if self._missing_part_error(exc):
                    return None
                raise
            return etree.tostring(root, encoding="utf-8", xml_declaration=True)
        raise TypeError("OpcPackage must provide read_bytes() or read_xml().")

    def read_xml(self, part: str, *, required: bool = True) -> etree._Element | None:
        xml_reader = getattr(self.package, "read_xml", None)
        root: etree._Element | None = None
        if callable(xml_reader) and self._has_part(part) is not False:
            for candidate in (part, f"/{part}"):
                try:
                    root = self._root(xml_reader(candidate))
                except (KeyError, FileNotFoundError):
                    continue
                except RavenError as exc:
                    if self._missing_part_error(exc):
                        continue
                    raise
                break
        if root is None:
            data = self.read_optional_bytes(part)
            if data is not None:
                try:
                    root = etree.fromstring(
                        data,
                        parser=etree.XMLParser(resolve_entities=False, no_network=True),
                    )
                except etree.XMLSyntaxError as exc:
                    raise RavenError(
                        ErrorCode.UNSUPPORTED_DOCUMENT,
                        f"Invalid XML in package part {part}.",
                        stage="citation.package.read",
                    ) from exc
        if root is None:
            if required:
                raise RavenError(
                    ErrorCode.UNSUPPORTED_DOCUMENT,
                    f"Required package part {part} is missing.",
                    stage="citation.package.read",
                )
            return None
        return copy.deepcopy(root)

    @staticmethod
    def _serialize(root: etree._Element) -> bytes:
        return etree.tostring(
            root,
            encoding="utf-8",
            xml_declaration=True,
            standalone=True,
        )

    def _write(self, part: str, data: bytes) -> None:
        byte_writer = getattr(self.package, "set_bytes", None)
        if callable(byte_writer):
            byte_writer(part, data)
            return
        xml_writer = getattr(self.package, "set_xml", None)
        if callable(xml_writer):
            xml_writer(part, self._root(data))
            return
        raise TypeError("OpcPackage must provide set_bytes() or set_xml().")

    def _delete(self, part: str) -> None:
        for name in ("delete_part", "remove_part", "delete"):
            method = getattr(self.package, name, None)
            if callable(method):
                method(part)
                return

    def commit(self, roots: Mapping[str, etree._Element]) -> list[str]:
        """Write staged roots and restore prior bytes if a package write fails."""

        encoded = {part: self._serialize(root) for part, root in roots.items()}
        snapshots = {part: self.read_optional_bytes(part) for part in encoded}
        written: list[str] = []
        try:
            for part, data in encoded.items():
                self._write(part, data)
                written.append(part)
        except Exception:
            for part in reversed(written):
                original = snapshots[part]
                if original is None:
                    self._delete(part)
                else:
                    self._write(part, original)
            raise
        return sorted(written)


def _custom_root(adapter: _PackageAdapter) -> etree._Element:
    root = adapter.read_xml(CUSTOM_PART, required=False)
    if root is not None:
        return root
    return etree.Element(
        f"{{{CUSTOM_NS}}}Properties",
        nsmap={None: CUSTOM_NS, "vt": VT_NS},
    )


def _preference_properties(
    custom_root: etree._Element,
) -> tuple[list[tuple[int, str]], list[str]]:
    values: list[tuple[int, str]] = []
    warnings: list[str] = []
    for prop in list(custom_root):
        match = _PREF_RE.match(prop.get("name", ""))
        if not match:
            continue
        value = "".join(prop.itertext())
        values.append((int(match.group(1)), value))
    values.sort()
    if values:
        expected = list(range(1, len(values) + 1))
        actual = [index for index, _ in values]
        if actual != expected:
            warnings.append(
                f"Zotero preference chunks are non-contiguous: {actual!r}."
            )
    return values, warnings


def _parse_preference_xml(
    custom_root: etree._Element,
) -> tuple[etree._Element | None, str, list[str]]:
    chunks, warnings = _preference_properties(custom_root)
    raw = "".join(value for _, value in chunks)
    if not chunks:
        return None, "", warnings
    try:
        root = etree.fromstring(
            raw.encode("utf-8"),
            parser=etree.XMLParser(resolve_entities=False, no_network=True),
        )
    except (UnicodeEncodeError, etree.XMLSyntaxError):
        warnings.append("Zotero document preferences contain invalid XML.")
        return None, raw, warnings
    return root, raw, warnings


def _child(root: etree._Element, local_name: str) -> etree._Element | None:
    return next((item for item in root if _local_name(item) == local_name), None)


def _preference_value(root: etree._Element, name: str) -> str | None:
    prefs = _child(root, "prefs")
    if prefs is None:
        return None
    for item in prefs:
        if _local_name(item) == "pref" and item.get("name", "").lower() == name.lower():
            return item.get("value")
    return None


def _inspect_preferences(adapter: _PackageAdapter) -> dict[str, Any]:
    custom = adapter.read_xml(CUSTOM_PART, required=False)
    if custom is None:
        return {
            "exists": False,
            "data_version": None,
            "style": None,
            "locale": None,
            "session": None,
            "has_bibliography": False,
            "field_type": None,
            "chunks": 0,
            "warnings": [],
        }
    root, raw, warnings = _parse_preference_xml(custom)
    chunks, _ = _preference_properties(custom)
    if root is None:
        return {
            "exists": bool(chunks),
            "data_version": None,
            "style": None,
            "locale": None,
            "session": None,
            "has_bibliography": False,
            "field_type": None,
            "chunks": len(chunks),
            "raw_xml": raw,
            "warnings": warnings,
        }
    style = _child(root, "style")
    session = _child(root, "session")
    has_bibliography = (
        style is not None
        and style.get("hasBibliography", "0").lower() in {"1", "true", "yes"}
    )
    if _local_name(root) != "data":
        warnings.append(f"Legacy Zotero preference root {_local_name(root)!r} detected.")
    return {
        "exists": True,
        "data_version": root.get("data-version"),
        "style": style.get("id") if style is not None else None,
        "locale": style.get("locale") if style is not None else None,
        "session": session.get("id") if session is not None else None,
        "has_bibliography": has_bibliography,
        "field_type": _preference_value(root, "fieldType"),
        "chunks": len(chunks),
        "raw_xml": raw,
        "warnings": warnings,
    }


def _preference_document(
    existing: etree._Element | None,
    *,
    style: str,
    locale: str,
    session: str,
    has_bibliography: bool,
) -> etree._Element:
    if existing is not None and _local_name(existing) == "data":
        root = copy.deepcopy(existing)
    else:
        root = etree.Element("data")
    root.set("data-version", PREFERENCE_DATA_VERSION)

    session_node = _child(root, "session")
    if session_node is None:
        session_node = etree.Element("session")
        root.insert(0, session_node)
    session_node.set("id", session)

    style_node = _child(root, "style")
    if style_node is None:
        style_node = etree.Element("style")
        root.insert(1 if len(root) else 0, style_node)
    style_node.set("id", style)
    style_node.set("locale", locale)
    style_node.set("hasBibliography", "1" if has_bibliography else "0")
    style_node.setdefault("bibliographyStyleHasBeenSet", "0")

    prefs = _child(root, "prefs")
    if prefs is None:
        prefs = etree.SubElement(root, "prefs")
    field_type = None
    for item in prefs:
        if _local_name(item) == "pref" and item.get("name", "").lower() == "fieldtype":
            field_type = item
            break
    if field_type is None:
        field_type = etree.SubElement(prefs, "pref")
        field_type.set("name", "fieldType")
    field_type.set("value", "Field")
    return root


def _next_pid(used: set[int]) -> int:
    pid = 2
    while pid in used:
        pid += 1
    used.add(pid)
    return pid


def _write_preference_properties(
    custom: etree._Element,
    preference_xml: str,
) -> None:
    for prop in list(custom):
        if _PREF_RE.match(prop.get("name", "")):
            custom.remove(prop)
    used_pids = {
        int(value)
        for prop in custom
        if (value := prop.get("pid", "")).isdigit()
    }
    for index, chunk in enumerate(chunk_utf16(preference_xml), start=1):
        prop = etree.SubElement(custom, f"{{{CUSTOM_NS}}}property")
        prop.set("fmtid", CUSTOM_FMTID)
        prop.set("pid", str(_next_pid(used_pids)))
        prop.set("name", f"{PREFERENCE_PREFIX}{index}")
        value = etree.SubElement(prop, f"{{{VT_NS}}}lpwstr")
        value.text = chunk


def _relationship_root(adapter: _PackageAdapter) -> tuple[etree._Element, bool]:
    root = adapter.read_xml(ROOT_RELS_PART, required=False)
    if root is None:
        root = etree.Element(f"{{{REL_NS}}}Relationships", nsmap={None: REL_NS})
    for relationship in root:
        if relationship.get("Type") == CUSTOM_REL_TYPE:
            if relationship.get("Target", "").lstrip("/") != CUSTOM_PART:
                relationship.set("Target", CUSTOM_PART)
                return root, True
            return root, False
    used_ids = {item.get("Id", "") for item in root}
    sequence = 1
    while f"rId{sequence}" in used_ids:
        sequence += 1
    relationship = etree.SubElement(root, f"{{{REL_NS}}}Relationship")
    relationship.set("Id", f"rId{sequence}")
    relationship.set("Type", CUSTOM_REL_TYPE)
    relationship.set("Target", CUSTOM_PART)
    return root, True


def _content_types_root(adapter: _PackageAdapter) -> tuple[etree._Element, bool]:
    root = adapter.read_xml(CONTENT_TYPES_PART, required=False)
    if root is None:
        root = etree.Element(
            f"{{{CONTENT_TYPES_NS}}}Types", nsmap={None: CONTENT_TYPES_NS}
        )
    for override in root:
        if override.get("PartName", "").lstrip("/") == CUSTOM_PART:
            if override.get("ContentType") != CUSTOM_CONTENT_TYPE:
                override.set("ContentType", CUSTOM_CONTENT_TYPE)
                return root, True
            return root, False
    override = etree.SubElement(root, f"{{{CONTENT_TYPES_NS}}}Override")
    override.set("PartName", f"/{CUSTOM_PART}")
    override.set("ContentType", CUSTOM_CONTENT_TYPE)
    return root, True


def _preference_edits(
    adapter: _PackageAdapter,
    *,
    style: str,
    locale: str,
    session: str | None,
    has_bibliography: bool,
) -> tuple[dict[str, etree._Element], dict[str, Any]]:
    custom = _custom_root(adapter)
    existing, _, warnings = _parse_preference_xml(custom)
    inspected = _inspect_preferences(adapter)
    effective_session = (
        session
        or cast(str | None, inspected.get("session"))
        or uuid.uuid4().hex
    )
    preferences = _preference_document(
        existing,
        style=style,
        locale=locale,
        session=effective_session,
        has_bibliography=has_bibliography,
    )
    preference_xml = etree.tostring(preferences, encoding="unicode")
    _write_preference_properties(custom, preference_xml)

    edits: dict[str, etree._Element] = {CUSTOM_PART: custom}
    relationships, relationship_changed = _relationship_root(adapter)
    if relationship_changed:
        edits[ROOT_RELS_PART] = relationships
    content_types, content_types_changed = _content_types_root(adapter)
    if content_types_changed:
        edits[CONTENT_TYPES_PART] = content_types
    info = {
        "exists": True,
        "data_version": PREFERENCE_DATA_VERSION,
        "style": style,
        "locale": locale,
        "session": effective_session,
        "has_bibliography": has_bibliography,
        "field_type": "Field",
        "chunks": len(chunk_utf16(preference_xml)),
        "warnings": warnings,
    }
    return edits, info


class CitationManager:
    """Manage native Zotero fields and preferences in an in-memory OPC package."""

    def __init__(self, package: OpcPackageLike | Any) -> None:
        self._adapter = _PackageAdapter(package)
        self._settings: Settings = getattr(package, "settings", settings)

    def _documents(self) -> list[_StoryDocument]:
        documents: list[_StoryDocument] = []
        for story, part, required in (
            (StoryKind.BODY, BODY_PART, True),
            (StoryKind.FOOTNOTE, FOOTNOTES_PART, False),
            (StoryKind.ENDNOTE, ENDNOTES_PART, False),
        ):
            root = self._adapter.read_xml(part, required=required)
            if root is None:
                continue
            scan = scan_complex_fields(root, part=part, story=story)
            documents.append(
                _StoryDocument(story, part, root, scan.fields, scan.warnings)
            )
        return documents

    @staticmethod
    def _all_fields(documents: Sequence[_StoryDocument]) -> list[ComplexField]:
        return [field for document in documents for field in document.fields]

    @staticmethod
    def _document_for_field(
        documents: Sequence[_StoryDocument], field: ComplexField
    ) -> _StoryDocument:
        for document in documents:
            if document.part == field.part:
                return document
        raise RavenError(
            ErrorCode.INTERNAL_ERROR,
            f"Field part {field.part!r} was not loaded.",
            stage="citation.manager",
        )

    def _effective_preferences(
        self,
        current: Mapping[str, Any],
        *,
        style: str | None,
        locale: str | None,
    ) -> tuple[str, str]:
        return (
            style or str(current.get("style") or self._settings.default_csl_style),
            locale or str(current.get("locale") or self._settings.default_locale),
        )

    def list_citations(self) -> dict[str, Any]:
        """List Zotero citation and bibliography fields in all supported stories."""

        documents = self._documents()
        citations: list[dict[str, Any]] = []
        bibliographies: list[dict[str, Any]] = []
        warnings = [warning for document in documents for warning in document.warnings]
        seen_ids: set[str] = set()

        for field in self._all_fields(documents):
            parsed = parse_zotero_instruction(field.instruction)
            if parsed is None:
                if field.is_zotero_citation or field.is_zotero_bibliography:
                    warnings.append(
                        f"Unreadable Zotero instruction in {field.part}, field "
                        f"{field.ordinal}."
                    )
                continue
            base = {
                "field_id": field.field_id,
                "story": field.story.value,
                "part": field.part,
                "visible_text": field.visible_text,
                "payload": parsed.payload,
                "legacy": parsed.legacy,
            }
            if parsed.kind == "citation":
                citation_id = parsed.payload.get("citationID")
                if citation_id is None:
                    warnings.append(
                        f"Zotero citation in {field.part} has no citationID."
                    )
                else:
                    base["citation_id"] = str(citation_id)
                    if str(citation_id) in seen_ids:
                        warnings.append(f"Duplicate citationID {citation_id!r}.")
                    seen_ids.add(str(citation_id))
                if parsed.legacy:
                    warnings.append(
                        f"Citation {citation_id or field.field_id!r} uses a legacy instruction."
                    )
                citations.append(base)
            else:
                bibliographies.append(base)

        if len(bibliographies) > 1:
            warnings.append(
                f"Document contains {len(bibliographies)} managed bibliographies; one is allowed."
            )
        return {
            "citations": citations,
            "bibliography": bibliographies[0] if bibliographies else None,
            "warnings": warnings,
        }

    def insert(
        self,
        locator: DocumentLocator,
        items: Sequence[CitationItemInput | Mapping[str, Any]],
        formatted: str | None = None,
        style: str | None = None,
        locale: str | None = None,
        *,
        position: Position | None = None,
    ) -> dict[str, Any]:
        """Insert a dirty native Zotero citation field."""

        documents = self._documents()
        existing_ids = {
            str(parsed.payload["citationID"])
            for field in self._all_fields(documents)
            if (parsed := parse_zotero_instruction(field.instruction)) is not None
            and parsed.kind == "citation"
            and "citationID" in parsed.payload
        }
        citation_id = uuid.uuid4().hex
        while citation_id in existing_ids:
            citation_id = uuid.uuid4().hex
        payload = build_citation_payload(
            items,
            citation_id=citation_id,
            formatted=formatted,
        )
        visible_text = str(
            cast(Mapping[str, Any], payload["properties"]).get("plainCitation", "")
        )
        instruction = build_citation_instruction(payload)
        story, part = resolve_story_part(locator)
        document = next((item for item in documents if item.part == part), None)
        if document is None:
            raise RavenError(
                ErrorCode.UNSUPPORTED_DOCUMENT,
                f"Package part {part} is missing.",
                stage="citation.insert",
            )
        inserted = insert_complex_field(
            document.root,
            locator,
            instruction,
            visible_text,
            position=position or ("after" if locator.exact_text else "end"),
        )

        current = _inspect_preferences(self._adapter)
        effective_style, effective_locale = self._effective_preferences(
            current, style=style, locale=locale
        )
        has_bibliography = any(
            field.is_zotero_bibliography for field in self._all_fields(documents)
        )
        preference_edits, preferences = _preference_edits(
            self._adapter,
            style=effective_style,
            locale=effective_locale,
            session=cast(str | None, current.get("session")),
            has_bibliography=has_bibliography,
        )
        edits = {document.part: document.root, **preference_edits}
        changed_parts = self._adapter.commit(edits)
        warnings = list(preferences["warnings"])
        if formatted is None:
            warnings.append(
                "Inserted a deterministic placeholder; Zotero Refresh must render CSL output."
            )
        return {
            "citation_id": citation_id,
            "field": inserted.as_dict(),
            "payload": payload,
            "preferences": preferences,
            "changed_parts": changed_parts,
            "warnings": warnings,
            "story": story.value,
        }

    def update(
        self,
        citation_id: str,
        items: Sequence[CitationItemInput | Mapping[str, Any]] | None = None,
        formatted: str | None = None,
        style: str | None = None,
        locale: str | None = None,
    ) -> dict[str, Any]:
        """Update one citation while retaining unknown Zotero payload properties."""

        documents = self._documents()
        field = find_unique_field(
            self._all_fields(documents), citation_id, citations_only=True
        )
        parsed = parse_zotero_instruction(field.instruction)
        if parsed is None or parsed.kind != "citation":
            raise RavenError(
                ErrorCode.ZOTERO_INCOMPATIBLE,
                f"Field {citation_id!r} has an unreadable Zotero instruction.",
                stage="citation.update",
                remediation="Refresh the citation in Zotero and retry.",
            )
        old_items = parsed.payload.get("citationItems")
        if items is None:
            if not isinstance(old_items, list) or not old_items:
                raise RavenError(
                    ErrorCode.ZOTERO_INCOMPATIBLE,
                    f"Citation {citation_id!r} has no readable citation items.",
                    stage="citation.update",
                )
            effective_items: Sequence[CitationItemInput | Mapping[str, Any]] = [
                cast(Mapping[str, Any], value)
                for value in old_items
                if isinstance(value, Mapping)
            ]
        else:
            effective_items = items

        if formatted is None and items is None:
            properties = parsed.payload.get("properties")
            retained = (
                properties.get("formattedCitation")
                if isinstance(properties, Mapping)
                else None
            )
            effective_formatted = str(retained) if retained is not None else field.visible_text
        else:
            effective_formatted = formatted
        payload = build_citation_payload(
            effective_items,
            citation_id=citation_id,
            formatted=effective_formatted,
            existing=parsed.payload,
        )
        visible_text = str(
            cast(Mapping[str, Any], payload["properties"]).get("plainCitation", "")
        )
        replace_complex_field(
            field,
            instruction=build_citation_instruction(payload),
            visible_text=visible_text,
        )
        document = self._document_for_field(documents, field)
        edits: dict[str, etree._Element] = {document.part: document.root}
        preferences: dict[str, Any] | None = None
        if style is not None or locale is not None:
            current = _inspect_preferences(self._adapter)
            effective_style, effective_locale = self._effective_preferences(
                current, style=style, locale=locale
            )
            preference_edits, preferences = _preference_edits(
                self._adapter,
                style=effective_style,
                locale=effective_locale,
                session=cast(str | None, current.get("session")),
                has_bibliography=any(
                    item.is_zotero_bibliography
                    for item in self._all_fields(documents)
                ),
            )
            edits.update(preference_edits)
        changed_parts = self._adapter.commit(edits)
        warnings: list[str] = []
        if items is not None and formatted is None:
            warnings.append(
                "Updated with a deterministic placeholder; Zotero Refresh must render CSL output."
            )
        return {
            "citation_id": citation_id,
            "payload": payload,
            "visible_text": visible_text,
            "preferences": preferences,
            "changed_parts": changed_parts,
            "warnings": warnings,
        }

    def remove(self, citation_id: str, keep_visible: bool = False) -> dict[str, Any]:
        """Remove one citation field without tracked-change wrappers."""

        documents = self._documents()
        field = find_unique_field(
            self._all_fields(documents), citation_id, citations_only=True
        )
        document = self._document_for_field(documents, field)
        visible_text = field.visible_text
        remove_complex_field(field, keep_visible=keep_visible)
        changed_parts = self._adapter.commit({document.part: document.root})
        return {
            "citation_id": citation_id,
            "removed": True,
            "kept_visible_text": keep_visible,
            "visible_text": visible_text if keep_visible else None,
            "changed_parts": changed_parts,
            "warnings": [],
        }

    def sync_bibliography(
        self,
        locator: DocumentLocator | None = None,
        heading: str | None = "References",
        style: str | None = None,
        locale: str | None = None,
    ) -> dict[str, Any]:
        """Create or dirty the document's single managed Zotero bibliography."""

        documents = self._documents()
        bibliography_fields = [
            field
            for field in self._all_fields(documents)
            if field.is_zotero_bibliography
        ]
        if len(bibliography_fields) > 1:
            raise RavenError(
                ErrorCode.ANCHOR_AMBIGUOUS,
                "The document contains multiple managed Zotero bibliographies.",
                stage="citation.bibliography",
                remediation="Remove duplicates in Word, leaving one bibliography field.",
            )
        warnings: list[str] = []
        if bibliography_fields:
            field = bibliography_fields[0]
            existing = parse_bibliography_instruction(field.instruction)
            instruction = build_bibliography_instruction(existing)
            replace_complex_field(
                field,
                instruction=instruction,
                visible_text=BIBLIOGRAPHY_PLACEHOLDER,
            )
            document = self._document_for_field(documents, field)
            field_result = {
                **field.as_dict(),
                "instruction": instruction,
                "visible_text": BIBLIOGRAPHY_PLACEHOLDER,
            }
            if locator is not None:
                warnings.append(
                    "A bibliography already exists; locator and heading were not used."
                )
            created = False
        else:
            if locator is None:
                raise RavenError(
                    ErrorCode.INVALID_REQUEST,
                    "A locator is required to create a bibliography.",
                    stage="citation.bibliography",
                    remediation="Provide a body, footnote, or endnote paragraph locator.",
                )
            _, part = resolve_story_part(locator)
            document = next((item for item in documents if item.part == part), None)
            if document is None:
                raise RavenError(
                    ErrorCode.UNSUPPORTED_DOCUMENT,
                    f"Package part {part} is missing.",
                    stage="citation.bibliography",
                )
            instruction = build_bibliography_instruction()
            field = append_field_paragraphs(
                document.root,
                locator,
                instruction,
                BIBLIOGRAPHY_PLACEHOLDER,
                heading=heading,
            )
            field_result = field.as_dict()
            created = True

        current = _inspect_preferences(self._adapter)
        effective_style, effective_locale = self._effective_preferences(
            current, style=style, locale=locale
        )
        preference_edits, preferences = _preference_edits(
            self._adapter,
            style=effective_style,
            locale=effective_locale,
            session=cast(str | None, current.get("session")),
            has_bibliography=True,
        )
        edits = {document.part: document.root, **preference_edits}
        changed_parts = self._adapter.commit(edits)
        warnings.extend(cast(list[str], preferences["warnings"]))
        warnings.append("Zotero Refresh must generate the authoritative bibliography.")
        return {
            "created": created,
            "field": field_result,
            "preferences": preferences,
            "changed_parts": changed_parts,
            "warnings": warnings,
        }

    def set_preferences(
        self,
        *,
        style: str | None = None,
        locale: str | None = None,
        session: str | None = None,
        has_bibliography: bool | None = None,
        field_type: str = "Field",
    ) -> dict[str, Any]:
        """Write Zotero data-version 3 document preferences."""

        if field_type != "Field":
            raise RavenError(
                ErrorCode.ZOTERO_INCOMPATIBLE,
                "Native Raven citations require Zotero fieldType='Field'.",
                stage="citation.preferences",
                remediation="Use field_type='Field'.",
            )
        current = _inspect_preferences(self._adapter)
        effective_style, effective_locale = self._effective_preferences(
            current, style=style, locale=locale
        )
        bibliography = (
            any(
                field.is_zotero_bibliography
                for field in self._all_fields(self._documents())
            )
            if has_bibliography is None
            else has_bibliography
        )
        edits, preferences = _preference_edits(
            self._adapter,
            style=effective_style,
            locale=effective_locale,
            session=session or cast(str | None, current.get("session")),
            has_bibliography=bibliography,
        )
        changed_parts = self._adapter.commit(edits)
        return {
            "preferences": preferences,
            "changed_parts": changed_parts,
            "warnings": preferences["warnings"],
        }

    def inspect_preferences(self) -> dict[str, Any]:
        """Inspect Zotero custom-property chunks without mutating the package."""

        return _inspect_preferences(self._adapter)
