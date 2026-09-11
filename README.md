# Raven Academic Writing MCP

Raven is a local-first Model Context Protocol (MCP) server for conservative editing of
Microsoft Word `.docx` manuscripts. It inspects WordprocessingML, stages reviewable edits
in memory, writes a separate output by default, and creates native Zotero citation and
bibliography fields that remain editable by the Zotero Word plugin.

> [!WARNING]
> Raven is alpha software. Its Word and Zotero compatibility evidence is fixture-level,
> not live end-to-end validation with desktop Word or Zotero. Work on a
> copy, review the semantic preview and audit receipt, and open the result in desktop Word
> before relying on it. Native Zotero field formats are a de facto protocol and may change.

## What Raven is for

- Inspecting a closed transitional OOXML `.docx`, including body, header, footer, footnote,
  endnote, and classic-comment stories.
- Locating text with paragraph indexes, SHA-256 paragraph hashes, exact text, occurrence,
  and optional prefix/suffix context.
- Staging tracked text insertion, replacement, deletion, and paragraph insertion.
- Adding classic Word comments and setting the first drawing's alternative text in a
  located paragraph.
- Listing, inserting, updating, and removing native Zotero fields, and creating or
  dirtying a single Zotero bibliography field.
- Searching a Zotero library read-only, preferring Zotero's local HTTP API and using the
  Web API only as a configured fallback.
- Producing validation reports and a JSON audit receipt for committed changes.

## Non-goals

Raven is not:

- a general Word renderer, layout engine, or replacement for desktop Word;
- a CSL processor—fallback citation text and bibliography contents are not authoritative;
- a Zotero library writer, database reader, attachment downloader, or metadata resolver;
- a converter for `.doc`, `.docm`, Strict OOXML, encrypted, or signed documents;
- a way to automate Word's **Zotero Refresh** command;
- an editor for arbitrary nested Word markup or for text across fields, bookmarks, content
  controls, or existing revisions;
- a guarantee that LibreOffice, Word Online, or another editor will preserve Zotero fields.

## Safety model

Raven uses an inspect → prepare → review → commit workflow:

1. **Inspect** returns the document SHA-256 and paragraph locators. The source must be
   inside an allowed root and closed in Word.
2. **Prepare** clones the package in memory, applies a bounded operation batch, runs
   package, semantic, and Zotero validation, and returns a semantic preview plus a
   short-lived confirmation token. It does not write a document.
3. **Review** is a caller responsibility. Check every operation, warning, changed part,
   and the intended output path.
4. **Commit** rechecks the source SHA-256, acquires a Raven output lock, and atomically
   writes the staged package. Existing outputs require `overwrite=true` and are backed up.
5. Raven writes `<output>.raven-audit.json` with hashes, intent, operations, warnings,
   validation results, and changed package parts.

The default policy is copy-on-write because `overwrite` defaults to `false`. Explicitly
committing to an existing path, including the source path, can overwrite it after creating
a timestamped backup. Prefer a new output path.

Raven also rejects unsafe ZIP paths, duplicate or encrypted members, DTD/entity
declarations, macro/ActiveX content, digital signatures, Strict OOXML markers, malformed
relationships, and configured resource-limit violations. External relationships are
recorded but never fetched. These controls reduce document-handling risk; they are not an
OS sandbox and do not protect files that an authorized MCP client can reach inside an
allowed root.

See [Architecture](docs/architecture.md) for the detailed trust and transaction model.

## Prerequisites

- **Python 3.12 or later.**
- **Zotero desktop** with its local API available for local library search and item
  hydration. It is optional for document-only tools; Web API credentials can provide a
  read-only fallback.
- **Microsoft Word desktop with the Zotero plugin** to refresh and verify native citations
  and bibliographies. Keep the document closed while Raven reads or writes it.
- [`uv`](https://docs.astral.sh/uv/) is recommended. `pip` is also supported.

No specific Word or Zotero desktop version has been live-validated by this project.

## Install and run

The distribution name is `raven-academic-mcp`; the executable is `raven-mcp`.
The package commands below assume the alpha distribution is available from the configured
Python package index.

Run without a persistent install:

```bash
uvx --from raven-academic-mcp raven-mcp
```

Install as a uv tool:

```bash
uv tool install raven-academic-mcp
raven-mcp
```

Install with pip in an isolated Python 3.12+ environment:

```bash
python -m pip install raven-academic-mcp
raven-mcp
```

Run from a source checkout:

```bash
uv sync --dev
uv run raven-mcp
```

Raven is a stdio server. Starting it directly waits for an MCP client on standard input;
it does not open a port or provide an interactive shell.

## MCP client configuration

Set `RAVEN_ALLOWED_ROOTS` explicitly. If it is absent, Raven permits only the server
process's current working directory, which may not be the manuscript directory.

### Cursor

Add a server entry to `.cursor/mcp.json` in a project or to `~/.cursor/mcp.json`:

```json
{
  "mcpServers": {
    "raven": {
      "command": "uvx",
      "args": ["--from", "raven-academic-mcp", "raven-mcp"],
      "env": {
        "RAVEN_ALLOWED_ROOTS": "/absolute/path/to/manuscripts"
      }
    }
  }
}
```

For a source checkout, replace `command` and `args` with:

```json
{
  "command": "uv",
  "args": [
    "--directory",
    "/absolute/path/to/raven",
    "run",
    "raven-mcp"
  ]
}
```

### Claude Desktop

Add the same server definition under `mcpServers` in Claude Desktop's configuration file:

```json
{
  "mcpServers": {
    "raven": {
      "command": "uvx",
      "args": ["--from", "raven-academic-mcp", "raven-mcp"],
      "env": {
        "RAVEN_ALLOWED_ROOTS": "/absolute/path/to/manuscripts"
      }
    }
  }
}
```

Typical configuration locations are:

- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`

Restart the client after changing its configuration. On Windows, escape backslashes in
JSON strings and separate multiple allowed roots with `;`; POSIX systems use `:`.

## Environment variables

| Variable | Default | Meaning |
| --- | --- | --- |
| `RAVEN_ALLOWED_ROOTS` | server working directory | One or more permitted filesystem roots, separated by the OS path separator. Paths are expanded and resolved at startup. |
| `RAVEN_MAX_DOCUMENT_BYTES` | `104857600` (100 MiB) | Maximum compressed input and serialized output size. |
| `RAVEN_MAX_UNCOMPRESSED_BYTES` | `536870912` (512 MiB) | Maximum total uncompressed package size. |
| `RAVEN_MAX_ZIP_MEMBERS` | `10000` | Maximum package-member count. |
| `RAVEN_MAX_OPERATIONS` | `250` | Maximum operations in one `document_prepare_changes` batch. |
| `RAVEN_TRANSACTION_TTL` | `3600` | In-memory prepared-transaction lifetime in seconds. |
| `RAVEN_ZOTERO_LOCAL_URL` | `http://127.0.0.1:23119/api` | Base URL for Zotero's local HTTP API. |
| `RAVEN_ZOTERO_WEB_URL` | `https://api.zotero.org` | Base URL for the Zotero Web API. |
| `ZOTERO_API_KEY` | unset | Web API key. Used only when both Web API variables are set. |
| `ZOTERO_USER_ID` | unset | Numeric Zotero user ID for Web API access. |
| `RAVEN_DEFAULT_CSL_STYLE` | `http://www.zotero.org/styles/apa` | Style ID written to new or updated Zotero document preferences. Raven does not render the style. |
| `RAVEN_DEFAULT_LOCALE` | `en-US` | Locale written to new or updated Zotero document preferences. |

Treat the Web API key as a secret. Raven sends it only in the `Zotero-API-Key` header, but
MCP client environment configuration may store it in plaintext.

## Example: inspect → prepare → review → commit → Word Refresh

The following is an illustrative MCP tool sequence. Use absolute paths and values returned
by your own calls.

1. Inspect the closed document:

```json
{
  "name": "document_inspect",
  "arguments": {
    "document_path": "/work/manuscript.docx",
    "limit": 200
  }
}
```

Save `summary.sha256` and the target paragraph's complete `locator`.

2. Find or fetch the exact Zotero item:

```json
{
  "name": "zotero_search",
  "arguments": {
    "query": "Ada Lovelace analytical engine",
    "limit": 10
  }
}
```

3. Prepare a citation after exact visible text. Supplying only `item_key` asks Raven to
   hydrate the URI and CSL-JSON from Zotero:

```json
{
  "name": "citation_prepare_insert",
  "arguments": {
    "document_path": "/work/manuscript.docx",
    "expected_sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
    "locator": {
      "story": "body",
      "story_part": "word/document.xml",
      "paragraph_index": 12,
      "paragraph_hash": "abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789",
      "exact_text": "the analytical engine",
      "occurrence": 1
    },
    "items": [
      {
        "item_key": "ABCD2345"
      }
    ],
    "intent": "Cite the source supporting the analytical-engine statement"
  }
}
```

4. Review the returned `operations`, `semantic_diff`, `warnings`, and `validation`. Keep
   the returned `transaction_id` and `confirmation_token` private. If the preview is not
   acceptable, call `document_abort`.

5. Commit to a new path:

```json
{
  "name": "document_commit",
  "arguments": {
    "transaction_id": "returned-transaction-id",
    "confirmation_token": "returned-confirmation-token",
    "output_path": "/work/manuscript-raven.docx"
  }
}
```

6. Review the audit JSON, open `manuscript-raven.docx` in desktop Word, and use the Zotero
   plugin's **Refresh** command. The pre-refresh visible citation may be a deterministic
   placeholder. Zotero is responsible for style rendering, numbering, disambiguation, and
   bibliography generation.

## Tool catalog

| Tool | Purpose |
| --- | --- |
| `document_inspect` | Summarize and page through a closed document's paragraphs, locators, protected ranges, stories, styles, fields, and external relationships. |
| `document_prepare_changes` | Stage a bounded batch of text, paragraph, comment, and alt-text operations. |
| `document_commit` | Commit one reviewed transaction and emit an audit receipt. |
| `document_abort` | Remove an in-memory prepared transaction. |
| `document_validate` | Run `package`, `semantic`, `zotero`, or `full` validation. |
| `zotero_status` | Report the first available local or Web API source. |
| `zotero_search` | Search user or group libraries read-only. |
| `zotero_get_items` | Fetch up to 100 exact item keys read-only. |
| `citation_list` | List native citation fields and at most one primary bibliography result, plus warnings. |
| `citation_prepare_insert` | Stage one native citation field and Zotero document preferences. |
| `citation_prepare_update` | Stage an update selected by unique `citationID`. |
| `citation_prepare_remove` | Stage untracked field removal, optionally retaining visible text. |
| `bibliography_prepare_sync` | Stage creation or dirtying of the single managed bibliography field. |
| `citation_scan_plain` | Heuristically find likely plain-text author-year, narrative, and numeric citations. |

Raven also exposes the `raven://capabilities` resource and the `review_section` and
`citation_audit` prompts. See the complete [Tool reference](docs/tool-reference.md).

## Development

```bash
uv sync --dev
uv run ruff format --check .
uv run ruff check .
uv run pyright
uv run pytest
PYTHONPATH=. uv run python scripts/generate_openxml_fixtures.py .artifacts/openxml-fixtures
dotnet run --project tools/openxml-validator -- .artifacts/openxml-fixtures
uv build
```

Development and fixture rules are in [Contributing](docs/contributing.md).

## Status and compatibility

Raven is version `0.1.0` and classified as **Alpha**. The implementation has defensive
package handling, staged transactions, structured domain errors, and native-field
construction, but compatibility evidence remains fixture-only. There has been no claimed
live validation against desktop Word, the Zotero Word plugin, Zotero desktop, LibreOffice,
or Word Online.

Before using Raven on valuable material:

- retain the original and commit to a new output;
- inspect the audit receipt and run `document_validate`;
- open the output in desktop Word;
- run Zotero Refresh and verify every citation and bibliography entry;
- review tracked changes, comments, fields, footnotes, headers, figures, and layout.

See [Compatibility](docs/compatibility.md) for the support and fixture matrix and known
editor limitations.

## Documentation

- [Architecture and safety model](docs/architecture.md)
- [Tool, resource, prompt, and error reference](docs/tool-reference.md)
- [Document, editor, Zotero, and fixture compatibility](docs/compatibility.md)
- [Contributing and release guidance](docs/contributing.md)

## License

Raven is licensed under the [Apache License 2.0](LICENSE).
