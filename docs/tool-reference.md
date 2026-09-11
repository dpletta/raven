# Raven MCP reference

This is the public MCP surface implemented by Raven 0.1.0: 14 tools, one resource, and two
prompts. Raven uses stdio transport.

## Conventions

### Closed documents and allowed paths

Every document tool expects a closed `.docx` under one of `RAVEN_ALLOWED_ROOTS`. Raven
rejects the operation when Word's sibling owner file (`~$<name>`) exists. That check is
best-effort; close Word before every inspect, prepare, validate, or commit call.

Paths may be relative to the server process, but absolute paths are safer. The output path
must also resolve under an allowed root.

### Inspect before prepare

Use `document_inspect` immediately before editing. Pass:

- `summary.sha256` as `expected_sha256`; and
- the complete returned paragraph `locator`, including `story_part` and `paragraph_hash`.

Both checks are optional in the schema but materially reduce stale-anchor risk.

### Prepare tool output

`document_prepare_changes`, `citation_prepare_insert`, `citation_prepare_update`,
`citation_prepare_remove`, and `bibliography_prepare_sync` all return a transaction
preview:

| Field | Meaning |
| --- | --- |
| `transaction_id` | Random ID for commit or abort. |
| `confirmation_token` | Random token required by `document_commit`. Treat it as sensitive. |
| `source_path` | Canonical source path. |
| `source_sha256` | SHA-256 captured at preparation. |
| `created_at`, `expires_at` | UTC timestamps for the in-memory transaction. |
| `operations` | Normalized request operation(s), followed by an internal `mutation_result` entry. |
| `semantic_diff` | A compact operation-oriented preview; it is not a rendered Word diff. |
| `warnings` | Mutation and validation warnings. |
| `validation` | Full package, semantic, and Zotero validation report for the staged package. |

A preview does not write a document. Review it, then pass its ID and token to
`document_commit`, or discard it with `document_abort`.

### `DocumentLocator`

```text
{
  story: "body" | "header" | "footer" | "footnote" | "endnote" | "comment"
         = "body",
  story_part?: string | null,
  paragraph_index: integer >= 0,
  paragraph_hash?: string | null,
  exact_text?: string | null,
  occurrence: integer >= 1 = 1,
  prefix?: string | null,
  suffix?: string | null
}
```

`paragraph_index` is zero-based within one discovered story part. `occurrence` is
one-based. `prefix` must immediately precede the selected exact text and `suffix` must
immediately follow it. Matching is exact and case-sensitive.

For ordinary `insert_text`, `before`/`after` uses `locator.exact_text` and the locator's
occurrence. For `replace_text`, `delete_text`, and `add_comment`, the operation's `text`
becomes the exact text. Replace/delete use the operation's own `occurrence`; comments use
the locator occurrence.

### `CitationItemInput`

```text
{
  item_key: string,
  uri?: string | null,
  csl_json?: object | null,
  locator?: string | null,
  label: string = "page",
  prefix?: string | null,
  suffix?: string | null,
  suppress_author: boolean = false,
  author_only: boolean = false
}
```

If either `uri` or `csl_json` is absent or empty, citation insert/update asks Zotero to
hydrate both from `item_key`. Supply both to avoid a Zotero request. Raven embeds snapshots
in the field; it does not later reconcile them with the library.

## Document tools

### `document_inspect`

```text
document_inspect(
  document_path: string,
  story?: StoryKind | null = null,
  offset: integer = 0,
  limit: integer = 200,
  include_metadata: boolean = true
) -> object
```

Constraints: `offset >= 0`; `1 <= limit <= 1000`.

Returns:

- `summary`: path, SHA-256, core title, paragraph/word/heading/table/figure/note/comment/
  revision/citation counts, bibliography presence, and warnings;
- `paragraphs`: the requested page of `{locator, text, style, kind,
  protected_ranges}`;
- `pagination`: offset, limit, returned, total, and nullable `next_offset`;
- `metadata`: core properties, story inventory, style ID/name map, unfetched external
  relationships, and parsed complex-field instructions/results, or `null` when
  `include_metadata=false`.

`story` filters paragraph results after full inspection. The summary and metadata still
describe the complete document. Field instructions and embedded citation data can be
sensitive; use `include_metadata=false` if the client does not need them.

Typical next step: use a returned locator and `summary.sha256` in a prepare tool.

### `document_prepare_changes`

```text
document_prepare_changes(
  document_path: string,
  operations: DocumentOperation[1..RAVEN_MAX_OPERATIONS],
  intent: string,
  expected_sha256?: string | null = null,
  author: string = "Raven",
  idempotency_key?: string | null = null
) -> TransactionPreview
```

All operations are applied in list order to one staged package. If any operation or final
validation fails, no transaction is registered and no document is written.

Supported discriminated operations:

#### `insert_text`

```text
{
  type: "insert_text",
  locator: DocumentLocator,
  text: string,
  position: "start" | "end" | "before" | "after" = "end",
  tracked: boolean = true
}
```

`before` and `after` require `locator.exact_text`. The insertion must not land in a
protected range. Tabs and newlines are emitted as Word tab/break nodes when possible.

#### `replace_text`

```text
{
  type: "replace_text",
  locator: DocumentLocator,
  text: string,
  replacement: string,
  occurrence: integer >= 1 = 1,
  tracked: boolean = true
}
```

Replaces one exact visible occurrence in one paragraph. A tracked replacement emits a
deletion revision and, when non-empty, an insertion revision.

#### `delete_text`

```text
{
  type: "delete_text",
  locator: DocumentLocator,
  text: string,
  occurrence: integer >= 1 = 1,
  tracked: boolean = true
}
```

Equivalent to replacement with an empty string.

#### `insert_paragraph`

```text
{
  type: "insert_paragraph",
  locator: DocumentLocator,
  text: string,
  position: "before" | "after" = "after",
  style?: string | null,
  tracked: boolean = true
}
```

Inserts a sibling paragraph. When `style` is absent Raven copies the target paragraph
properties; when present it writes that paragraph style ID. The inserted text, not the
paragraph container itself, is wrapped as an insertion revision when tracked.

#### `add_comment`

```text
{
  type: "add_comment",
  locator: DocumentLocator,
  text: string,
  comment: string,
  author?: string | null,
  initials?: string | null
}
```

Adds a classic Word comment anchored to one exact visible-text occurrence. It creates the
comments part/relationship/content-type declaration when needed. Operation `author`
overrides the prepare call's author.

#### `set_alt_text`

```text
{
  type: "set_alt_text",
  locator: DocumentLocator,
  title?: string | null,
  description: string
}
```

Updates the first supported DrawingML or VML drawing in the located paragraph. There is no
figure-index argument.

`idempotency_key` applies only to this tool. Reusing a live key returns its existing
preview without comparing the new request body. Keys expire with transactions.

### `document_commit`

```text
document_commit(
  transaction_id: string,
  confirmation_token: string,
  output_path: string,
  overwrite: boolean = false
) -> object
```

Returns:

```text
{
  transaction_id: string,
  output_path: string,
  source_sha256: string,
  output_sha256: string,
  changed_parts: string[],
  audit_path: string,
  backup_path: string | null
}
```

The path must end in `.docx`. Commit rechecks the source hash, then atomically writes the
staged package. It does not rerun validation; the returned preview contains preparation's
validation report.

If the output exists, `overwrite=false` returns `COMMIT_CONFLICT`. With `overwrite=true`,
Raven first copies it to `<output>.YYYYMMDDTHHMMSSZ.bak`. An existing source can be used
as the output only with explicit overwrite; a distinct output is strongly recommended.

On success Raven writes `<output>.raven-audit.json` and consumes the transaction. The
document write and audit write are separate; if audit creation fails, inspect the output
path before retrying.

### `document_abort`

```text
document_abort(transaction_id: string) -> {
  transaction_id: string,
  aborted: boolean
}
```

Deletes a pending in-memory transaction. It does not read or write a document. An unknown
ID returns `aborted: false`. Expired transactions are cleaned up lazily by transaction
lookup/registration; before that cleanup runs, abort can still find an expired entry and
return `aborted: true`.

### `document_validate`

```text
document_validate(
  document_path: string,
  profile: string = "full"
) -> object
```

Profiles:

| Profile | Checks |
| --- | --- |
| `package` | Re-serialized ZIP integrity, XML, content-type coverage, relationship targets, and external relationship count. |
| `semantic` | Story discovery, complex-field balance, classic-comment references/ranges, bookmarks, and revision IDs/authors. |
| `zotero` | Citation payload shape, bibliography instruction JSON, and contiguous/valid `ZOTERO_PREF_*` XML. |
| `full` | All three reports plus flattened `errors` and `warnings`. |

The return always includes `sha256` and `profile`. A validation report uses `valid`,
`errors`, `warnings`, and `checks`; `full` additionally nests each component report.
Invalid content is reported with `valid: false`; opening an unsafe or unsupported package
fails the tool before a report can be produced.

## Zotero read tools

Raven never writes to the Zotero library. It uses the local HTTP API first. The Web API is
tried only when both credentials are configured and local access fails with a transport
error or HTTP 403, 404, 405, or 501.

### `zotero_status`

```text
zotero_status() -> {
  available: true,
  source: "local" | "web",
  api_version: string | null,
  schema_version: string | null,
  server_id: string | null
}
```

Returns the first available source. Unavailability is a tool error; there is no successful
`available: false` response in the current implementation.

### `zotero_search`

```text
zotero_search(
  query: string = "",
  library_type: "user" | "group" = "user",
  library_id?: string | null = null,
  collection_key?: string | null = null,
  item_type?: string | null = null,
  tag?: string | null = null,
  search_mode: "titleCreatorYear" | "everything" = "titleCreatorYear",
  limit: integer = 20,
  start: integer = 0,
  sort: string = "dateModified",
  direction: "asc" | "desc" = "desc"
) -> object
```

Constraints:

- group requests require a digits-only `library_id`;
- `collection_key`, when present, must be an eight-character uppercase Zotero key;
- `1 <= limit <= 100`; `start >= 0`.

Returns `items`, `returned`, `start`, and `limit`. Each item contains:

```text
{
  key: string,
  version: integer | null,
  library: object,
  data: object,
  csl_json: object,
  uri: string | null,
  source: "local" | "web"
}
```

Results are a single Zotero API page. Raven does not return a total count or next-page
token; increment `start` by the number returned when another page is needed.

### `zotero_get_items`

```text
zotero_get_items(
  item_keys: string[1..100],
  library_type: "user" | "group" = "user",
  library_id?: string | null = null
) -> object
```

Keys must be eight uppercase characters from Zotero's key alphabet
(`2-9`, `A-Z` excluding ambiguous characters). Duplicate keys are deduplicated internally.
A group library requires a digits-only ID.

Returns:

```text
{
  items: ZoteroItem[],
  missing_keys: string[]
}
```

Found items follow requested key order. Missing keys are not themselves a tool error.

## Citation tools

Citation prepare tools perform whole-field surgery and return a standard transaction
preview. They do not write a document until `document_commit`.

Citation fields are supported only in body, footnote, and endnote stories at the standard
parts `word/document.xml`, `word/footnotes.xml`, and `word/endnotes.xml`. Headers, footers,
and comments may be inspected and edited as ordinary text but cannot receive native
citations through these tools.

### `citation_list`

```text
citation_list(document_path: string) -> object
```

Returns:

- `sha256`;
- `citations`: field ID, story, part, visible text, parsed payload, legacy flag, and
  `citation_id` when present;
- `bibliography`: the first parsed bibliography or `null`;
- `warnings`: malformed boundaries/instructions, missing or duplicate citation IDs,
  legacy instructions, and multiple bibliographies.

The current return does not include parsed `ZOTERO_PREF_*` preferences. Use
`document_validate(profile="zotero")` for preference validity/counts; citation insertion
and bibliography sync previews include their newly written preference summary in the
mutation result.

### `citation_prepare_insert`

```text
citation_prepare_insert(
  document_path: string,
  locator: DocumentLocator,
  items: CitationItemInput[1..],
  expected_sha256?: string | null = null,
  formatted_citation?: string | null = null,
  author: string = "Raven",
  intent: string = "Insert Zotero citation",
  style?: string | null = null,
  locale?: string | null = null
) -> TransactionPreview
```

Raven hydrates items missing a URI or CSL-JSON, creates a unique `citationID`, writes a
dirty native field, and writes/updates Zotero document preferences.

Insertion position is not a separate tool argument:

- when `locator.exact_text` is present, the field is inserted after that occurrence;
- otherwise it is inserted at the end of the paragraph.

If `formatted_citation` is absent, visible text is
`[Citation: KEY1; KEY2]` and the preview warns that Zotero Refresh must render CSL output.
If supplied, markup-like tags are stripped and HTML entities unescaped for the plain
visible field result; Raven does not verify that the supplied text matches the items or
style.

`style` and `locale` update preference metadata only. They do not render the citation.

### `citation_prepare_update`

```text
citation_prepare_update(
  document_path: string,
  citation_id: string,
  items?: CitationItemInput[] | null = null,
  formatted_citation?: string | null = null,
  expected_sha256?: string | null = null,
  author: string = "Raven",
  intent: string = "Update Zotero citation"
) -> TransactionPreview
```

`citation_id` must identify exactly one native citation.

- Omit `items` to retain the existing item payload.
- Omit both `items` and `formatted_citation` to retain the existing formatted fallback
  (or current visible field result if that property is absent).
- Supply `items` without `formatted_citation` to write a deterministic placeholder and a
  Refresh warning.

Unknown existing payload properties and matching item properties are preserved where
possible. The field must be unnested, complete, contained in one paragraph, and have
isolated boundary runs. This tool has no style or locale arguments and does not update
document preferences.

### `citation_prepare_remove`

```text
citation_prepare_remove(
  document_path: string,
  citation_id: string,
  expected_sha256?: string | null = null,
  keep_visible_text: boolean = false,
  author: string = "Raven",
  intent: string = "Remove Zotero citation"
) -> TransactionPreview
```

Removes exactly one native field. With `keep_visible_text=true`, its current visible
result becomes ordinary text.

Removal is deliberately not a tracked deletion: wrapping field instructions or boundaries
in Word deletion markup can make the field unparsable or cause Zotero to operate on stale
boundaries. Copy-on-write, preview review, the semantic diff, and the audit receipt are the
review controls for this operation. The preview always includes a warning about the
untracked field removal.

`author` and `intent` are recorded in the audit receipt; removal itself does not create a
Word revision.

### `bibliography_prepare_sync`

```text
bibliography_prepare_sync(
  document_path: string,
  expected_sha256?: string | null = null,
  locator?: DocumentLocator | null = null,
  heading?: string | null = "References",
  style?: string | null = null,
  locale?: string | null = null,
  author: string = "Raven",
  intent: string = "Synchronize Zotero bibliography"
) -> TransactionPreview
```

Behavior:

- no bibliography: `locator` is required; Raven optionally inserts a `Heading1` paragraph
  using `heading`, followed by a bibliography field paragraph;
- one bibliography: Raven rewrites it as dirty with the placeholder
  `[Bibliography: refresh with Zotero]`; supplied locator and heading are ignored with a
  warning;
- multiple bibliographies: preparation fails with `ANCHOR_AMBIGUOUS`.

Preferences are written with `hasBibliography=1`. `style` and `locale` update preference
metadata only. Desktop Zotero Refresh must generate the actual entries.

### `citation_scan_plain`

```text
citation_scan_plain(document_path: string) -> {
  sha256: string,
  matches: object[]
}
```

Scans visible paragraph text for simple regular-expression candidates:

- parenthetical author-year;
- narrative author-year;
- bracketed numeric citations and ranges.

Each match contains `kind`, matched `text`, zero-based `start`/`end`, and a paragraph
`locator`. The scanner ignores hidden field instructions because inspection exposes only
visible text, but it can still match visible field results and unrelated bracketed
numbers. It is a review aid, not a parser or Zotero resolver.

## Resource

### `raven://capabilities`

No arguments. Returns JSON with:

- server `version` and `transport: "stdio"`;
- supported/rejected document families and discovered story kinds;
- tracked-text/classic-comment capability flags;
- native Zotero field format, Refresh requirement, fixture-only validation flag, and
  LibreOffice/Word Online support flags;
- two-phase, copy-on-write, SHA-256 concurrency, and configured path-root safety data.

The resource is runtime configuration metadata, not proof that a particular document will
validate. In 0.1.0, citation compatibility is explicitly
`fixture_validated_only: true`, `libreoffice_editing: false`, and
`word_online_plugin: false`.

## Prompts

Prompts return workflow text for the MCP client; they do not inspect or mutate anything.

### `review_section`

```text
review_section(
  document_path: string,
  heading: string,
  objective: string
) -> string
```

Directs an agent to inspect the named section, propose minimal tracked edits, preserve
fields/comments, call `document_prepare_changes`, present its semantic diff, and wait for
approval of the output path before commit.

### `citation_audit`

```text
citation_audit(document_path: string) -> string
```

Directs an agent to inspect the document, list native fields, scan likely plain-text
citations, match only exact Zotero records, and report unresolved references and
bibliography status before preparing changes.

## Stable errors

Expected domain failures are exposed as an MCP tool error whose text is a JSON object:

```json
{
  "code": "PROTECTED_BOUNDARY",
  "message": "The edit crosses a protected field range.",
  "stage": "edit",
  "retryable": false,
  "remediation": null,
  "locator": {
    "story": "body",
    "paragraph_index": 3
  }
}
```

The stable contract is the `code` field:

| Code | Meaning / usual response |
| --- | --- |
| `INVALID_REQUEST` | Arguments conflict with a domain rule. Correct the request. |
| `PATH_NOT_ALLOWED` | A resolved path is outside `RAVEN_ALLOWED_ROOTS`. Reconfigure roots or choose another path. |
| `FILE_NOT_FOUND` | An input path does not exist. |
| `FILE_IN_USE` | A Word owner file exists, a Raven output lock is held, or atomic writing failed. Close the file or retry when `retryable` is true. |
| `UNSUPPORTED_DOCUMENT` | The extension, package family, content type, required part, macro/signature content, or output suffix is unsupported. |
| `UNSAFE_PACKAGE` | ZIP, XML, member path, relationship, or package structure failed safety checks. Do not bypass the check on untrusted input. |
| `RESOURCE_LIMIT` | Compressed size, expanded size, member count, output size, or operation count exceeded configuration. |
| `STALE_REVISION` | A document or paragraph hash no longer matches. Re-inspect and rebuild the request. |
| `ANCHOR_NOT_FOUND` | Story, paragraph, exact occurrence, drawing, or target field was not found. Re-inspect. |
| `ANCHOR_AMBIGUOUS` | A locator or citation ID matches multiple targets, or multiple bibliographies exist. Resolve manually. |
| `PROTECTED_BOUNDARY` | An edit would cross fields, bookmarks, controls, revisions, nested runs, or unsafe field boundaries. Choose a simpler anchor or repair in Word. |
| `UNSUPPORTED_REVISION` | A citation insertion point is inside tracked revision markup. Accept/reject the revision in Word first. |
| `REFERENCE_UNRESOLVED` | Zotero items or a unique live citation field could not be resolved. Search/list again. |
| `ZOTERO_UNAVAILABLE` | Local/Web API could not be reached or authorized. Check service and credentials; honor `retryable`. |
| `ZOTERO_INCOMPATIBLE` | Zotero returned unsupported data or a native field/preference cannot be safely understood. Refresh/upgrade and retry. |
| `TRANSACTION_NOT_FOUND` | Transaction is unknown, expired, aborted, consumed, or lost with a server restart. Prepare again. |
| `COMMIT_CONFLICT` | Output exists without overwrite permission or source changed after prepare. Re-inspect or choose a new output. |
| `VALIDATION_FAILED` | A staged or newly inserted structure did not pass Raven's validators. Inspect the reported errors. |
| `INTERNAL_ERROR` | An invariant or unexpected adapter/serialization condition failed. Preserve inputs and report a minimal reproducer. |

Do not parse `message` text for control flow. `stage`, `retryable`, `remediation`, and
`locator` are diagnostic fields and may be `null`.

Pydantic/MCP schema failures and the explicit `document_inspect` pagination check may be
reported by the MCP framework rather than this JSON envelope. Unexpected programming or
I/O errors outside Raven's mapped paths may also become generic MCP errors.
