"""Two-phase transaction, concurrency, and output-safety tests."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import NoReturn

import pytest

from raven_mcp.config import Settings
from raven_mcp.docx.opc import OpcPackage
from raven_mcp.errors import ErrorCode, RavenError
from raven_mcp.schemas import (
    CommitRequest,
    DocumentLocator,
    InsertText,
    PrepareChangesRequest,
    ReplaceText,
    TransactionPreview,
)
from raven_mcp.transactions import TransactionManager, sha256_file
from tests.conftest import DocxFactory, DocxSpec


def _locator(manager: TransactionManager, path: Path, index: int = 1) -> DocumentLocator:
    inspection = manager.inspect(str(path))
    return DocumentLocator.model_validate(inspection["paragraphs"][index]["locator"])


def _prepare_insert(
    manager: TransactionManager,
    path: Path,
    *,
    idempotency_key: str | None = None,
) -> TransactionPreview:
    return manager.prepare_changes(
        PrepareChangesRequest(
            document_path=str(path),
            expected_sha256=sha256_file(path),
            author="Transaction Tester",
            intent="Exercise transaction semantics",
            idempotency_key=idempotency_key,
            operations=[
                InsertText(
                    type="insert_text",
                    locator=_locator(manager, path),
                    text=" Added.",
                    position="end",
                    tracked=True,
                )
            ],
        )
    )


def test_prepare_is_copy_on_write_and_abort_is_idempotent(
    minimal_docx: Path,
    raven_settings: Settings,
) -> None:
    manager = TransactionManager(raven_settings)
    before = minimal_docx.read_bytes()

    preview = _prepare_insert(manager, minimal_docx)

    assert minimal_docx.read_bytes() == before
    assert preview.source_sha256 == sha256_file(minimal_docx)
    assert preview.semantic_diff == [
        {
            "operation": 0,
            "type": "insert_text",
            "text": " Added.",
            "position": "end",
        }
    ]
    assert preview.operations[-1] == {
        "type": "mutation_result",
        "result": {"applied": 1},
    }
    assert preview.validation["valid"] is True
    assert manager.abort(preview.transaction_id)["aborted"] is True
    assert manager.abort(preview.transaction_id)["aborted"] is False


def test_prepare_idempotency_returns_the_same_pending_transaction(
    minimal_docx: Path,
    raven_settings: Settings,
) -> None:
    manager = TransactionManager(raven_settings)

    first = _prepare_insert(manager, minimal_docx, idempotency_key="request-1")
    second = _prepare_insert(manager, minimal_docx, idempotency_key="request-1")

    assert second.transaction_id == first.transaction_id
    assert second.confirmation_token == first.confirmation_token


def test_commit_writes_docx_and_complete_audit_then_consumes_transaction(
    minimal_docx: Path,
    raven_settings: Settings,
    tmp_path: Path,
) -> None:
    manager = TransactionManager(raven_settings)
    preview = _prepare_insert(manager, minimal_docx)
    output = tmp_path / "committed.docx"

    result = manager.commit(
        CommitRequest(
            transaction_id=preview.transaction_id,
            confirmation_token=preview.confirmation_token,
            output_path=str(output),
        )
    )

    assert result.output_path == str(output)
    assert result.output_sha256 == sha256_file(output)
    assert result.source_sha256 == sha256_file(minimal_docx)
    assert "word/document.xml" in result.changed_parts
    audit_path = Path(result.audit_path)
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    assert audit["schema_version"] == 1
    assert audit["transaction_id"] == preview.transaction_id
    assert audit["author"] == "Transaction Tester"
    assert audit["intent"] == "Exercise transaction semantics"
    assert audit["output_sha256"] == result.output_sha256
    assert audit["semantic_diff"] == preview.semantic_diff
    assert audit["validation"]["valid"] is True
    assert not list(tmp_path.glob(".*.tmp"))

    reopened = OpcPackage.open(output, raven_settings)
    document = reopened.read_xml("word/document.xml")
    assert "Added." in "".join(document.itertext())
    with pytest.raises(RavenError) as consumed:
        manager.commit(
            CommitRequest(
                transaction_id=preview.transaction_id,
                confirmation_token=preview.confirmation_token,
                output_path=str(tmp_path / "second.docx"),
            )
        )
    assert consumed.value.code == ErrorCode.TRANSACTION_NOT_FOUND


def test_commit_overwrite_creates_byte_exact_backup(
    minimal_docx: Path,
    raven_settings: Settings,
    tmp_path: Path,
) -> None:
    manager = TransactionManager(raven_settings)
    preview = _prepare_insert(manager, minimal_docx)
    output = tmp_path / "existing.docx"
    original_output = b"existing output bytes"
    output.write_bytes(original_output)

    result = manager.commit(
        CommitRequest(
            transaction_id=preview.transaction_id,
            confirmation_token=preview.confirmation_token,
            output_path=str(output),
            overwrite=True,
        )
    )

    assert result.backup_path is not None
    assert Path(result.backup_path).read_bytes() == original_output
    assert output.read_bytes() != original_output


def test_commit_rejects_bad_token_extension_existing_output_and_owner_file(
    minimal_docx: Path,
    raven_settings: Settings,
    tmp_path: Path,
) -> None:
    manager = TransactionManager(raven_settings)
    preview = _prepare_insert(manager, minimal_docx)

    with pytest.raises(RavenError) as token:
        manager.commit(
            CommitRequest(
                transaction_id=preview.transaction_id,
                confirmation_token="wrong",
                output_path=str(tmp_path / "output.docx"),
            )
        )
    assert token.value.code == ErrorCode.INVALID_REQUEST

    with pytest.raises(RavenError) as extension:
        manager.commit(
            CommitRequest(
                transaction_id=preview.transaction_id,
                confirmation_token=preview.confirmation_token,
                output_path=str(tmp_path / "output.txt"),
            )
        )
    assert extension.value.code == ErrorCode.UNSUPPORTED_DOCUMENT

    outside = tmp_path.parent / f"outside-{tmp_path.name}.docx"
    with pytest.raises(RavenError) as path_error:
        manager.commit(
            CommitRequest(
                transaction_id=preview.transaction_id,
                confirmation_token=preview.confirmation_token,
                output_path=str(outside),
            )
        )
    assert path_error.value.code == ErrorCode.PATH_NOT_ALLOWED

    output = tmp_path / "output.docx"
    output.write_bytes(b"occupied")
    with pytest.raises(RavenError) as existing:
        manager.commit(
            CommitRequest(
                transaction_id=preview.transaction_id,
                confirmation_token=preview.confirmation_token,
                output_path=str(output),
            )
        )
    assert existing.value.code == ErrorCode.COMMIT_CONFLICT

    owner = output.with_name(f"~${output.name}")
    owner.write_bytes(b"")
    with pytest.raises(RavenError) as owner_error:
        manager.commit(
            CommitRequest(
                transaction_id=preview.transaction_id,
                confirmation_token=preview.confirmation_token,
                output_path=str(output),
                overwrite=True,
            )
        )
    assert owner_error.value.code == ErrorCode.FILE_IN_USE


def test_prepare_and_commit_detect_hash_conflicts(
    minimal_docx: Path,
    raven_settings: Settings,
    tmp_path: Path,
) -> None:
    manager = TransactionManager(raven_settings)
    with pytest.raises(RavenError) as stale:
        manager.open_document(str(minimal_docx), expected_sha256="0" * 64)
    assert stale.value.code == ErrorCode.STALE_REVISION

    preview = _prepare_insert(manager, minimal_docx)
    minimal_docx.write_bytes(minimal_docx.read_bytes() + b"changed-after-prepare")
    output = tmp_path / "conflict.docx"
    with pytest.raises(RavenError) as conflict:
        manager.commit(
            CommitRequest(
                transaction_id=preview.transaction_id,
                confirmation_token=preview.confirmation_token,
                output_path=str(output),
            )
        )
    assert conflict.value.code == ErrorCode.COMMIT_CONFLICT
    assert not output.exists()


def test_failed_multi_operation_prepare_leaves_source_unchanged(
    minimal_docx: Path,
    raven_settings: Settings,
) -> None:
    manager = TransactionManager(raven_settings)
    original = minimal_docx.read_bytes()
    locator = _locator(manager, minimal_docx).model_copy(update={"paragraph_hash": None})
    request = PrepareChangesRequest(
        document_path=str(minimal_docx),
        expected_sha256=sha256_file(minimal_docx),
        intent="The second operation must roll back the first",
        operations=[
            InsertText(
                type="insert_text",
                locator=locator,
                text=" staged",
                tracked=False,
            ),
            ReplaceText(
                type="replace_text",
                locator=locator,
                text="not present",
                replacement="never",
                tracked=False,
            ),
        ],
    )

    with pytest.raises(RavenError) as error:
        manager.prepare_changes(request)

    assert error.value.code == ErrorCode.ANCHOR_NOT_FOUND
    assert minimal_docx.read_bytes() == original


def test_failed_package_write_does_not_replace_existing_output(
    minimal_docx: Path,
    raven_settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = TransactionManager(raven_settings)
    preview = _prepare_insert(manager, minimal_docx)
    output = tmp_path / "protected.docx"
    existing = b"keep this output"
    output.write_bytes(existing)

    def fail_write(self: OpcPackage, path: Path) -> NoReturn:
        raise RavenError(
            ErrorCode.FILE_IN_USE,
            "simulated write failure",
            stage="package",
        )

    monkeypatch.setattr(OpcPackage, "write", fail_write)
    with pytest.raises(RavenError, match="simulated"):
        manager.commit(
            CommitRequest(
                transaction_id=preview.transaction_id,
                confirmation_token=preview.confirmation_token,
                output_path=str(output),
                overwrite=True,
            )
        )

    assert output.read_bytes() == existing
    assert manager.abort(preview.transaction_id)["aborted"] is True


def test_operation_limit_and_invalid_validation_profile_are_rejected(
    minimal_docx: Path,
    raven_settings: Settings,
) -> None:
    limited = TransactionManager(replace(raven_settings, max_operations=1))
    locator = _locator(limited, minimal_docx)
    request = PrepareChangesRequest(
        document_path=str(minimal_docx),
        intent="Too many",
        operations=[
            InsertText(type="insert_text", locator=locator, text="one"),
            InsertText(type="insert_text", locator=locator, text="two"),
        ],
    )

    with pytest.raises(RavenError) as limit:
        limited.prepare_changes(request)
    assert limit.value.code == ErrorCode.RESOURCE_LIMIT

    with pytest.raises(RavenError) as profile:
        limited.validate(str(minimal_docx), "unknown")
    assert profile.value.code == ErrorCode.INVALID_REQUEST


def test_source_owner_file_prevents_inspection(
    docx_factory: DocxFactory,
    raven_settings: Settings,
) -> None:
    path = docx_factory("open-in-word.docx", DocxSpec())
    path.with_name(f"~${path.name}").write_bytes(b"owner")
    manager = TransactionManager(raven_settings)

    with pytest.raises(RavenError) as error:
        manager.inspect(str(path))

    assert error.value.code == ErrorCode.FILE_IN_USE
    assert error.value.retryable is True
