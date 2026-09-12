#!/usr/bin/env bash
# Repository bootstrap for Raven. Idempotent: safe to re-run against cached state.
set -euo pipefail

# Sync the locked Python environment, including dev tooling (ruff, pyright, pytest).
uv sync --frozen --all-groups

# Restore the pinned Microsoft Open XML SDK used by the fixture validator.
dotnet restore tools/openxml-validator/Raven.OpenXmlValidator.csproj --locked-mode
