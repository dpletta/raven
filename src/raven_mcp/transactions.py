"""Staged, optimistic-concurrency document transactions."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import tempfile
import threading
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import portalocker

from raven_mcp.citations.zotero import CitationManager
from raven_mcp.config import Settings, settings as default_settings
from raven_mcp.docx.opc import OpcPackage
from raven_mcp.docx.wordml import (
    add_comment,
    delete_text,
    insert_paragraph,
    insert_text,
    inspect_document,
    replace_text,
    set_alt_text,
)
from raven_mcp.errors import ErrorCode, RavenError
from raven_mcp.schemas import (
    AddComment,
    BibliographySyncRequest,
    CitationInsertRequest,
    CitationRemoveRequest,
    CitationUpdateRequest,
    CommitRequest,
    CommitResult,
    DeleteText,
    InsertParagraph,
    InsertText,
    PrepareChangesRequest,
    ReplaceText,
    SetAltText,
    TransactionPreview,
)
from raven_mcp.validation import (
    raise_for_failed_validation,
    validate_document,
    validate_package,
    validate_semantic,
    validate_zotero,
)

Mutation = Callable[[OpcPackage], tuple[dict[str, Any], list[dict[str, Any]], list[str]]]

_PLAIN_AUTHOR_YEAR = re.compile(
    r"\((?:[A-Z][\w'’-]+(?:\s+et\s+al\.)?,?\s+\d{4}[a-z]?"
    r"(?:;\s*)?)+\)"
)
_PLAIN_NARRATIVE = re.compile(r"\b[A-Z][\w'’-]+(?:\s+et\s+al\.)?\s+\(\d{4}[a-z]?\)")
_PLAIN_NUMERIC = re.compile(r"\[(?:\d+(?:\s*[-–]\s*\d+)?)(?:\s*,\s*\d+)*\]")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _owner_file(path: Path) -> Path:
    return path.with_name(f"~${path.name}")


def ensure_not_open_in_word(path: Path) -> None:
    owner = _owner_file(path)
    if owner.exists():
        raise RavenError(
            ErrorCode.FILE_IN_USE,
            f"Word owner file exists: {owner.name}",
            stage="transaction",
            retryable=True,
            remediation="Close the document in Microsoft Word and retry.",
        )


@dataclass(slots=True)
class PendingTransaction:
    transaction_id: str
    confirmation_token: str
    source_path: Path
    source_sha256: str
    package: OpcPackage
    created_at: datetime
    expires_at: datetime
    author: str
    intent: str
    operations: list[dict[str, Any]]
    semantic_diff: list[dict[str, Any]]
    warnings: list[str]
    validation: dict[str, Any]
    idempotency_key: str | None = None

    def preview(self) -> TransactionPreview:
        return TransactionPreview(
            transaction_id=self.transaction_id,
            confirmation_token=self.confirmation_token,
            source_path=str(self.source_path),
            source_sha256=self.source_sha256,
            created_at=self.created_at,
            expires_at=self.expires_at,
            operations=self.operations,
            semantic_diff=self.semantic_diff,
            warnings=self.warnings,
            validation=self.validation,
        )


class TransactionManager:
    """Prepare validated document changes and commit them atomically."""

    def __init__(self, settings: Settings = default_settings) -> None:
        self.settings = settings
        self._transactions: dict[str, PendingTransaction] = {}
        self._idempotency: dict[str, str] = {}
        self._lock = threading.RLock()

    def _cleanup(self) -> None:
        now = datetime.now(UTC)
        expired = [
            transaction_id
            for transaction_id, transaction in self._transactions.items()
            if transaction.expires_at <= now
        ]
        for transaction_id in expired:
            transaction = self._transactions.pop(transaction_id)
            if transaction.idempotency_key:
                self._idempotency.pop(transaction.idempotency_key, None)

    def open_document(
        self, document_path: str, expected_sha256: str | None = None
    ) -> tuple[Path, str, OpcPackage]:
        path = self.settings.resolve_path(document_path)
        ensure_not_open_in_word(path)
        source_sha = sha256_file(path)
        if expected_sha256 and not secrets.compare_digest(source_sha, expected_sha256):
            raise RavenError(
                ErrorCode.STALE_REVISION,
                "The document hash no longer matches the caller's revision.",
                stage="transaction.open",
                remediation="Inspect the current document and retry with its SHA-256.",
            )
        return path, source_sha, OpcPackage.open(path, self.settings)

    def inspect(self, document_path: str) -> dict[str, Any]:
        path, source_sha, package = self.open_document(document_path)
        summary, paragraphs, metadata = inspect_document(package, path, source_sha)
        return {
            "summary": summary.model_dump(mode="json"),
            "paragraphs": [paragraph.model_dump(mode="json") for paragraph in paragraphs],
            "metadata": metadata,
        }

    def validate(self, document_path: str, profile: str = "full") -> dict[str, Any]:
        _, source_sha, package = self.open_document(document_path)
        validators: dict[str, Callable[[OpcPackage], dict[str, Any]]] = {
            "package": validate_package,
            "semantic": validate_semantic,
            "zotero": validate_zotero,
            "full": validate_document,
        }
        try:
            validator = validators[profile]
        except KeyError as exc:
            raise RavenError(
                ErrorCode.INVALID_REQUEST,
                "Validation profile must be package, semantic, zotero, or full.",
                stage="validation",
            ) from exc
        return {"sha256": source_sha, "profile": profile, **validator(package)}

    def list_citations(self, document_path: str) -> dict[str, Any]:
        _, source_sha, package = self.open_document(document_path)
        result = CitationManager(package).list_citations()
        return {"sha256": source_sha, **result}

    def scan_plain_citations(self, document_path: str) -> dict[str, Any]:
        inspection = self.inspect(document_path)
        matches: list[dict[str, Any]] = []
        for paragraph in inspection["paragraphs"]:
            text = str(paragraph["text"])
            for kind, pattern in (
                ("author-year", _PLAIN_AUTHOR_YEAR),
                ("narrative", _PLAIN_NARRATIVE),
                ("numeric", _PLAIN_NUMERIC),
            ):
                for match in pattern.finditer(text):
                    matches.append(
                        {
                            "kind": kind,
                            "text": match.group(0),
                            "start": match.start(),
                            "end": match.end(),
                            "locator": paragraph["locator"],
                        }
                    )
        return {"sha256": inspection["summary"]["sha256"], "matches": matches}

    def _register(
        self,
        *,
        source_path: Path,
        source_sha: str,
        package: OpcPackage,
        author: str,
        intent: str,
        operations: list[dict[str, Any]],
        semantic_diff: list[dict[str, Any]],
        warnings: list[str],
        idempotency_key: str | None = None,
    ) -> TransactionPreview:
        validation = validate_document(package)
        raise_for_failed_validation(validation)
        now = datetime.now(UTC)
        transaction = PendingTransaction(
            transaction_id=uuid.uuid4().hex,
            confirmation_token=secrets.token_urlsafe(32),
            source_path=source_path,
            source_sha256=source_sha,
            package=package,
            created_at=now,
            expires_at=now + timedelta(seconds=self.settings.transaction_ttl_seconds),
            author=author,
            intent=intent,
            operations=operations,
            semantic_diff=semantic_diff,
            warnings=warnings + list(validation.get("warnings", [])),
            validation=validation,
            idempotency_key=idempotency_key,
        )
        with self._lock:
            self._cleanup()
            self._transactions[transaction.transaction_id] = transaction
            if idempotency_key:
                self._idempotency[idempotency_key] = transaction.transaction_id
        return transaction.preview()

    def _prepare(
        self,
        *,
        document_path: str,
        expected_sha256: str | None,
        author: str,
        intent: str,
        operations: list[dict[str, Any]],
        mutation: Mutation,
        idempotency_key: str | None = None,
    ) -> TransactionPreview:
        with self._lock:
            self._cleanup()
            if idempotency_key and idempotency_key in self._idempotency:
                existing = self._transactions.get(self._idempotency[idempotency_key])
                if existing is not None:
                    return existing.preview()

        source_path, source_sha, package = self.open_document(
            document_path, expected_sha256
        )
        staged = package.clone()
        result, semantic_diff, warnings = mutation(staged)
        operations_with_result = [
            *operations,
            {"type": "mutation_result", "result": result},
        ]
        return self._register(
            source_path=source_path,
            source_sha=source_sha,
            package=staged,
            author=author,
            intent=intent,
            operations=operations_with_result,
            semantic_diff=semantic_diff,
            warnings=warnings,
            idempotency_key=idempotency_key,
        )

    def prepare_changes(self, request: PrepareChangesRequest) -> TransactionPreview:
        if len(request.operations) > self.settings.max_operations:
            raise RavenError(
                ErrorCode.RESOURCE_LIMIT,
                f"Operation count exceeds the limit of {self.settings.max_operations}.",
                stage="transaction.prepare",
            )

        def mutate(package: OpcPackage) -> tuple[dict[str, Any], list[dict[str, Any]], list[str]]:
            diffs: list[dict[str, Any]] = []
            for index, operation in enumerate(request.operations):
                if isinstance(operation, InsertText):
                    insert_text(
                        package,
                        operation.locator,
                        operation.text,
                        position=operation.position,
                        tracked=operation.tracked,
                        author=request.author,
                    )
                    detail = {"text": operation.text, "position": operation.position}
                elif isinstance(operation, ReplaceText):
                    replace_text(
                        package,
                        operation.locator,
                        operation.text,
                        operation.replacement,
                        occurrence=operation.occurrence,
                        tracked=operation.tracked,
                        author=request.author,
                    )
                    detail = {"before": operation.text, "after": operation.replacement}
                elif isinstance(operation, DeleteText):
                    delete_text(
                        package,
                        operation.locator,
                        operation.text,
                        occurrence=operation.occurrence,
                        tracked=operation.tracked,
                        author=request.author,
                    )
                    detail = {"before": operation.text, "after": ""}
                elif isinstance(operation, InsertParagraph):
                    insert_paragraph(
                        package,
                        operation.locator,
                        operation.text,
                        position=operation.position,
                        style=operation.style,
                        tracked=operation.tracked,
                        author=request.author,
                    )
                    detail = {"text": operation.text, "position": operation.position}
                elif isinstance(operation, AddComment):
                    comment_id = add_comment(
                        package,
                        operation.locator,
                        operation.text,
                        operation.comment,
                        author=operation.author or request.author,
                        initials=operation.initials,
                    )
                    detail = {"anchor": operation.text, "comment_id": comment_id}
                elif isinstance(operation, SetAltText):
                    set_alt_text(
                        package,
                        operation.locator,
                        operation.description,
                        title=operation.title,
                    )
                    detail = {
                        "title": operation.title,
                        "description": operation.description,
                    }
                else:  # pragma: no cover - discriminated schema is exhaustive
                    raise RavenError(
                        ErrorCode.INVALID_REQUEST,
                        "Unsupported document operation.",
                        stage="transaction.prepare",
                    )
                diffs.append({"operation": index, "type": operation.type, **detail})
            return {"applied": len(diffs)}, diffs, []

        return self._prepare(
            document_path=request.document_path,
            expected_sha256=request.expected_sha256,
            author=request.author,
            intent=request.intent,
            operations=[
                operation.model_dump(mode="json") for operation in request.operations
            ],
            mutation=mutate,
            idempotency_key=request.idempotency_key,
        )

    def prepare_citation_insert(
        self, request: CitationInsertRequest
    ) -> TransactionPreview:
        def mutate(package: OpcPackage) -> tuple[dict[str, Any], list[dict[str, Any]], list[str]]:
            result = CitationManager(package).insert(
                request.locator,
                request.items,
                formatted=request.formatted_citation,
                style=request.style,
                locale=request.locale,
            )
            diff = {
                "type": "insert_citation",
                "citation_id": result["citation_id"],
                "locator": request.locator.model_dump(mode="json"),
            }
            return result, [diff], list(result["warnings"])

        return self._prepare(
            document_path=request.document_path,
            expected_sha256=request.expected_sha256,
            author=request.author,
            intent=request.intent,
            operations=[request.model_dump(mode="json")],
            mutation=mutate,
        )

    def prepare_citation_update(
        self, request: CitationUpdateRequest
    ) -> TransactionPreview:
        def mutate(package: OpcPackage) -> tuple[dict[str, Any], list[dict[str, Any]], list[str]]:
            result = CitationManager(package).update(
                request.citation_id,
                request.items,
                request.formatted_citation,
            )
            return result, [{"type": "update_citation", "citation_id": request.citation_id}], list(
                result["warnings"]
            )

        return self._prepare(
            document_path=request.document_path,
            expected_sha256=request.expected_sha256,
            author=request.author,
            intent=request.intent,
            operations=[request.model_dump(mode="json")],
            mutation=mutate,
        )

    def prepare_citation_remove(
        self, request: CitationRemoveRequest
    ) -> TransactionPreview:
        def mutate(package: OpcPackage) -> tuple[dict[str, Any], list[dict[str, Any]], list[str]]:
            result = CitationManager(package).remove(
                request.citation_id, request.keep_visible_text
            )
            warning = (
                "Citation field removal is not represented as a tracked deletion."
            )
            return result, [{"type": "remove_citation", "citation_id": request.citation_id}], [
                warning
            ]

        return self._prepare(
            document_path=request.document_path,
            expected_sha256=request.expected_sha256,
            author=request.author,
            intent=request.intent,
            operations=[request.model_dump(mode="json")],
            mutation=mutate,
        )

    def prepare_bibliography(
        self, request: BibliographySyncRequest
    ) -> TransactionPreview:
        def mutate(package: OpcPackage) -> tuple[dict[str, Any], list[dict[str, Any]], list[str]]:
            result = CitationManager(package).sync_bibliography(
                request.locator,
                request.heading,
                request.style,
                request.locale,
            )
            return result, [{"type": "sync_bibliography", "created": result["created"]}], list(
                result["warnings"]
            )

        return self._prepare(
            document_path=request.document_path,
            expected_sha256=request.expected_sha256,
            author=request.author,
            intent=request.intent,
            operations=[request.model_dump(mode="json")],
            mutation=mutate,
        )

    def _get(self, transaction_id: str) -> PendingTransaction:
        with self._lock:
            self._cleanup()
            transaction = self._transactions.get(transaction_id)
        if transaction is None:
            raise RavenError(
                ErrorCode.TRANSACTION_NOT_FOUND,
                "The transaction does not exist or has expired.",
                stage="transaction",
                remediation="Prepare the changes again.",
            )
        return transaction

    def abort(self, transaction_id: str) -> dict[str, Any]:
        with self._lock:
            transaction = self._transactions.pop(transaction_id, None)
            if transaction and transaction.idempotency_key:
                self._idempotency.pop(transaction.idempotency_key, None)
        return {"transaction_id": transaction_id, "aborted": transaction is not None}

    def commit(self, request: CommitRequest) -> CommitResult:
        transaction = self._get(request.transaction_id)
        if not secrets.compare_digest(
            transaction.confirmation_token, request.confirmation_token
        ):
            raise RavenError(
                ErrorCode.INVALID_REQUEST,
                "The transaction confirmation token is invalid.",
                stage="transaction.commit",
            )

        output = self.settings.resolve_path(request.output_path, must_exist=False)
        if output.suffix.casefold() != ".docx":
            raise RavenError(
                ErrorCode.UNSUPPORTED_DOCUMENT,
                "The output path must end in .docx.",
                stage="transaction.commit",
            )
        output.parent.mkdir(parents=True, exist_ok=True)
        if output.exists() and not request.overwrite:
            raise RavenError(
                ErrorCode.COMMIT_CONFLICT,
                f"Output already exists: {output}",
                stage="transaction.commit",
                remediation="Choose a new output path or explicitly allow overwrite.",
            )
        ensure_not_open_in_word(transaction.source_path)
        ensure_not_open_in_word(output)

        lock_path = output.with_name(f".{output.name}.raven.lock")
        backup: Path | None = None
        try:
            with portalocker.Lock(lock_path, mode="a", timeout=0):
                current_sha = sha256_file(transaction.source_path)
                if not secrets.compare_digest(current_sha, transaction.source_sha256):
                    raise RavenError(
                        ErrorCode.COMMIT_CONFLICT,
                        "The source document changed after the transaction was prepared.",
                        stage="transaction.commit",
                        remediation="Inspect the source and prepare the changes again.",
                    )
                if output.exists() and request.overwrite:
                    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
                    backup = output.with_name(f"{output.name}.{timestamp}.bak")
                    shutil.copy2(output, backup)
                transaction.package.write(output)
        except portalocker.exceptions.LockException as exc:
            raise RavenError(
                ErrorCode.FILE_IN_USE,
                f"Another Raven process is writing {output.name}.",
                stage="transaction.commit",
                retryable=True,
            ) from exc
        finally:
            lock_path.unlink(missing_ok=True)

        output_sha = sha256_file(output)
        audit = {
            "schema_version": 1,
            "transaction_id": transaction.transaction_id,
            "created_at": transaction.created_at.isoformat(),
            "committed_at": datetime.now(UTC).isoformat(),
            "source_path": str(transaction.source_path),
            "output_path": str(output),
            "source_sha256": transaction.source_sha256,
            "output_sha256": output_sha,
            "author": transaction.author,
            "intent": transaction.intent,
            "operations": transaction.operations,
            "semantic_diff": transaction.semantic_diff,
            "warnings": transaction.warnings,
            "changed_parts": sorted(transaction.package.changed_parts),
            "validation": transaction.validation,
        }
        audit_path = output.with_suffix(f"{output.suffix}.raven-audit.json")
        self._write_json_atomic(audit_path, audit)
        self.abort(transaction.transaction_id)
        return CommitResult(
            transaction_id=transaction.transaction_id,
            output_path=str(output),
            source_sha256=transaction.source_sha256,
            output_sha256=output_sha,
            changed_parts=sorted(transaction.package.changed_parts),
            audit_path=str(audit_path),
            backup_path=str(backup) if backup else None,
        )

    @staticmethod
    def _write_json_atomic(path: Path, value: Mapping[str, Any]) -> None:
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=path.parent,
                prefix=f".{path.name}.",
                suffix=".tmp",
                delete=False,
            ) as stream:
                temporary = Path(stream.name)
                json.dump(value, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        except OSError:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            raise
