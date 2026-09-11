"""Runtime configuration and filesystem policy."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from raven_mcp.errors import ErrorCode, RavenError


def _split_paths(value: str) -> tuple[Path, ...]:
    return tuple(Path(item).expanduser().resolve() for item in value.split(os.pathsep) if item)


@dataclass(frozen=True, slots=True)
class Settings:
    allowed_roots: tuple[Path, ...]
    max_document_bytes: int = 100 * 1024 * 1024
    max_uncompressed_bytes: int = 512 * 1024 * 1024
    max_zip_members: int = 10_000
    max_operations: int = 250
    transaction_ttl_seconds: int = 3_600
    zotero_local_url: str = "http://127.0.0.1:23119/api"
    zotero_web_url: str = "https://api.zotero.org"
    zotero_api_key: str | None = None
    zotero_user_id: str | None = None
    default_csl_style: str = "http://www.zotero.org/styles/apa"
    default_locale: str = "en-US"

    @classmethod
    def from_env(cls) -> Settings:
        roots_value = os.environ.get("RAVEN_ALLOWED_ROOTS")
        roots = _split_paths(roots_value) if roots_value else (Path.cwd().resolve(),)
        return cls(
            allowed_roots=roots,
            max_document_bytes=int(os.environ.get("RAVEN_MAX_DOCUMENT_BYTES", 100 * 1024 * 1024)),
            max_uncompressed_bytes=int(
                os.environ.get("RAVEN_MAX_UNCOMPRESSED_BYTES", 512 * 1024 * 1024)
            ),
            max_zip_members=int(os.environ.get("RAVEN_MAX_ZIP_MEMBERS", "10000")),
            max_operations=int(os.environ.get("RAVEN_MAX_OPERATIONS", "250")),
            transaction_ttl_seconds=int(os.environ.get("RAVEN_TRANSACTION_TTL", "3600")),
            zotero_local_url=os.environ.get(
                "RAVEN_ZOTERO_LOCAL_URL", "http://127.0.0.1:23119/api"
            ).rstrip("/"),
            zotero_web_url=os.environ.get("RAVEN_ZOTERO_WEB_URL", "https://api.zotero.org").rstrip(
                "/"
            ),
            zotero_api_key=os.environ.get("ZOTERO_API_KEY"),
            zotero_user_id=os.environ.get("ZOTERO_USER_ID"),
            default_csl_style=os.environ.get(
                "RAVEN_DEFAULT_CSL_STYLE", "http://www.zotero.org/styles/apa"
            ),
            default_locale=os.environ.get("RAVEN_DEFAULT_LOCALE", "en-US"),
        )

    def resolve_path(self, value: str | Path, *, must_exist: bool = True) -> Path:
        candidate = Path(value).expanduser()
        try:
            resolved = candidate.resolve(strict=must_exist)
        except FileNotFoundError as exc:
            raise RavenError(
                ErrorCode.FILE_NOT_FOUND,
                f"File not found: {candidate}",
                stage="path",
                remediation="Provide an existing path inside an allowed root.",
            ) from exc

        if not any(
            resolved == root or resolved.is_relative_to(root) for root in self.allowed_roots
        ):
            raise RavenError(
                ErrorCode.PATH_NOT_ALLOWED,
                f"Path is outside configured roots: {resolved}",
                stage="path",
                remediation="Add the parent directory to RAVEN_ALLOWED_ROOTS.",
            )
        return resolved


settings = Settings.from_env()
