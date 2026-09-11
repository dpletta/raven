# Contributing to Raven

Raven edits a format in which a small XML mistake can corrupt a manuscript or silently
detach a citation. Contributions should favor explicit invariants, fixture evidence, and
conservative rejection over broad but unverified compatibility.

## Development setup

Requirements:

- Python 3.12 or later;
- `uv`;
- a checkout of the repository.

Create/synchronize the environment, including the `dev` dependency group:

```bash
uv sync --dev
```

Run the stdio server from the checkout:

```bash
uv run raven-mcp
```

The process waits for MCP messages on standard input. For an MCP client configuration,
use:

```json
{
  "command": "uv",
  "args": [
    "--directory",
    "/absolute/path/to/raven",
    "run",
    "raven-mcp"
  ],
  "env": {
    "RAVEN_ALLOWED_ROOTS": "/absolute/path/to/scratch-documents"
  }
}
```

Use a dedicated scratch root containing copies, never irreplaceable manuscripts.

## Quality commands

Run the complete local quality gate:

```bash
uv run ruff format --check .
uv run ruff check .
uv run pyright
uv run pytest
```

To apply formatting:

```bash
uv run ruff format .
uv run ruff check --fix .
```

Review automated fixes before retaining them, especially around XML serialization,
exception mapping, and type narrowing.

Build the wheel and source distribution:

```bash
uv build
```

The project targets Python 3.12, uses Ruff with a 100-character line length, and runs
Pyright in strict mode over `src` and `tests`. Pytest discovers tests under `tests`.

## Change discipline

For changes to package handling, locators, field surgery, or transactions:

1. State the invariant being protected.
2. Add the smallest fixture that demonstrates the prior and intended behavior.
3. Test both a successful path and a nearby rejected/ambiguous path.
4. Assert semantic output and package invariants, not unstable ZIP byte layout.
5. Run full validation on staged and round-tripped package bytes.
6. Update the tool reference, architecture, and compatibility matrix when public behavior
   changes.

New tool arguments belong in strict Pydantic schemas. Expected caller/document failures
must map to an existing stable `ErrorCode`, or introduce a documented code with tests.
Avoid leaking tracebacks, credentials, full local configuration, or unrelated package
content through tool errors.

Public MCP tool names and argument meanings are compatibility surfaces. Renaming a tool,
changing defaults, broadening accepted documents, or changing error codes requires
explicit release notes and migration guidance.

## Fixture guidance

Compatibility claims must be backed by redistributable, minimal fixtures. Place test
fixtures under a clearly named `tests/fixtures` hierarchy when adding the corresponding
tests. A fixture should include a short provenance/expectation note in test code or
adjacent fixture metadata.

### Privacy and licensing

- Never commit a real manuscript, review comment, author identity, Zotero API key,
  unpublished result, or proprietary publisher template without documented permission.
- Prefer generated documents with fictional names, synthetic metadata, and public-domain
  prose.
- Remove core/custom properties, comments, revision authors, relationship targets,
  embedded thumbnails, and attachment metadata that are irrelevant to the case.
- Record whether the fixture was generated, hand-authored, or captured from an
  application, and under which terms it can be redistributed.
- Keep the smallest package that still preserves the behavior. Do not remove parts whose
  interaction is the subject of the test.

### Fixture categories

Maintain coverage for:

- minimal transitional `.docx` and nonstandard-but-valid part ordering;
- body/header/footer/footnote/endnote/comment story discovery;
- simple and nested runs, tabs, breaks, drawings, tables, styles, and metadata;
- balanced and malformed fields, bookmarks, comments, content controls, and revisions;
- tracked insertion, replacement, deletion, and paragraph insertion;
- classic-comment creation and alt-text updates;
- current and legacy Zotero citations, unknown payload members, duplicate IDs, missing
  IDs, and malformed JSON;
- zero, one, and multiple bibliography fields;
- valid, absent, chunked, non-contiguous, and malformed `ZOTERO_PREF_*` properties,
  including supplementary Unicode characters near chunk boundaries;
- ZIP traversal, duplicate members, encryption, DTD/entities, Strict OOXML, macro,
  ActiveX, signature, broken relationship, and resource-limit rejection;
- local and Web Zotero response variants, missing keys, invalid records, authorization,
  endpoint incompatibility, rate/server errors, and narrow fallback behavior;
- stale document/paragraph hashes, ambiguous anchors, transaction expiry, output
  conflicts, lock contention, backup creation, and audit receipts.

### Assertions

Useful fixture assertions include:

- untouched package-part payloads remain identical;
- only expected part names appear in `changed_parts`;
- relationships and content types remain complete;
- fields/comments/bookmarks stay balanced;
- unknown citation payload properties survive safe updates;
- preference chunks are contiguous and respect the 255 UTF-16-unit limit;
- external relationships are never requested;
- all unsafe inputs fail with the intended stable error code;
- prepare does not write, commit rechecks the source, and abort does not mutate files.

Avoid snapshots of random transaction IDs, confirmation tokens, UTC timestamps, generated
citation/session IDs, or full ZIP bytes unless the test normalizes them. Prefer structural
XML comparisons and explicit invariant assertions.

## Live compatibility evidence

Do not describe a feature as Word-validated or Zotero-validated based only on XML fixtures.
A live result should record at least:

- OS and architecture;
- exact Word version/build;
- exact Zotero desktop and Word-plugin versions;
- source fixture hash and Raven version/commit;
- operation sequence and audit receipt;
- whether Word reported repair/recovery;
- whether Zotero recognized and refreshed every field;
- before/after field code and preference properties;
- visual/manual checks for revisions, comments, notes, bibliography, and layout.

Live runs should use disposable library items and document copies. Do not automate or
publish credentials. Until such evidence is reproducible and checked into the project's
release process, documentation must retain the fixture-only warning.

## Documentation expectations

When behavior changes:

- `README.md` should explain the user-visible workflow and status;
- `docs/tool-reference.md` must match exact registered names, arguments, defaults,
  constraints, outputs, and stable errors;
- `docs/architecture.md` should describe changed trust boundaries and transaction/data
  flow;
- `docs/compatibility.md` should distinguish implemented, rejected, fixture-tested, and
  live-validated behavior.

Do not copy an internal helper signature into the public reference unless the helper is
actually registered as an MCP tool, resource, or prompt.

## Release guidance

Before publishing an alpha release:

1. Decide whether public tool/schema/error changes require a version change and migration
   note.
2. Update the project version in `pyproject.toml` and keep the source-tree fallback version
   in `src/raven_mcp/__init__.py` consistent.
3. Review dependency lower bounds and resolve known security advisories.
4. Run the complete quality gate on Python 3.12 or later.
5. Run all fixture compatibility tests and archive their results.
6. Build with `uv build` from a clean source tree.
7. Install the produced wheel into a fresh environment and confirm that both
   `raven-mcp` and `python -m raven_mcp` start as stdio servers.
8. Inspect wheel/sdist contents to ensure required documentation and license files are
   present and no fixtures contain private data.
9. Recheck README installation commands against the intended package index.
10. Publish release notes that list tool/schema/error changes, compatibility evidence,
    known limitations, and the continuing requirement for Zotero Refresh.

Do not promote the status beyond Alpha or remove the fixture-only warning solely because
the package builds or the structural tests pass. A release should never claim live
Word/Zotero support unless the versioned evidence described above was actually collected.

## License

Contributions are accepted under the repository's
[Apache License 2.0](../LICENSE). By contributing, ensure you have the right to submit the
code, documentation, and fixtures under those terms.
