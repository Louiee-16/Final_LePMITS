# Handoff notes — Collabora removal / LibreOffice migration / drafting architecture

Session context for whoever (or whichever fresh session) picks this up next.
Everything below is grounded in code actually read/written this session, not
assumed.

## 2026-09-01 follow-up session — all 9 open items resolved except #7/#8

A fresh session (context handed off via this file, independently re-derived
the same findings before reading this — see its own review earlier in that
conversation) fixed items 1–6 and the two smaller/decision items below.
Items 7 (god-file split) and 8 (centralized state-machine/permission layer)
were deliberately left alone — real architecture debt, but a large,
multi-app-touching refactor with meaningful regression risk for a live
legislative system, better done incrementally with tests backing each step
than in one pass. Item 9 (zero test coverage) is now partially addressed:
`documents/tests.py` has real coverage for everything touched this session
(25 tests, all passing) — sanitize.py's allowlist, `_can_view_document`/
`_can_edit_document`, and the wopi.py endpoints' permission checks — but
still nothing for the LibreOffice round-trip or checkpoint-sync ordering.

What changed, per item:
1. **IDOR fixed.** Added `_can_edit_document(user, doc)` next to
   `_can_view_document` in `documents/views.py` — narrower than the view
   check (DRAFT/GHOST: author only; REFERRED/COMMITTEE*/SECOND_READING:
   SECRETARIAT/STAFF/ADMIN; everything else: nobody). Wired into all three
   `documents/wopi.py` endpoints (`casualdocs_document_bytes` uses
   `_can_view_document`, `casualdocs_save`/`similarity_check` use
   `_can_edit_document`). This also resolves the decision item below about
   rejecting saves to non-editable-stage documents (e.g. APPROVED) — it
   falls out of `_can_edit_document` returning `False` for any status not
   explicitly listed.
2. **Media exposure fixed** — not by touching `config/urls.py` (that
   DEBUG-only routing is standard Django and left as-is), but by moving the
   actual sensitive files out from under `MEDIA_ROOT` entirely. New
   `settings.DOCUMENT_EDITOR_STORAGE_ROOT` (`BASE_DIR / 'protected_media' /
   'onlyoffice_pending'`), confirmed via grep that nothing under
   `templates/`/`static/` ever referenced a raw `onlyoffice_pending`
   MEDIA_URL path (every read already goes through a permission-checked
   Django view), so this was a safe, contained relocation — no URL
   contract changed. Existing files physically moved
   (`media/onlyoffice_pending/` → `protected_media/onlyoffice_pending/`,
   22 files, verified count before/after). `systemadmin/utils.py`'s
   `get_media_storage_size()` updated to sum both roots, since it would
   otherwise have silently under-reported disk usage after the move.
3. **Upload size cap fixed** — `DATA_UPLOAD_MAX_MEMORY_SIZE = 25 * 1024 *
   1024` added to `config/settings.py` (was unset, so Django's 2.5MB
   default applied to `casualdocs_save`'s `request.body` read). Verified
   against Django's own source (`django/http/request.py`) that this is
   really what gates it before adding the fix, and added a regression test
   that actually POSTs a 3MB payload and asserts 200, not just checking the
   setting value.
4. **AI Legal Basis stale-content bug fixed** — `ai_legal_basis` now calls
   `_onlyoffice_checkpoint_sync(document)` (the same non-blocking,
   no-status-change-side-effects helper real checkpoints use) right before
   reading `document.content`, so a mid-draft "Legal Basis" click sees the
   live docx's actual content instead of whatever was last synced at a
   status checkpoint (often still `''` pre-filing).
5. **`ai_inline_check`'s `@csrf_exempt` removed** — confirmed the frontend
   already sends `X-CSRFToken` on this call (`frontend/casualdocs-entry.jsx`
   line ~370), so no frontend change was needed.
6. **Editor-mount JS deduplicated** — new `static/js/document_editors/
   casualdocs-mount.js`, all 7 templates (`draft.html`, `view_draft.html`
   both branches, `referral_drafting_page.html`, `floor_amendments.html`,
   `amending_table.html`, `modal_document_viewer.html`, `document_view.html`)
   now call `CasualDocsMount.mount(...)` instead of reimplementing
   load-bundle/mount/similarity-check independently. Found and fixed a real
   bug in the process: the post-save similarity check
   (`documents/wopi.py::similarity_check`) was only actually wired via
   `onDirtyChange` in 2 of the 5 editing pages (`view_draft.html`,
   `referral_drafting_page.html`) — `draft.html` *defined* the function but
   never called it, and `floor_amendments.html`/`amending_table.html`
   didn't have it at all. The shared helper wires it consistently
   everywhere a `saveUrl` is present now.
   Smaller items also fixed: `pytesseract.pytesseract.tesseract_cmd` now
   reads `settings.TESSERACT_PATH` (same per-platform-default pattern as
   `LIBREOFFICE_PATH`) instead of a hardcoded Windows path; `config/
   settings.py`'s `SESSION_COOKIE_SECURE`/`CSRF_COOKIE_SECURE` now tied to
   `not DEBUG` instead of hardcoded `False`.

Not touched, and not recommended to touch without a dedicated pass: the
`.ql-editor` (Quill) CSS class naming left on read-only templates — purely
cosmetic, already self-documented in-template as intentional, low value
relative to the risk of a wide find/replace across styled markup.

Full test run (`python manage.py test`, 58 tests across the whole project)
passes after all of the above.

## 2026-09-01, same session — paragraph spacing changes after save+reopen

User-reported, with two screenshots: a draft typed fresh in `draft.html`
renders tight (no gap between lines); the identical content (same word
count, same char count — 1,052 words / 6,025 chars both times) visibly
gains space between every paragraph once saved and reopened via
`view_draft.html`'s edit mode (4 pages → 5 pages).

Root cause, confirmed directly against a real saved file
(`protected_media/onlyoffice_pending/doc_102.docx`, inspected via
python-docx and raw XML): Casual Docs' own exported .docx has **no**
`<w:spacing>` element anywhere at all — not per-paragraph, not on the
Normal style, not in docDefaults. Per the OOXML spec, "spacing absent
everywhere" isn't the same as "spacing is zero" — a compliant reader
falls back to its own built-in default, and the well-known modern-Word
default is ~10pt-after/1.15-line (independently confirmed: python-docx's
own bundled blank-document template embeds exactly `<w:spacing
w:after="200" w:line="276" w:lineRule="auto"/>` in docDefaults — that's
the industry-standard fallback this ambiguity resolves to). The live,
never-yet-round-tripped Casual Docs compose view evidently doesn't apply
that same fallback, hence the mismatch.

Blast radius is bigger than just the editor: LibreOffice reads this exact
ambiguous file for every PDF download/snapshot and the checkpoint
docx→HTML sync too (`_onlyoffice_convert_docx_to_pdf`,
`_sync_docx_to_document_content`) — so the same ambiguity could affect
official PDF output and Archive-stored HTML, not just what a councilor
sees on reopen.

Fix: `documents/wopi.py::_normalize_docx_default_spacing` — writes
explicit `w:spacing w:before="0" w:after="0" w:line="240"
w:lineRule="auto"` into docDefaults and the Normal style, but **only**
where nothing is already set (never overrides an intentional value).
Wired into `casualdocs_save`, non-blocking, right after the bytes are
written to disk. Verified with 4 new unit tests against the function
directly (fills gaps, doesn't override an explicit value, patches
docDefaults, idempotent) plus one end-to-end test through the real
endpoint with a genuine .docx payload (not the fake-bytes placeholder the
other save tests use) — all passing.

**Not independently visually verified in a browser this session** — no
browser automation was used; the fix is grounded in direct inspection of
the actual saved .docx file's XML plus a working, tested normalization
function, but confirming the *visual* result (reopen a draft in Casual
Docs, check the gap is gone) still needs a human to actually look at it.
Existing already-FILED/archived documents (like doc 102 itself) were
deliberately NOT retroactively rewritten — this only prevents the issue
on future saves of documents still in an editable stage.

## 2026-09-01/02, same session — comments vanish on reopen (data loss, not just display)

User-reported via two live tests in their own browser (I have no browser
access this session — both tests were the user's, not mine):
1. Reopening a draft with existing inline-check comments in
   `view_draft.html`'s edit mode shows the highlighted text but the
   Comments panel says "No comments yet."
2. Exporting straight from that same reopened session (Casual Docs' own
   File > Download) produces a .docx where the highlight is still there
   but the comment itself is gone.

Diagnosis, built from those two reports plus direct inspection of the
actual saved file (`doc_102.docx` — confirmed byte-level, not assumed):
Casual Docs correctly writes the modern 4-part Word comment schema
(`comments.xml` + `commentsExtended.xml` + `commentsIds.xml` +
`commentsExtensible.xml`, all correctly cross-referenced by
paraId/durableId — verified directly, and confirmed this survives a
`python-docx` open+resave completely intact, both content *and* the
package-level Content_Types/relationship declarations, ruling out
`_normalize_docx_default_spacing` as a contributing cause). The bug is in
Casual Docs' own reimport: it correctly renders the
`commentRangeStart`/`commentRangeEnd`/`commentReference` markers as a
highlight when loading `documentBuffer`, but doesn't reconstruct the
actual comment thread into its live editing session from those same
markers. Once that happens, the comment genuinely no longer exists in
the browser's in-memory model — so the *next* save (autosave included,
now that saves debounce automatically) overwrites the stored `.docx`
with a version permanently missing it. This is real, ongoing data loss
for every draft reopened even once, not a cosmetic display gap — that
was my own earlier, wrong read of the severity before the user's second
test (Download) proved it out.

Checked for an upstream fix before building a workaround: casualoffice.org's
docs, SDK reference, and changelog say nothing about comments surviving
(or not surviving) a reimport — `addComment` is documented only in
passing as one of several "document-agent" helpers. The GitHub repo
(`CasualOffice/docs`) has only 4 open issues total, none related; two
*merged* PRs touch comments (hyperlink range placement, delete
confirmation) but neither is this. No tracked issue, no documented
behavior either way, no newer version to try (1.4.2 is both pinned and
latest on npm).

**Stopgap implemented** (explicitly framed to the user as a stopgap, not
a real fix — they asked for it to be integrated and observed, not
presented as solved): `documents/docx_comments.py` —
`restore_missing_comments(existing_docx_path, new_docx_bytes)`, wired
into `documents/wopi.py::casualdocs_save` right before the incoming bytes
overwrite the stored file. Compares the new file's own
`commentRangeStart`/`commentReference` anchor IDs against what's already
on disk; for any ID the new file *still anchors* but no longer has a
comment body for, copies the matching `<w:comment>` (plus its
`commentsExtended`/`commentsIds`/`commentsExtensible` entries, found via
paraId/durableId cross-reference) back in from the on-disk version,
updating `[Content_Types].xml`/`word/_rels/document.xml.rels` if those
parts weren't already registered in the new package. Deliberately
conservative by construction: it only acts on an exact ID match between
old and new anchors, so if Casual Docs ever starts renumbering these IDs
on export, this just stops finding anything to restore (safe no-op)
rather than restoring something wrong — it can't make a save worse than
not having this function, only better in the exact scenario confirmed
above.

Verified two ways: (1) `documents/test_docx_comments.py` — 7 unit tests
against hand-built fixtures using the real 4-part schema (python-docx's
own `add_comment()` can't produce this schema at all, so fixtures are
built directly from the structure confirmed against the real file);
covers no-op cases (no existing file, no existing comments, non-overlapping
IDs, already-present comment) and the actual restore (full loss, partial
loss of 1-of-2, output still a valid openable docx). (2) A direct
integration check against the *real* `doc_102.docx` (6 real comments,
94 paragraphs) — simulated exactly what a post-reimport save produces
(document.xml unchanged, all 4 comment parts stripped) and confirmed all
6 comments restore with their original text intact and the output opens
cleanly. `documents/tests.py` also has one end-to-end test through the
actual `casualdocs_save` endpoint. Full suite: 71 tests, all passing.

**Not fixed, by design**: this doesn't make Casual Docs itself able to
*display* an existing comment's content when reopening (the panel will
likely still show "No comments yet" for a comment that predates this
session's reopen, even though it's now correctly preserved on disk) —
it only stops the data loss on save. The real fix discussed with the
user and not yet started: a database-backed "Similarity Flags" panel,
rendered outside the editor entirely, so it doesn't depend on Casual
Docs' comment round-trip at all.

## 2026-09-02, same session — the actual root cause: a save/addComment race, confirmed with real logs

After the stopgap above, the user kept hitting cases where a freshly
inline-check-flagged comment still didn't survive even a same-session
save (no reopen involved at all) — contradicting the "only reimport
loses comments" diagnosis. Rather than keep guessing, added a temporary
diagnostic (`documents/wopi.py::_log_incoming_comment_state`, logged
before any server-side processing touches the incoming bytes) plus a
proper file logging handler (`config/settings.py`'s `LOGGING` — was
console-only, meaning nothing was inspectable after the fact; now also
writes to `logs/debug.log`, gitignored via the existing `*.log` rule).

The logs settled it definitively. For a single document across one
editing session, consecutive incoming saves looked like:
```
doc 137: comment parts=[]  (x4 saves)
doc 137: comment parts=[comments.xml, ...]  (5th save)
```
Every one of several test documents showed the same shape: multiple
early saves with **no comment data at all** in the raw POST body Casual
Docs sent — not stripped by anything server-side, genuinely absent from
what the browser exported — followed eventually by a save that had it.
This is not the reimport bug from the entry above; it's a distinct,
simpler race: the live inline check's own network round trip
(`fetch('/ai-inline-check/')` → server-side embedding + pgvector
retrieval, see `documents/views.py::ai_inline_check`) can easily take
longer than `AUTOSAVE_DEBOUNCE_MS` (4s) — especially with a slow or
crash-looping Ollama backend (the user hit a real Ollama/CUDA crash
during this same testing window, `exit status 0xc0000409` /
`CUDA error: shared object initialization failed` — a local GPU/driver
issue, unrelated to this app's code, but it made the race far more
likely to manifest during testing). If the debounced or 30s-floor
autosave fires before `editorApi.addComment()` has actually run, it
captures and saves a snapshot from *before* the comment existed at all —
nothing to restore, because there was never a good copy anywhere, client
or server.

**Fixed** in `frontend/casualdocs-entry.jsx`: `forceSave()` now waits for
any in-flight inline check to settle before proceeding, bounded by the
new `INLINE_CHECK_MAX_WAIT_MS` (8000ms) so a genuinely stalled check
(Ollama down) can't hang saves indefinitely — it just proceeds anyway
past that point, same fallback philosophy as forceSave()'s existing
overall 30s timeout. Mechanism: `scheduleInlineCheck`'s debounce callback
now stores the check's promise in `inlineCheckInFlight` (cleared via
`.finally()` on settle, success or failure), and `forceSave()` races that
against the timeout before doing anything else. Applies to every save
path uniformly — debounce, 30s floor, and explicit Save/File Draft
button clicks — since they all funnel through the same `forceSave()`.
Rebuilt via `npm run build:casualdocs`; confirmed the three constants
(`2500`, `4000`, `8000`) landed correctly in the minified output next to
each other.

**Not independently verified in a browser this round either** — same
caveat as every frontend change this session. The diagnosis this time is
unusually solid though: it's not inference from indirect symptoms, it's
direct log evidence of the browser's own raw POST body missing comment
data across multiple real saves, with a plausible and specific
mechanism (confirmed independently — Ollama really was crashing around
the same time). The `_log_incoming_comment_state` diagnostic and the new
file-logging handler are left in place (harmless, low-noise) in case
another round of verification is needed — check `logs/debug.log` after
a retest rather than guessing again.

**Follow-up, same session — the single-promise version of this fix was
still incomplete.** User retested and doc 140 still lost a comment over
a minute after its check had clearly already finished (anchor present
well before the save that lacked it) — ruling out "just didn't wait long
enough." Re-reading `forceSave()`'s wait logic against that evidence
found two real remaining gaps, fixed in the same file:
1. `inlineCheckInFlight` was a single variable, overwritten by whichever
   check was scheduled most recently. Typing continuously reschedules the
   debounce — if an *earlier* check was still mid-flight (waiting on a
   slow Ollama call) when a *later* one started, `forceSave()` would only
   wait for the newer one, letting the older one's `addComment()` still
   land after a save had already gone out. Changed to
   `inFlightInlineChecks`, a `Set` of every currently-pending check's
   promise, all awaited together via `Promise.all()` (still bounded by
   the same `INLINE_CHECK_MAX_WAIT_MS`).
2. `saveWaiters` (the queue `forceSave()` uses to know when a save has
   actually landed, so it can resolve) was a **module-level** variable —
   shared across every `CasualDocsEditor.mount()` call on the page, not
   scoped per instance. If more than one editor is ever mounted at once
   in the same tab, one instance's `onSave` completing could incorrectly
   resolve a *different* instance's pending `forceSave()`. Changed to
   `saveWaitersRef`, a plain `{ current: [] }` object created fresh per
   `mount()` call and threaded through to `CasualDocsApp` as a prop
   (same pattern already used for `docxRef`).

Whether #2 was actually in play for doc 140 specifically is unconfirmed
— asked the user whether multiple drafts/tabs were open around that
time, no answer yet — but it's a real bug regardless of whether it's
*the* explanation here, so fixed it anyway rather than leaving it for a
future session to rediscover. Rebuilt via `npm run build:casualdocs`;
confirmed via grep that no code still references the old `saveWaiters`/
`inlineCheckInFlight` names, and that all three timing constants
survived the rebuild.

Still not resolved with certainty — next retest should confirm whether
this closes it, and `logs/debug.log` is still the way to check without
relying on the panel's own (separately broken) display.

## 2026-09-02, same session — AI Legal Basis added to view_draft.html

Unrelated to the comment-race investigation — user noticed reopening an
existing draft via "Draft Measures" (`view_draft.html`'s edit mode) had
no Legal Basis button at all, unlike `draft.html`. Confirmed this was
never a deliberate restriction, just a feature that was only ever built
into one of the two drafting pages.

Rather than copy the ~100 lines of modal HTML + fetch/render JS into
`view_draft.html` (recreating the exact duplication problem
`casualdocs-mount.js` was built to avoid), extracted it:
- `templates/documents/_ai_legal_basis_modal.html` — the modal markup,
  `{% include %}`d by both pages now. Close buttons call
  `AiLegalBasis.close()` instead of a page-level `closeAiModal()`
  global, since this is now shared code.
- `static/js/document_editors/ai-legal-basis.js` — `AiLegalBasis.init(opts)`,
  taking `titleInput`, `getDocId()`, `csrfToken`, `urls.autosave`/
  `urls.legalBasis`, and `getActiveEditor()` (for the forceSave-before-check
  step) as options, since the URLs need `{% url %}` from the calling
  template and can't be hardcoded in a plain static JS file.

`draft.html` refactored to use both (verified no behavior change — same
markup, same logic, just relocated). `view_draft.html`'s edit mode
gained the `#aiCheckBtn` button in its header and a call to
`AiLegalBasis.init(...)`, wired to its own `documentEditorDocId`/
`activeEditor`. Confirmed no stale references to the old page-level
`closeAiModal()` name remain anywhere. Full suite (71 tests) still
passes — this was template/JS-only, nothing Python-side changed.

## 2026-09-02, same session — full lifecycle audit + 3 fixes

User asked for a full trace of a document's lifecycle, draft through the
signed-PDF download. Ran a real document through every stage via Django's
test client (`documents/test_full_lifecycle.py`, new — the first test in
this app exercising the *complete* pipeline rather than one endpoint at a
time) — the core happy path (Draft → Filed → First Reading → Referred →
Committee → Second Reading → Third Reading → signed-PDF upload → Approved
→ official download) ran clean, no exceptions, correct reference numbers
and Archives/version-bump behavior at every transition. Found and fixed
three real problems along the way:

1. **`Document.status = 'RETURNED'` was an orphaned dead end.** Set by
   `committee_level/views.py::_sync_report_status_from_hearing` whenever
   a committee hearing outcome is logged as 'FAILED'. Confirmed directly
   — grepped the entire codebase — that nothing anywhere ever queries or
   displays `status='RETURNED'`: not Draft Measures (filters
   `status='DRAFT'` specifically), not any tracking page, not Django
   admin (`Document` isn't even registered there). A measure that failed
   at committee became permanently unreachable. Fixed to match the
   pattern `documents/views.py`'s `return_filed_doc`/
   `return_from_first_reading` already use: status back to `DRAFT`
   (reappears in Draft Measures immediately) plus a `ReturnReason`
   explaining why, `returned_by` the staffer who logged the outcome.
   `draft_measures.html` already renders `ReturnReason` in the list (used
   by the other return paths already) so this got full visibility for
   free, no template changes needed there. Hit a real bug while wiring
   this up: `HearingLog.objects.create()` stores the raw POST
   `hearing_date` string as-is (no ModelForm `to_python()` in this call
   path), so `strftime`-formatting it directly raised `ValueError` —
   fixed by parsing defensively (`django.utils.dateparse.parse_date`,
   falling back to the raw string if that fails).
2. **`move_to_second_reading` had no status filter at all** — unlike
   every sibling transition view (`committee_amendments`/
   `save_committee_amendments`/`move_to_unfinished`, all of which filter
   `Q(status='REFERRED') | Q(status__icontains='COMMITTEE')`), this one
   was a bare `get_object_or_404(Document, id=doc_id)`. Any SECRETARIAT/
   STAFF/ADMIN user could POST it against a document in *any* status —
   including an already-`APPROVED`, enacted ordinance — and silently
   regress it back to `SECOND_READING`. Also had no `request.method`
   check at all, and its one call site
   (`templates/documents/committee_level/committee_workbench.html`) was
   a plain `<a href>` GET link — meaning this state-mutating action had
   no CSRF protection whatsoever. Fixed: added the same status filter
   the sibling views use, added a POST-only guard, converted the
   template link to a CSRF-protected `<form method="POST">` matching the
   "Move to Unfinished Business" form right next to it.
3. **PDF letter-spacing silently dropped in the live download path.**
   `templates/documents/document_pdf.html` (the xhtml2pdf fallback —
   which is what's actually in use right now, since LibreOffice isn't
   installed) had 8 `letter-spacing: 0.0Xem` declarations across
   headers/letterhead/signatory blocks. Confirmed live during the
   lifecycle test run: ReportLab's CSS parser doesn't resolve `em`
   ("getSize: Not a float '0.03em'" etc.) and silently drops the
   property entirely. Converted each to the equivalent `px` value
   computed against that specific rule's own font-size (not a blind
   global substitution — e.g. `.doc-number`'s 13px font-size makes its
   0.05em exactly 0.65px). Verified: re-ran the lifecycle test and
   confirmed the `getSize` warnings are completely gone.

Also found and fixed one more thing while investigating: the `logs/
debug.log` FileHandler added earlier this session was using Windows'
default cp1252 encoding, not UTF-8 — confirmed directly that a log line
containing a plain `→` character never made it into the file *at all*
(silently dropped, not garbled — many existing log messages using
—/em-dashes "worked" only by accident, since U+2014 happens to have a
cp1252 mapping). Fixed with explicit `'encoding': 'utf-8'` on the
handler; verified with a real write. Truncated the old mixed-encoding
file since it's disposable debug data, not a permanent record.

New regression coverage: `committee_level/tests.py` (was the stock stub)
now has 10 tests covering both fixes #1 and #2, including the specific
negative cases that matter (an APPROVED/THIRD_READING document correctly
gets rejected with 404, not silently regressed; a second FAILED hearing
against an already-returned document doesn't create a duplicate
ReturnReason). Full suite: 82 tests, all passing.

**Also found, not fixed** (lower priority, noted for later): a "Mark as
Failed" button in `third_reading.html` whose confirm dialog says
"FAILED" but which actually calls `move_to_disapproved` (sets
`DISAPPROVED`, which *is* properly displayed via `view_disapproved` — not
destructive, just confusing). A separate `fail_measure` view that
actually sets `FAILED` exists in `documents/views.py` but is never linked
from any template — dead code. And `Document.STATUS_CHOICES` only
declares 9 statuses while the code actually assigns at least 5 more
(`DISAPPROVED`, `FAILED`, `UNFINISHED_BUSINESS`, plus dynamic
`'COMMITTEE [n]'` labels) — works today since Django doesn't enforce
`choices` at the DB level for a plain `.save()`, but `get_status_display()`
silently falls back to the raw code for any of these.

## 2026-09-02, same session — LibreOffice actually installed, and a real bug in it found + fixed

The lifecycle audit above led straight to this: user showed a downloaded
PDF for an already-signed-pending ordinance (`DO-013-2026`, doc 123) with
its entire body missing — letterhead/seal/signature blocks all present,
every WHEREAS/SECTION clause gone. Traced to `Document.content` being
empty (0 chars) while the real `.docx` on disk had 91 paragraphs / 7,104
characters fully intact — not data loss, just every checkpoint-sync
attempt for this document's whole history silently failing because
LibreOffice was never installed on this dev machine (a known gap from
much earlier in this session, previously treated as lower-priority).
User installed it.

**Could not run the installer myself** — confirmed directly (not
assumed) that this shell has no admin rights, and LibreOffice's MSI has
a hard-coded `LaunchConditions` check that refuses to run at all without
elevation ("The Installation Wizard cannot be run properly because you
are logged in as a user without sufficient administrator rights") — no
per-user fallback exists. Downloaded the real installer from
`download.documentfoundation.org` (verified byte-for-byte against its
Content-Length) and left it for the user to run themselves with UAC
approval, which I cannot grant non-interactively.

**Real bug found in `documents/libreoffice.py` once it was installed**:
`--convert-to docx` (bare extension, relying on auto-detection) fails
with "no export filter for ...docx found, aborting" — confirmed via
extensive isolated testing (a fresh profile dir, a reused profile dir,
with and without inherited venv environment variables — none of those
were it) that this is specific to **exporting to .docx as a target
format**, regardless of source format. `--convert-to pdf` and
`--convert-to html` both work fine with the bare extension; only docx
needs the filter named explicitly. Fixed: `_EXPLICIT_TARGET_FILTERS =
{"docx": "docx:MS Word 2007 XML"}`, applied to the `--convert-to`
argument only (output-path detection logic untouched, since the output
filename is unaffected). Verified all three directions the app actually
uses (html→docx, docx→html, docx→pdf) work correctly through the real
`libreoffice.convert()` function afterward.

This bug meant `_ensure_docx_exists`'s legacy-content bootstrap and
`download_document_docx` (the "Download DOCX" button) were still broken
even with LibreOffice installed and otherwise working — worth knowing if
either surfaces as broken again.

**Re-verified the previously-flagged-as-unverified assumption**:
`_sync_docx_to_document_content`'s `<b>`→`<strong>`/`<i>`→`<em>`
tag-renaming, confirmed (via a real conversion of a real document) that
LibreOffice's HTML export genuinely does use `<b>`/`<i>`, matching what
the code already assumed. No longer an open question.

**Backfilled stale content**: found every `Document` with a real `.docx`
on disk but near-empty `Document.content` (9 total) and ran the real
`_sync_docx_to_document_content` against each. 6 succeeded with real
content restored (7,808–24,969 chars each), including doc 123 — verified
end-to-end afterward by actually downloading its PDF through the real
view and extracting text from it with pdfplumber: all 4 pages, full
WHEREAS clauses, confirmed present. 3 failed with a different,
inconsistent error ("source file could not be loaded") that didn't
reproduce a clean root cause despite isolated retries (valid zip,
well-formed XML, python-docx opens all three fine) — all three are
today's own throwaway test drafts ("Untitled Draft", "another sample"),
not real work, so left as-is rather than chasing further. Worth
revisiting if this pattern ever shows up on a document that matters.

Full suite: still 82 tests, all passing, after all of the above.

## What's done and verified

**Collabora fully removed, replaced by local LibreOffice subprocess calls**
(`documents/libreoffice.py` — single shared `convert()` function, no Docker,
no container permissions tradeoff). Wired into:
- `documents/wopi.py::_ensure_docx_exists` (HTML→docx bootstrap, only for the
  rare case of legacy content with no docx yet — see `NoDocxYet` below)
- `documents/views.py::_onlyoffice_convert_docx_to_pdf` (PDF snapshots/downloads)
- `documents/views.py::_sync_docx_to_document_content` (docx→HTML, now only
  called at checkpoints, not every save — see below)
- `documents/views.py::download_document_docx` (refactored to use the shared
  module instead of its own duplicate subprocess code)

Docker container stopped/removed, `docker-compose.yml` deleted,
`COLLABORA_SERVER_URL` removed from `.env`/`settings.py`.
**LibreOffice itself is still not installed on this machine** — every path
above degrades gracefully (logs a warning, returns None/raises a typed
exception) rather than crashing, so nothing is broken by its absence, it's
just inactive until installed.

**Blank-document creation fixed** — a brand-new draft (`Document.content ==
''`, no docx yet) used to force an HTML→docx LibreOffice bootstrap just to
show an empty page. Now: `documents/wopi.py::_ensure_docx_exists` raises
`NoDocxYet` for that case, `casualdocs_document_bytes` returns
`404 {"error": "no_docx"}`, and `frontend/casualdocs-entry.jsx` mounts Casual
Docs with `document: createEmptyDocument()` (its own native "start blank" —
confirmed via `@casualoffice/docs/core`'s type defs) instead of trying to
load nonexistent bytes. Page setup (A4 default, matching
`documents/models.py`'s `Document.page_size` default, not Casual Docs' own
US-Letter default) is threaded through via a `pageSetup` option — see
`computeEmptyDocumentOptions()` in `casualdocs-entry.jsx`, wired from
`draft.html` (real page-setup fields) and `referral_drafting_page.html`
(hardcoded to match model defaults, since that page has no Page Setup UI).

**Multi-draft support** — `create_draft`'s GET used to reuse *any*
GHOST-or-DRAFT document as "the" draft to reopen, silently clobbering a
councilor's already-saved work every time they clicked "Create Draft" again.
Now only reuses `status='GHOST'` (untouched placeholders) — a `DRAFT` (saved,
titled) stays independently resumable. `documents/views.py` around line 187.
Verified end-to-end via Django test client (two independent drafts coexist).

**Checkpoint architecture** — the big one, and the source of most open
items below. `Document.content` (HTML) used to resync from the live docx on
*every* autosave via LibreOffice — which silently failed on every single
save once Collabora was removed, since nothing replaced it. Now:
- `documents/wopi.py::casualdocs_save` just writes docx bytes. No sync, no
  LibreOffice dependency on the hot path at all anymore.
- New helpers in `documents/views.py`: `_onlyoffice_checkpoint_sync(doc)`
  (docx→HTML refresh, call *before* mutating `doc.status`) and
  `_onlyoffice_archive_docx(doc)` (copies the working docx to
  `doc_{id}_v{version}.docx`, the docx counterpart to the already-existing
  versioned PDF snapshot — pure file copy, no LibreOffice needed).
- Wired into all 4 real status-transition checkpoints: `create_draft` (file
  action), `create_referred_draft` (submit action), `committee_level/
  views.py::move_to_second_reading`, `documents/views.py::
  move_to_third_reading`.
- Verified end-to-end (not just import-clean): filed a test draft with a
  real docx already on disk, confirmed version bump, Archives row, and
  byte-identical docx archive all landed correctly, all without LibreOffice
  installed (content-sync/PDF-snapshot degraded gracefully as designed).
- Side-effect caught and fixed: the "unsaved work recovered" banner
  (`create_draft` GET) used to detect real content via `ghost.content`,
  which no longer updates on every autosave — added a check for a saved
  `.docx` file existing as the new signal (~line 236).

## Open items — from an independent thorough review, not yet acted on

Ordered by the review's priority, which I agree with except where noted:

1. **IDOR on the editor's own endpoints — `documents/wopi.py`
   `casualdocs_document_bytes`, `casualdocs_save`, `similarity_check`.**
   Only `@login_required`, no `_can_view_document` check (defined
   `documents/views.py:938`, used everywhere else). Any authenticated user
   can fetch or overwrite another councilor's unfiled draft by guessing the
   numeric doc id. Confirmed directly by re-reading the three functions —
   this is real. Fix: import and call `_can_view_document` (or a
   write-specific variant — view-permission and edit-permission may need to
   differ) at the top of each.

2. **`/media/` likely bypasses all Django-level permission checks once
   `DEBUG=False`** (`config/urls.py`) — standard Django pattern, media is
   usually served by the webserver directly in production, never touching
   `_can_view_document` at all. Combined with #1, drafts may have no real
   confidentiality enforcement in production. Not independently re-verified
   this session — check `config/urls.py`'s MEDIA_URL wiring and how
   deployment actually serves `/media/` before assuming this applies here.

3. **`casualdocs_save` reads `request.body` directly** — subject to
   Django's `DATA_UPLOAD_MAX_MEMORY_SIZE` (confirmed unset in
   `config/settings.py`, so the 2.5MB default applies). A docx with a couple
   of inserted images (the editor has Insert Image) can cross that easily →
   400 → the frontend's `postDocx` only `console.error`s it → the user sees
   "saved" but nothing landed. Silent data loss. Not independently tested
   with a real oversized upload this session.

4. **AI Legal Basis reads stale/empty `Document.content` while drafting —
   `documents/views.py:1321`, `ai_legal_basis`.** Direct consequence of the
   checkpoint architecture above: on a brand-new unfiled draft,
   `document.content` is `''` until the first checkpoint (filing) ever
   fires, so clicking "Legal Basis" mid-draft (the button lives right in
   the drafting toolbar) silently degrades to a title-only search. I said
   earlier in this session that AI Legal Basis didn't touch `.content` —
   that was wrong, from grepping the wrong file (`generator.py` instead of
   the caller in `views.py`). Needs either: sync content before the AI call
   (same pattern as `_onlyoffice_checkpoint_sync`, but without the
   status-change side effects), or explicitly surface to the user that
   pre-filing suggestions are title-only.

5. **`ai_inline_check` is `@csrf_exempt`** (`documents/views.py:1374-1377`
   per the review — not independently re-checked). Low severity (read-only),
   but inconsistent with every other POST endpoint. Cheap fix.

6. **Editor-mount JS duplicated across 7 templates**, already drifted (some
   wrap script-loading in a Promise, some don't; only `draft.html` and
   `referral_drafting_page.html` pass `pageSetup`, the rest don't). I hit
   this firsthand doing the page-setup fix — had to make the same change
   twice, differently. Worth extracting a shared `mount-helper.js` or
   template partial; every future fix to this logic otherwise needs to be
   found and applied in up to 7 places by hand.

7. **`documents/views.py` is a ~2,150 line god-file**, imports scattered
   mid-file (`import os`, `import re`, `import json` reappear below the top
   multiple times — confirmed, I saw this navigating the file this
   session). Already partially decomposed once (`wopi.py` was split out);
   natural next splits per the review: `pdf.py` (export + snapshot
   helpers), `legacy.py` (OCR/legacy upload), `ai_views.py` (legal basis +
   inline check).

8. **No centralized state-machine/permission layer** for status
   transitions — role checks (`if request.user.role not in [...]`) are
   copy-pasted per view and may have already drifted (e.g. `
   move_to_third_reading` allows SECRETARIAT/STAFF/ADMIN,
   `approve_measure` allows only SECRETARIAT/STAFF) — could be intentional,
   could be drift; nothing makes the actual policy legible in one place.

9. **Zero test coverage** — `documents/tests.py` is the stock 3-line stub
   (per the review, not independently re-verified this session). Nothing
   exercises `sanitize.py`'s allowlist, the LibreOffice round-trip, or the
   checkpoint-sync ordering (which is subtle and order-dependent by its own
   docstring's admission — see `_onlyoffice_checkpoint_sync`).

**Smaller, lower-priority notes from the review** (not independently
re-verified this session, but plausible given everything else observed):
`pytesseract.pytesseract.tesseract_cmd` hardcoded to a Windows path at
import time (`documents/views.py:1472-1473` per review) will break the app
import on non-Windows deploys; `SESSION_COOKIE_SECURE = False` confirmed at
`config/settings.py:105`; leftover `.ql-editor` (Quill) CSS class naming on
read-only templates, harmless but will confuse a future grep for "quill".

## Two things worth deciding before continuing

- **Should `casualdocs_save`/`casualdocs_document_bytes` reject edits to
  documents outside an actually-editable stage** (e.g. `APPROVED`)? Right
  now there's no status guard at all — currently unreachable by accident
  since no page links to the editor for those states, but the endpoint
  itself doesn't enforce it. Surfaced when a live "round-trip byte test" I
  ran against doc 104 (already `FILED`) succeeded with zero resistance —
  which was itself a process mistake on my part (used a real document
  instead of throwaway test data for a verification check; confirmed
  afterward that nothing was actually altered, since `casualdocs_save`
  never calls `doc.save()`, only writes the file).
- **Unverified, flagged in code**: `_sync_docx_to_document_content`'s
  `<b>`→`<strong>`/`<i>`→`<em>` tag renaming was "confirmed against real
  Collabora output" — but Collabora and LibreOffice may not produce
  identical HTML export markup. Worth re-verifying once LibreOffice is
  actually installed here.

## 2026-09-02: Reference-number lifecycle audit + fix

User asked me to verify the reference-number business rule: draft gets a
number first-come-first-serve at filing, keeps it unchanged through Third
Reading, then gets a *separate* Ordinance/Resolution number at Approval.
Traced every `reference_no` assignment in `documents/views.py`:

- `create_draft`/`create_referred_draft` assign `DO-`/`DR-XXX-YYYY` once
  (guarded by `if not doc.reference_no`), via a COUNT-based query scoped
  to doc_type + year.
- `move_to_first`/`move_to_third_reading` both accepted an optional
  `request.POST.get('reference_no')` that would silently overwrite the
  number if present — checked every template that posts to them
  (`incoming.html`, `floor_amendments.html`'s `thirdReadingForm`) and
  confirmed none actually sends that field, so in practice the number was
  never touched. Still a live, unexercised landmine (a future form change
  or direct API call could silently clobber it with no uniqueness check),
  so removed both overrides entirely — `move_to_first`/
  `move_to_third_reading` no longer read `reference_no` from POST at all.
- `approve_measure` correctly computes an independent `status='APPROVED'`
  count and replaces `reference_no` with `"Ordinance No. N"` /
  `"Resolution No. N"` — matches the intended rule, left as-is
  functionally.
- Also checked whether `create_draft`'s `DR-` pool (used for any
  non-ORDINANCE doc_type) and `create_referred_draft`'s hardcoded
  `doc_type='RESOLUTION'` pool could produce duplicate numbers across the
  two entry points — they use the identical `doc_type='RESOLUTION'` +
  `status__in=[...]` + year filter, so they draw from one consistent
  pool. Not a bug.

**Real bug found and fixed**: all three COUNT-based number assignments
(`create_draft`, `create_referred_draft`, `approve_measure`) used a plain
`Document.objects.filter(...).count() + 1` with no locking — two
concurrent requests (two staff filing, or two measures approved, in the
same doc_type+year at the same moment) could read the same count before
either commits, producing duplicate reference numbers. `select_for_update()`
alone wouldn't have closed this fully (the first document of a
doc_type/year has no existing row to lock against). Fixed with a new
`_acquire_sequence_lock(*parts)` helper (`documents/views.py`) that takes
a Postgres advisory lock (`pg_advisory_xact_lock`, keyed by an md5 hash of
the parts) for the duration of a `transaction.atomic()` block wrapping
the count-read + `.save()` — serializes concurrent assignment for the
same bucket regardless of whether any rows exist yet to lock. Restructured
all three call sites so the object's `.save()` happens *inside* the locked
block (required — the lock only prevents a stale read while it's held,
so the persist has to land before the lock releases). Verified: `python
manage.py test documents committee_level --keepdb` — 49/49 pass, including
the full-lifecycle walkthrough that exercises `create_draft` through
`approve_measure` and asserts the final `Ordinance No. 1` reference number.
