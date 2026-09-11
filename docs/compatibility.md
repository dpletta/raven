# Compatibility

Raven 0.1.0 is alpha software. Its runtime capability resource explicitly reports native
Zotero support as `fixture_validated_only: true`.

> [!WARNING]
> No live end-to-end validation with Microsoft Word, the Zotero Word plugin, Zotero
> desktop, LibreOffice, or Word Online is claimed. “Supported” below means accepted and
> handled by the current implementation against fixture-shaped OOXML—not certified
> interoperability with an editor version.

Always retain the original, commit to a new output path, inspect the audit receipt, open
the result in desktop Word, run Zotero Refresh when fields are present, and manually
review the document.

## Document types

| Document/package | Status | Notes |
| --- | --- | --- |
| Transitional OOXML `.docx` | Implemented, fixture-only | Requires one valid Office document relationship and a WordprocessingML main-document content type. |
| `.doc` binary Word format | Rejected | Not an OPC ZIP package and not converted by Raven. |
| `.docm` / macro-enabled packages | Rejected | Extension and macro-enabled content, VBA, and ActiveX markers are unsupported. |
| Strict OOXML `.docx` | Rejected | Strict namespaces/markers are explicitly rejected. |
| Encrypted/password-protected package | Rejected | Encrypted ZIP members and unreadable encrypted containers are unsupported. |
| Digitally signed `.docx` | Rejected | Signature parts, relationships, and content types are rejected because rewriting invalidates signatures. |
| Malformed or unsafe OPC ZIP | Rejected | Includes traversal/absolute paths, duplicates, DTD/entities, invalid relationships, and configured resource-limit violations. |
| PDF, RTF, ODT, LaTeX, Markdown | Rejected | Raven has no import/export or conversion layer. |

Raven accepts only a path whose suffix is `.docx`; content inspection then applies the
stricter package checks. Renaming an unsupported file does not make it supported.

## Word content

### Implemented inspection/edit scope

- Main body plus directly related headers, footers, footnotes, endnotes, and classic
  comments are discovered for inspection.
- Visible paragraph text includes normal text and insertions, but excludes deleted text and
  field instructions.
- Ordinary tracked text operations work on conservative simple-run structures.
- Existing fields, bookmarks, content controls, revisions, and malformed boundaries are
  protected from overlapping ordinary edits.
- Classic comments and first-drawing alternative text are supported in safe located
  paragraphs.
- Unknown package parts are retained as opaque bytes; untouched member payloads and ZIP
  metadata are retained when the archive is rewritten.

### Unsupported or deliberately limited content

- Cross-paragraph replacement/deletion and arbitrary structural document transformations.
- Editing text that requires splitting nested hyperlinks, smart tags, structured document
  tags, equations, or other non-run containers.
- Edits through fields, bookmarks, content controls, or existing tracked changes.
- Precise rendered-page/layout guarantees, pagination, font substitution, and visual diff.
- Modern threaded comments as a distinct authoring feature. Raven creates classic Word
  comments only.
- Selecting a specific drawing when multiple drawings share a paragraph.
- Preserving package-level byte identity after an edit. Modified XML is reserialized and
  the ZIP is rewritten.

## Story compatibility

| Story | Inspect / ordinary operations | Native Zotero fields |
| --- | --- | --- |
| Body | Yes, fixture-only | Yes, only `word/document.xml` |
| Header | Yes, when directly related | No |
| Footer | Yes, when directly related | No |
| Footnote | Yes; separator notes are filtered | Yes, only `word/footnotes.xml` |
| Endnote | Yes; separator notes are filtered | Yes, only `word/endnotes.xml` |
| Classic comment body | Yes | No |

OPC permits relocated part names, but citation management currently expects the standard
body/footnote/endnote paths. Ordinary inspection follows the actual main-document
relationship and direct story relationships.

## Editor and integration policy

| Editor/integration | Status | Responsibility |
| --- | --- | --- |
| Microsoft Word desktop | Intended review target; not live-validated | Open and visually inspect the committed output. |
| Zotero Word plugin in desktop Word | Required for authoritative native-field refresh; not live-validated | Refresh citations and bibliographies, then verify them. |
| Zotero desktop local HTTP API | Implemented read-only; not live-validated | Search/hydrate items when available. Raven does not access Zotero's database directly. |
| Zotero Web API v3 | Optional read-only fallback; not live-validated | Requires API key and numeric user ID; library permissions remain Zotero's responsibility. |
| Word Online | Unsupported for the Zotero plugin workflow | Raven reports `word_online_plugin: false`. |
| LibreOffice | Unsupported for native Zotero editing | Raven reports `libreoffice_editing: false`; field preservation is not guaranteed. |
| Google Docs, Pages, other converters | Unsupported | Conversion may flatten or rewrite fields and custom properties. |

Raven does not detect which editor created a `.docx`. These statuses describe the
supported workflow, not an editor fingerprint enforced at package-open time.

No Word or Zotero version range is promised until a versioned live compatibility suite
exists.

## Zotero native-field protocol warning

Raven writes the field instructions and document properties used by Zotero's Word
integration:

```text
ADDIN ZOTERO_ITEM CSL_CITATION {extended CSL JSON}
ADDIN ZOTERO_BIBL {metadata JSON} CSL_BIBLIOGRAPHY
ZOTERO_PREF_1, ZOTERO_PREF_2, ... custom document properties
```

This is a de facto interoperability protocol inferred from document artifacts, not a
formal stable API promised for third-party writers. Zotero may change field payloads,
preference XML, chunking, session handling, or plugin behavior. Raven preserves unknown
existing payload members where practical, but that does not guarantee forward
compatibility.

New fields are marked dirty and contain a caller-supplied visible fallback or a
deterministic placeholder. Preference properties use data version 3, `fieldType="Field"`,
and chunks of at most 255 UTF-16 code units. Structural validation confirms Raven's
expected shape only; it does not execute the Zotero plugin.

## Fixture matrix

The matrix is the current fixture-level compatibility contract. It describes the
implemented structural scenarios and expected behavior; it is not evidence of live
application testing or a promise that every producer-specific variant has a checked-in
fixture.

| Fixture-shaped scenario | Expected Raven behavior | Evidence boundary |
| --- | --- | --- |
| Minimal transitional `.docx` | Inspect body, hash paragraphs, validate, preserve unrelated parts | OOXML/package-level only |
| Body with styles, tables, drawings, core metadata | Count/summarize structures; expose styles; preserve content not selected for edit | Structural, not visual layout |
| Direct headers and footers | Discover and inspect; permit conservative ordinary edits | No editor round-trip |
| Footnotes/endnotes with separator records | Exclude separator records; inspect normal notes; support native fields only at standard parts | No Zotero note-citation refresh |
| Classic comments | Validate range/body/reference balance; create a classic comment part when absent | No live comment display check |
| Existing tracked insert/delete/move markup | Count revisions and protect affected ranges | No Accept/Reject Changes round-trip |
| Existing fields and bookmarks | Parse/protect spans and report unbalanced boundaries | No field-code execution |
| Simple direct-run tracked replacement | Emit `w:del` plus `w:ins`; validate resulting package | No visual revision-pane check |
| Native current Zotero citation | Parse payload, list visible result, preserve unknown data on safe update | Protocol-shape only |
| Legacy Zotero citation prefix | Parse and warn that the instruction is legacy | No plugin migration check |
| New Zotero citation | Write dirty complex field, extended CSL JSON, preferences, and placeholder/fallback | Must be refreshed manually |
| One Zotero bibliography | List or dirty it; retain metadata; write placeholder | Must be refreshed manually |
| No bibliography plus safe locator | Add optional `Heading1` and bibliography paragraph; set preference flag | Must be refreshed manually |
| Duplicate citation IDs | Warn on list and reject ambiguous field update/removal | Structural detection only |
| Multiple bibliographies | Warn on list and reject sync as ambiguous | Structural detection only |
| Malformed fields/preferences | Report warnings or validation errors; reject unsafe surgery | Does not repair arbitrary corruption |
| External relationships | Preserve and report without fetching | Target availability not checked |
| Macro, ActiveX, signature, Strict OOXML, encrypted member | Reject before editing | Negative package fixture behavior |
| Oversized/member-heavy package | Reject at configured limits | Resource policy, not stress certification |
| Local Zotero response | Normalize item data, CSL-JSON, URI, source | Adapter payload shape only |
| Local transport/selected endpoint failure plus Web credentials | Use narrow Web API fallback | HTTP adapter behavior, not live service validation |

Contributors should record the provenance and expected invariants of every fixture and
must not upgrade a matrix entry to live-validated without a reproducible versioned
Word/Zotero run. See [Contributing](contributing.md).

## Why citation deletion is not tracked

Word tracked deletions are XML containers around content. A native citation is not merely
its visible text: it is a coordinated sequence of field begin, instruction, separator,
result, and end runs. Wrapping some or all of that sequence in `w:del` can:

- hide field instructions from parsers while leaving visible remnants;
- unbalance boundaries in the active document view;
- cause Zotero to miss the field or update a stale duplicate;
- produce different behavior depending on whether revisions are accepted or rejected.

Raven therefore removes the complete citation field atomically and does not represent that
field surgery as a tracked deletion. `keep_visible_text=true` can retain the current
rendered result as ordinary text, but that text is no longer a Zotero citation.

Reviewability comes from the prepare preview, explicit confirmation token, copy-on-write
output, semantic diff, warning, and audit receipt. If a workflow requires a visually
tracked citation deletion, perform and verify it manually in Word/Zotero rather than
asking Raven to create structurally ambiguous markup.

Citation insertion and field rewrites are likewise native field surgery, not ordinary
tracked text revisions.

## Zotero Refresh responsibility

Raven does not include a CSL processor and does not control desktop applications.
After any citation insertion/update or bibliography sync:

1. Commit to a new `.docx`.
2. Review the sidecar audit receipt.
3. Open the output in Microsoft Word desktop.
4. Confirm the Zotero Word plugin recognizes the document.
5. Run **Zotero → Refresh**.
6. Verify item identity, prefixes/suffixes, locators, style, locale, numbering,
   disambiguation, and bibliography content.
7. Save, close, and re-inspect if Raven will make another change.

The Zotero plugin owns authoritative citation and bibliography rendering. Raven's
placeholder or caller-supplied fallback is not a correctness assertion.
