"""Safe, loss-conscious Open Packaging Convention support for DOCX files."""

from __future__ import annotations

import copy
import io
import os
import posixpath
import re
import tempfile
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from urllib.parse import unquote, urlsplit

from lxml import etree

from raven_mcp.config import Settings
from raven_mcp.errors import ErrorCode, RavenError

CONTENT_TYPES_PART = "[Content_Types].xml"
CONTENT_TYPES_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
RELATIONSHIPS_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
OFFICE_DOCUMENT_REL = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"
)
CUSTOM_PROPERTIES_REL = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/custom-properties"
)
CUSTOM_PROPERTIES_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument.custom-properties+xml"
)
CUSTOM_PROPERTIES_NS = "http://schemas.openxmlformats.org/officeDocument/2006/custom-properties"
VT_NS = "http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes"

_STRICT_MARKERS = (
    b"http://purl.oclc.org/ooxml/",
    b"application/vnd.ms-word.document.macroenabled",
)
_FORBIDDEN_XML_MARKERS = (
    *_STRICT_MARKERS,
    b"digital-signature",
    b"vbaproject",
)
_UNSAFE_XML = re.compile(rb"<!\s*(?:DOCTYPE|ENTITY)\b", re.IGNORECASE)
_MACRO_NAMES = {
    "word/vbaproject.bin",
    "word/vbadata.xml",
    "word/activeX".casefold(),
}
_SIGNATURE_PREFIXES = ("_xmlsignatures/", "package/services/digital-signature/")
_SIGNATURE_REL_FRAGMENT = "/digital-signature/"
_SIGNATURE_CONTENT_FRAGMENT = "digital-signature"


@dataclass(frozen=True, slots=True)
class Relationship:
    """A relationship declared by an OPC relationships part."""

    source_part: str | None
    relationship_id: str
    relationship_type: str
    target: str
    target_mode: str | None = None

    @property
    def external(self) -> bool:
        return (self.target_mode or "").casefold() == "external"


@dataclass(slots=True)
class _Member:
    data: bytes
    info: zipfile.ZipInfo


def _package_error(
    code: ErrorCode,
    message: str,
    *,
    remediation: str | None = None,
) -> RavenError:
    return RavenError(code, message, stage="package", remediation=remediation)


def _normalise_part_name(part_name: str) -> str:
    raw = part_name.removeprefix("/")
    decoded = unquote(raw)
    path = PurePosixPath(decoded)
    components = decoded.split("/")
    invalid_component = any(item in {"", ".", ".."} for item in components[:-1]) or components[
        -1
    ] in {".", ".."}
    if (
        not raw
        or "\x00" in raw
        or "\\" in raw
        or raw.startswith("/")
        or decoded.startswith("/")
        or invalid_component
        or any(item in {"", ".", ".."} for item in path.parts)
        or (path.parts and ":" in path.parts[0])
    ):
        raise _package_error(
            ErrorCode.UNSAFE_PACKAGE,
            f"Unsafe OPC part name: {part_name!r}",
            remediation="Remove absolute paths, traversal components, and backslashes.",
        )
    return raw


def _safe_xml(data: bytes, part_name: str) -> etree._Element:
    if _UNSAFE_XML.search(data):
        raise _package_error(
            ErrorCode.UNSAFE_PACKAGE,
            f"DTD or entity declaration is not allowed in {part_name}.",
        )
    parser = etree.XMLParser(
        resolve_entities=False,
        no_network=True,
        load_dtd=False,
        huge_tree=False,
        recover=False,
        remove_blank_text=False,
    )
    try:
        return etree.fromstring(data, parser=parser)
    except (etree.XMLSyntaxError, ValueError) as exc:
        raise _package_error(
            ErrorCode.UNSAFE_PACKAGE,
            f"Malformed XML in {part_name}: {exc}",
        ) from exc


def _rels_part_for(source_part: str | None) -> str:
    if source_part is None:
        return "_rels/.rels"
    source = _normalise_part_name(source_part)
    parent, name = posixpath.split(source)
    return posixpath.join(parent, "_rels", f"{name}.rels")


def _source_for_rels(rels_part: str) -> str | None:
    name = _normalise_part_name(rels_part)
    if name == "_rels/.rels":
        return None
    parent, leaf = posixpath.split(name)
    if not parent.endswith("/_rels") or not leaf.endswith(".rels"):
        raise ValueError(f"Not a relationships part: {rels_part}")
    source_parent = parent.removesuffix("/_rels")
    return posixpath.join(source_parent, leaf.removesuffix(".rels"))


def resolve_relationship_target(source_part: str | None, target: str) -> str:
    """Resolve an internal relationship target to a safe package part name."""

    parsed = urlsplit(target)
    if parsed.scheme or parsed.netloc:
        raise _package_error(
            ErrorCode.UNSAFE_PACKAGE,
            f"Internal relationship has a URI target: {target!r}",
        )
    decoded = unquote(parsed.path)
    if "\\" in decoded or "\x00" in decoded:
        raise _package_error(
            ErrorCode.UNSAFE_PACKAGE,
            f"Unsafe relationship target: {target!r}",
        )
    if decoded.startswith("/"):
        candidate = decoded.removeprefix("/")
    else:
        base = "" if source_part is None else posixpath.dirname(source_part)
        candidate = posixpath.join(base, decoded)
    normalised = posixpath.normpath(candidate)
    if normalised in {"", ".", ".."} or normalised.startswith("../"):
        raise _package_error(
            ErrorCode.UNSAFE_PACKAGE,
            f"Relationship target escapes the package: {target!r}",
        )
    return _normalise_part_name(normalised)


class OpcPackage:
    """An independently editable in-memory DOCX package."""

    def __init__(
        self,
        *,
        path: Path,
        settings: Settings,
        original_bytes: bytes,
        members: dict[str, _Member],
        order: list[str],
        comment: bytes = b"",
    ) -> None:
        self.path = path
        self.settings = settings
        self._original_bytes = original_bytes
        self._members = members
        self._order = order
        self._comment = comment
        self._changed_parts: set[str] = set()
        self._removed_parts: set[str] = set()

    @classmethod
    def open(cls, path: Path, settings: Settings) -> OpcPackage:
        """Load and fully validate a DOCX without retaining a file handle."""

        resolved = settings.resolve_path(path)
        if resolved.suffix.casefold() != ".docx":
            raise _package_error(
                ErrorCode.UNSUPPORTED_DOCUMENT,
                "Only .docx packages are supported.",
            )
        try:
            size = resolved.stat().st_size
            if size > settings.max_document_bytes:
                raise _package_error(
                    ErrorCode.RESOURCE_LIMIT,
                    f"Document is {size} bytes; limit is {settings.max_document_bytes}.",
                )
            raw = resolved.read_bytes()
        except RavenError:
            raise
        except OSError as exc:
            raise _package_error(
                ErrorCode.UNSUPPORTED_DOCUMENT,
                f"Unable to read document: {exc}",
            ) from exc

        members: dict[str, _Member] = {}
        order: list[str] = []
        try:
            with zipfile.ZipFile(io.BytesIO(raw), mode="r") as archive:
                infos = archive.infolist()
                if len(infos) > settings.max_zip_members:
                    raise _package_error(
                        ErrorCode.RESOURCE_LIMIT,
                        f"Package has {len(infos)} members; limit is {settings.max_zip_members}.",
                    )
                declared_total = sum(info.file_size for info in infos)
                if declared_total > settings.max_uncompressed_bytes:
                    raise _package_error(
                        ErrorCode.RESOURCE_LIMIT,
                        "Declared uncompressed package size exceeds the configured limit.",
                    )

                actual_total = 0
                for info in infos:
                    if info.filename.startswith(("/", "\\")):
                        raise _package_error(
                            ErrorCode.UNSAFE_PACKAGE,
                            f"Absolute ZIP member path: {info.filename!r}",
                        )
                    name = _normalise_part_name(info.filename)
                    if name in members:
                        raise _package_error(
                            ErrorCode.UNSAFE_PACKAGE,
                            f"Duplicate ZIP member: {name}",
                        )
                    if info.flag_bits & 0x1:
                        raise _package_error(
                            ErrorCode.UNSAFE_PACKAGE,
                            f"Encrypted ZIP member is not supported: {name}",
                        )
                    chunks: list[bytes] = []
                    with archive.open(info, mode="r") as stream:
                        while chunk := stream.read(1024 * 1024):
                            actual_total += len(chunk)
                            if actual_total > settings.max_uncompressed_bytes:
                                raise _package_error(
                                    ErrorCode.RESOURCE_LIMIT,
                                    "Uncompressed package size exceeds the configured limit.",
                                )
                            chunks.append(chunk)
                    data = b"".join(chunks)
                    if len(data) != info.file_size:
                        raise _package_error(
                            ErrorCode.UNSAFE_PACKAGE,
                            f"ZIP member size mismatch: {name}",
                        )
                    members[name] = _Member(data=data, info=copy.copy(info))
                    order.append(name)
                comment = archive.comment
        except RavenError:
            raise
        except (
            zipfile.BadZipFile,
            NotImplementedError,
            RuntimeError,
            OSError,
            EOFError,
        ) as exc:
            raise _package_error(
                ErrorCode.UNSAFE_PACKAGE,
                f"Malformed or unsupported ZIP package: {exc}",
            ) from exc

        package = cls(
            path=resolved,
            settings=settings,
            original_bytes=raw,
            members=members,
            order=order,
            comment=comment,
        )
        package._validate_loaded_package()
        return package

    @property
    def members(self) -> Mapping[str, bytes]:
        return MappingProxyType(
            {
                name: member.data
                for name, member in self._members.items()
                if name not in self._removed_parts
            }
        )

    @property
    def changed_parts(self) -> frozenset[str]:
        return frozenset(self._changed_parts | self._removed_parts)

    def clone(self) -> OpcPackage:
        """Create an independent in-memory copy suitable for speculative edits."""

        clone = OpcPackage(
            path=self.path,
            settings=self.settings,
            original_bytes=self._original_bytes,
            members={
                name: _Member(member.data, copy.copy(member.info))
                for name, member in self._members.items()
            },
            order=list(self._order),
            comment=self._comment,
        )
        clone._changed_parts = set(self._changed_parts)
        clone._removed_parts = set(self._removed_parts)
        return clone

    def has_part(self, part_name: str) -> bool:
        name = _normalise_part_name(part_name)
        return name in self._members and name not in self._removed_parts

    def read_bytes(self, part_name: str) -> bytes:
        name = _normalise_part_name(part_name)
        member = self._members.get(name)
        if member is None or name in self._removed_parts:
            raise _package_error(
                ErrorCode.UNSUPPORTED_DOCUMENT,
                f"Package part does not exist: {name}",
            )
        return member.data

    def read_xml(self, part_name: str) -> etree._Element:
        name = _normalise_part_name(part_name)
        return _safe_xml(self.read_bytes(name), name)

    def set_bytes(self, part_name: str, data: bytes) -> None:
        name = _normalise_part_name(part_name)
        folded = name.casefold()
        if (
            folded in _MACRO_NAMES
            or folded.startswith("word/activex/")
            or "vbaproject" in folded
            or folded.startswith(_SIGNATURE_PREFIXES)
        ):
            raise _package_error(
                ErrorCode.UNSUPPORTED_DOCUMENT,
                f"Unsafe active content cannot be added: {name}",
            )
        if folded.endswith(".xml") or folded.endswith(".rels") or name == CONTENT_TYPES_PART:
            lowered_data = data.lower()
            if any(marker in lowered_data for marker in _FORBIDDEN_XML_MARKERS):
                raise _package_error(
                    ErrorCode.UNSUPPORTED_DOCUMENT,
                    "Strict OOXML, signatures, and active content are not supported.",
                )
            _safe_xml(data, name)
        if name not in self._members and len(self.members) >= self.settings.max_zip_members:
            raise _package_error(
                ErrorCode.RESOURCE_LIMIT,
                "Edited package exceeds the configured ZIP member limit.",
            )
        new_total = sum(
            len(member.data)
            for existing, member in self._members.items()
            if existing != name and existing not in self._removed_parts
        ) + len(data)
        if new_total > self.settings.max_uncompressed_bytes:
            raise _package_error(
                ErrorCode.RESOURCE_LIMIT,
                "Edited package exceeds the configured uncompressed size limit.",
            )
        if name in self._members:
            self._members[name].data = bytes(data)
        else:
            info = zipfile.ZipInfo(name)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o600 << 16
            self._members[name] = _Member(bytes(data), info)
            self._order.append(name)
        self._removed_parts.discard(name)
        self._changed_parts.add(name)

    def set_xml(
        self,
        part_name: str,
        root: etree._Element | etree._ElementTree,
    ) -> None:
        data = etree.tostring(
            root,
            encoding="UTF-8",
            xml_declaration=True,
            standalone=True,
        )
        _safe_xml(data, _normalise_part_name(part_name))
        self.set_bytes(part_name, data)

    def remove_part(self, part_name: str) -> None:
        name = _normalise_part_name(part_name)
        if name in self._members:
            self._removed_parts.add(name)
            self._changed_parts.discard(name)

    def member_info(self, part_name: str) -> zipfile.ZipInfo:
        name = _normalise_part_name(part_name)
        if name not in self._members or name in self._removed_parts:
            raise KeyError(name)
        return copy.copy(self._members[name].info)

    @property
    def content_types(self) -> Mapping[str, str]:
        defaults, overrides = self._content_type_maps()
        result: dict[str, str] = {}
        for name in self.members:
            explicit = overrides.get(f"/{name}")
            if explicit is not None:
                result[name] = explicit
                continue
            extension = (
                "rels"
                if name.endswith(".rels")
                else PurePosixPath(name).suffix.removeprefix(".").casefold()
            )
            if extension in defaults:
                result[name] = defaults[extension]
        return MappingProxyType(result)

    @property
    def content_type_defaults(self) -> Mapping[str, str]:
        defaults, _ = self._content_type_maps()
        return MappingProxyType(defaults)

    def content_type_for(self, part_name: str) -> str | None:
        return self.content_types.get(_normalise_part_name(part_name))

    def set_content_type_override(self, part_name: str, content_type: str) -> None:
        name = _normalise_part_name(part_name)
        root = self.read_xml(CONTENT_TYPES_PART)
        part_value = f"/{name}"
        query = f"{{{CONTENT_TYPES_NS}}}Override"
        for item in root.findall(query):
            if item.get("PartName") == part_value:
                if item.get("ContentType") != content_type:
                    item.set("ContentType", content_type)
                    self.set_xml(CONTENT_TYPES_PART, root)
                return
        override = etree.SubElement(root, query)
        override.set("PartName", part_value)
        override.set("ContentType", content_type)
        self.set_xml(CONTENT_TYPES_PART, root)

    def relationships(self, source_part: str | None = None) -> list[Relationship]:
        rels_name = _rels_part_for(source_part)
        if not self.has_part(rels_name):
            return []
        root = self.read_xml(rels_name)
        if root.tag != f"{{{RELATIONSHIPS_NS}}}Relationships":
            raise _package_error(
                ErrorCode.UNSAFE_PACKAGE,
                f"Invalid relationships root element in {rels_name}.",
            )
        relationships: list[Relationship] = []
        used_ids: set[str] = set()
        tag = f"{{{RELATIONSHIPS_NS}}}Relationship"
        for element in root.findall(tag):
            relationship_id = element.get("Id")
            relationship_type = element.get("Type")
            target = element.get("Target")
            if not relationship_id or not relationship_type or target is None:
                raise _package_error(
                    ErrorCode.UNSAFE_PACKAGE,
                    f"Malformed relationship in {rels_name}.",
                )
            if relationship_id in used_ids:
                raise _package_error(
                    ErrorCode.UNSAFE_PACKAGE,
                    f"Duplicate relationship id {relationship_id} in {rels_name}.",
                )
            used_ids.add(relationship_id)
            target_mode = element.get("TargetMode")
            if target_mode is not None and target_mode.casefold() != "external":
                raise _package_error(
                    ErrorCode.UNSAFE_PACKAGE,
                    f"Invalid relationship target mode in {rels_name}.",
                )
            relationships.append(
                Relationship(
                    source_part=source_part,
                    relationship_id=relationship_id,
                    relationship_type=relationship_type,
                    target=target,
                    target_mode=target_mode,
                )
            )
        return relationships

    def relationship_target(self, relationship: Relationship) -> str | None:
        if relationship.external:
            return None
        return resolve_relationship_target(
            relationship.source_part,
            relationship.target,
        )

    def external_relationships(self) -> list[Relationship]:
        result: list[Relationship] = []
        for name in self.members:
            if name == "_rels/.rels" or name.endswith(".rels"):
                try:
                    source = _source_for_rels(name)
                except ValueError:
                    continue
                result.extend(item for item in self.relationships(source) if item.external)
        return result

    def add_relationship(
        self,
        source_part: str | None,
        target_part: str,
        relationship_type: str,
        *,
        target_mode: str | None = None,
    ) -> str:
        folded_type = relationship_type.casefold()
        if _SIGNATURE_REL_FRAGMENT in folded_type or "vbaproject" in folded_type:
            raise _package_error(
                ErrorCode.UNSUPPORTED_DOCUMENT,
                "Signature and macro relationships cannot be added.",
            )
        normalized_source = _normalise_part_name(source_part) if source_part is not None else None
        rels_name = _rels_part_for(source_part)
        if target_mode is not None and target_mode.casefold() == "external":
            target_value = target_part
        else:
            target_name = _normalise_part_name(target_part)
            source_dir = "" if normalized_source is None else posixpath.dirname(normalized_source)
            target_value = posixpath.relpath(target_name, source_dir or ".")

        if self.has_part(rels_name):
            root = self.read_xml(rels_name)
        else:
            root = etree.Element(
                f"{{{RELATIONSHIPS_NS}}}Relationships",
                nsmap={None: RELATIONSHIPS_NS},
            )

        used_ids: set[str] = set()
        tag = f"{{{RELATIONSHIPS_NS}}}Relationship"
        for element in root.findall(tag):
            relationship_id = element.get("Id")
            if relationship_id:
                used_ids.add(relationship_id)
            if (
                element.get("Type") == relationship_type
                and element.get("Target") == target_value
                and element.get("TargetMode") == target_mode
            ):
                return element.get("Id", "")

        index = 1
        while f"rId{index}" in used_ids:
            index += 1
        relationship_id = f"rId{index}"
        element = etree.SubElement(root, tag)
        element.set("Id", relationship_id)
        element.set("Type", relationship_type)
        element.set("Target", target_value)
        if target_mode is not None:
            element.set("TargetMode", target_mode)
        self.set_xml(rels_name, root)
        return relationship_id

    def set_custom_property(self, name: str, value: str | int | bool | datetime) -> None:
        """Create or update a standard OPC custom document property."""

        part_name = "docProps/custom.xml"
        if self.has_part(part_name):
            root = self.read_xml(part_name)
        else:
            root = etree.Element(
                f"{{{CUSTOM_PROPERTIES_NS}}}Properties",
                nsmap={None: CUSTOM_PROPERTIES_NS, "vt": VT_NS},
            )

        properties = root.findall(f"{{{CUSTOM_PROPERTIES_NS}}}property")
        prop = next((item for item in properties if item.get("name") == name), None)
        if prop is None:
            used = {
                int(item.get("pid", "1")) for item in properties if item.get("pid", "").isdigit()
            }
            pid = 2
            while pid in used:
                pid += 1
            prop = etree.SubElement(root, f"{{{CUSTOM_PROPERTIES_NS}}}property")
            prop.set("fmtid", "{D5CDD505-2E9C-101B-9397-08002B2CF9AE}")
            prop.set("pid", str(pid))
            prop.set("name", name)
        else:
            for child in list(prop):
                prop.remove(child)

        if isinstance(value, bool):
            child = etree.SubElement(prop, f"{{{VT_NS}}}bool")
            child.text = "true" if value else "false"
        elif isinstance(value, int):
            child = etree.SubElement(prop, f"{{{VT_NS}}}i4")
            child.text = str(value)
        elif isinstance(value, datetime):
            child = etree.SubElement(prop, f"{{{VT_NS}}}filetime")
            normalized = value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
            child.text = normalized.isoformat().replace("+00:00", "Z")
        else:
            child = etree.SubElement(prop, f"{{{VT_NS}}}lpwstr")
            child.text = value

        self.set_xml(part_name, root)
        self.set_content_type_override(part_name, CUSTOM_PROPERTIES_CONTENT_TYPE)
        self.add_relationship(None, part_name, CUSTOM_PROPERTIES_REL)

    def to_bytes(self) -> bytes:
        """Serialize the current package, preserving untouched member metadata."""

        if not self.changed_parts:
            return self._original_bytes
        output = io.BytesIO()
        try:
            with zipfile.ZipFile(output, mode="w", allowZip64=True) as archive:
                archive.comment = self._comment
                for name in self._order:
                    if name in self._removed_parts:
                        continue
                    member = self._members[name]
                    info = copy.copy(member.info)
                    info.filename = name
                    archive.writestr(info, member.data)
        except (OSError, RuntimeError, ValueError, zipfile.BadZipFile) as exc:
            raise _package_error(
                ErrorCode.INTERNAL_ERROR,
                f"Unable to serialize package: {exc}",
            ) from exc
        data = output.getvalue()
        if len(data) > self.settings.max_document_bytes:
            raise _package_error(
                ErrorCode.RESOURCE_LIMIT,
                "Serialized document exceeds the configured document size limit.",
            )
        return data

    def write(self, path: Path) -> None:
        """Atomically replace an output path with the serialized package."""

        output_path = self.settings.resolve_path(path, must_exist=False)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        data = self.to_bytes()
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=output_path.parent,
                prefix=f".{output_path.name}.",
                suffix=".tmp",
                delete=False,
            ) as stream:
                temporary = Path(stream.name)
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, output_path)
        except OSError as exc:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            raise _package_error(
                ErrorCode.FILE_IN_USE,
                f"Unable to write document atomically: {exc}",
            ) from exc

    def _content_type_maps(self) -> tuple[dict[str, str], dict[str, str]]:
        root = self.read_xml(CONTENT_TYPES_PART)
        if root.tag != f"{{{CONTENT_TYPES_NS}}}Types":
            raise _package_error(
                ErrorCode.UNSAFE_PACKAGE,
                "The content types part has an invalid root element.",
            )
        defaults: dict[str, str] = {}
        overrides: dict[str, str] = {}
        for item in root:
            if item.tag == f"{{{CONTENT_TYPES_NS}}}Default":
                extension = item.get("Extension")
                content_type = item.get("ContentType")
                if extension and content_type:
                    if "/" in extension or "\\" in extension:
                        raise _package_error(
                            ErrorCode.UNSAFE_PACKAGE,
                            f"Unsafe content type extension: {extension!r}",
                        )
                    if extension.casefold() in defaults:
                        raise _package_error(
                            ErrorCode.UNSAFE_PACKAGE,
                            f"Duplicate default content type for {extension!r}.",
                        )
                    defaults[extension.casefold()] = content_type
            elif item.tag == f"{{{CONTENT_TYPES_NS}}}Override":
                part_name = item.get("PartName")
                content_type = item.get("ContentType")
                if part_name and content_type:
                    if not part_name.startswith("/"):
                        raise _package_error(
                            ErrorCode.UNSAFE_PACKAGE,
                            f"Invalid content type part name: {part_name!r}",
                        )
                    _normalise_part_name(part_name)
                    if part_name in overrides:
                        raise _package_error(
                            ErrorCode.UNSAFE_PACKAGE,
                            f"Duplicate content type override for {part_name!r}.",
                        )
                    overrides[part_name] = content_type
        return defaults, overrides

    def _validate_loaded_package(self) -> None:
        if not self.has_part(CONTENT_TYPES_PART):
            raise _package_error(
                ErrorCode.UNSUPPORTED_DOCUMENT,
                "The package has no [Content_Types].xml part.",
            )
        if not self.has_part("_rels/.rels"):
            raise _package_error(
                ErrorCode.UNSUPPORTED_DOCUMENT,
                "The package has no root relationships part.",
            )

        for name, data in self.members.items():
            folded = name.casefold()
            if (
                folded in _MACRO_NAMES
                or folded.startswith("word/activex/")
                or "vbaproject" in folded
            ):
                raise _package_error(
                    ErrorCode.UNSUPPORTED_DOCUMENT,
                    f"Macro-enabled content is not supported: {name}",
                )
            if folded.startswith(_SIGNATURE_PREFIXES):
                raise _package_error(
                    ErrorCode.UNSUPPORTED_DOCUMENT,
                    f"Signed packages are not supported: {name}",
                )
            if folded.endswith(".xml") or folded.endswith(".rels") or name == CONTENT_TYPES_PART:
                if any(marker in data.lower() for marker in _STRICT_MARKERS):
                    raise _package_error(
                        ErrorCode.UNSUPPORTED_DOCUMENT,
                        "Strict OOXML and macro-enabled content are not supported.",
                    )
                _safe_xml(data, name)

        content_types = {value.casefold() for value in self.content_types.values()}
        if any("macroenabled" in value for value in content_types):
            raise _package_error(
                ErrorCode.UNSUPPORTED_DOCUMENT,
                "Macro-enabled Office content types are not supported.",
            )
        if any(_SIGNATURE_CONTENT_FRAGMENT in value for value in content_types):
            raise _package_error(
                ErrorCode.UNSUPPORTED_DOCUMENT,
                "Digitally signed packages are not supported.",
            )

        office_targets = [
            item
            for item in self.relationships(None)
            if item.relationship_type == OFFICE_DOCUMENT_REL and not item.external
        ]
        if len(office_targets) != 1:
            raise _package_error(
                ErrorCode.UNSUPPORTED_DOCUMENT,
                "The package must contain exactly one Office document relationship.",
            )
        document_part = self.relationship_target(office_targets[0])
        if document_part is None or not self.has_part(document_part):
            raise _package_error(
                ErrorCode.UNSUPPORTED_DOCUMENT,
                "The main document relationship target is missing.",
            )
        main_type = (self.content_type_for(document_part) or "").casefold()
        if "wordprocessingml.document.main+xml" not in main_type:
            raise _package_error(
                ErrorCode.UNSUPPORTED_DOCUMENT,
                f"Unsupported main document content type: {main_type or 'missing'}",
            )

        for name in self.members:
            if name != "_rels/.rels" and not name.endswith(".rels"):
                continue
            try:
                source = _source_for_rels(name)
            except ValueError:
                continue
            for relationship in self.relationships(source):
                folded_type = relationship.relationship_type.casefold()
                if _SIGNATURE_REL_FRAGMENT in folded_type:
                    raise _package_error(
                        ErrorCode.UNSUPPORTED_DOCUMENT,
                        "Digitally signed packages are not supported.",
                    )
                if not relationship.external:
                    resolve_relationship_target(source, relationship.target)


__all__ = [
    "CONTENT_TYPES_NS",
    "CONTENT_TYPES_PART",
    "CUSTOM_PROPERTIES_CONTENT_TYPE",
    "CUSTOM_PROPERTIES_REL",
    "OFFICE_DOCUMENT_REL",
    "RELATIONSHIPS_NS",
    "OpcPackage",
    "Relationship",
    "resolve_relationship_target",
]
