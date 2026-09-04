/*
 * Casual Docs (@casualoffice/docs) editor entry — the single editor used
 * across every document-editing/viewing surface in this app (drafting,
 * committee/floor amendments via `mode: 'suggesting'` track changes, and
 * read-only viewing via `mode: 'viewing'`), replacing the earlier Collabora
 * (WOPI) integration entirely. See documents/wopi.py's
 * casualdocs_document_bytes/casualdocs_save for the two plain
 * session-authenticated endpoints this talks to (no WOPI — Casual Docs
 * takes docx bytes in via `documentBuffer` and hands edited bytes back out
 * via `onSave`, so there's no protocol layer to implement here).
 */
import React from 'react';
import { createRoot } from 'react-dom/client';
import { DocxEditor } from '@casualoffice/docs/react';
import { createEmptyDocument } from '@casualoffice/docs/core';
import '@casualoffice/docs/styles.css';

// Standard OOXML page dimensions in twips (1440/inch) — same values Word
// itself uses for these presets. This app's Document model defaults to A4
// (documents/models.py's PAGE_SIZE_CHOICES/page_size), the standard for
// Philippine government paperwork, which is NOT createEmptyDocument()'s
// own built-in default (US Letter) — so a brand-new blank draft needs
// these passed in explicitly or it silently starts in the wrong page size.
const PAGE_SIZE_TWIPS = {
    A4: { width: 11906, height: 16838 },
    LETTER: { width: 12240, height: 15840 },
    LEGAL: { width: 12240, height: 20160 },
};
const TWIPS_PER_CM = 1440 / 2.54;

function computeEmptyDocumentOptions(pageSetup) {
    if (!pageSetup) return undefined;
    const base = PAGE_SIZE_TWIPS[pageSetup.pageSize] || PAGE_SIZE_TWIPS.A4;
    const landscape = pageSetup.orientation === 'LANDSCAPE';
    const toTwips = (cm, fallbackCm) => Math.round((Number(cm) || fallbackCm) * TWIPS_PER_CM);
    return {
        pageWidth: landscape ? base.height : base.width,
        pageHeight: landscape ? base.width : base.height,
        marginTop: toTwips(pageSetup.marginTopCm, 2.54),
        marginBottom: toTwips(pageSetup.marginBottomCm, 2.54),
        marginLeft: toTwips(pageSetup.marginLeftCm, 2.54),
        marginRight: toTwips(pageSetup.marginRightCm, 2.54),
    };
}

// DocumentChunk.chunk_text (the source of `best.snippet` from
// /ai-inline-check/) is stored as raw HTML — confirmed directly against a
// real row earlier ('<p class="">WHEREAS, ...</p>...'). Strips it down to
// plain text before it goes into a comment, which renders it verbatim
// (comments are plain text, not HTML) — without this the tags showed up
// literally in the comment (confirmed live).
function stripHtml(html) {
    const el = document.createElement('div');
    el.innerHTML = html || '';
    return (el.textContent || '').replace(/\s+/g, ' ').trim();
}

// Small floating "Checking similarity…" pill — anchored to the viewport
// (position: fixed) rather than the editor container, since different
// pages mount the editor into containers with different positioning
// contexts and layout structures; viewport-fixed is consistent everywhere
// without depending on any of them. Appended to document.body, outside
// React's own tree, since it's purely a transient status indicator with no
// need to participate in reconciliation. Shown only once a check actually
// starts querying the server — not during the debounce wait — so it
// doesn't flicker on every keystroke pause.
function createInlineCheckIndicator() {
    const el = document.createElement('div');
    el.style.cssText = `
        position: fixed; bottom: 20px; left: 20px; z-index: 9999;
        display: none; align-items: center; gap: 8px;
        background: #1e293b; color: #cbd5e1; font-size: 11px; font-weight: 600;
        padding: 8px 14px; border-radius: 999px; box-shadow: 0 4px 12px rgba(0,0,0,0.2);
        pointer-events: none;
    `;
    const spinner = document.createElement('span');
    spinner.style.cssText = `
        width: 11px; height: 11px; border-radius: 50%;
        border: 2px solid rgba(203,213,225,0.35); border-top-color: #cbd5e1;
        animation: casualdocsInlineCheckSpin 0.7s linear infinite; flex-shrink: 0;
    `;
    const label = document.createElement('span');
    label.textContent = 'Checking for similar legislation…';
    el.appendChild(spinner);
    el.appendChild(label);
    document.body.appendChild(el);

    if (!document.getElementById('casualdocs-inline-check-spin-keyframes')) {
        const style = document.createElement('style');
        style.id = 'casualdocs-inline-check-spin-keyframes';
        style.textContent = '@keyframes casualdocsInlineCheckSpin { to { transform: rotate(360deg); } }';
        document.head.appendChild(style);
    }

    return {
        show() { el.style.display = 'flex'; },
        hide() { el.style.display = 'none'; },
        destroy() { el.remove(); },
    };
}

function postDocx(saveUrl, csrfToken, buffer) {
    return fetch(saveUrl, {
        method: 'POST',
        headers: {
            'Content-Type': 'application/octet-stream',
            'X-CSRFToken': csrfToken,
        },
        body: buffer,
    });
}

// Casual Docs only clears its own internal "unsaved changes" flag (the one
// driving its beforeunload "Leave site?" prompt and the "Unsaved changes —
// Restore/Discard" banner) from its own Ctrl+S / File -> Save UI action
// (handleDownloadDocument in its source), not from the plain DocxEditorRef
// .save()/.exportDocx() methods — those serialize + call onSave but leave
// the dirty flag set. There's no ref-exposed method that does both, so
// forceSave() below dispatches a synthetic Ctrl+S keydown to go through the
// editor's real save path instead of calling ref.save() directly — that's
// what makes onSave fire *and* clears the dirty flag, matching what a user
// pressing Ctrl+S themselves gets. A saveWaiters queue (passed in per-mount
// as saveWaitersRef, see mount() below — deliberately NOT a module-level
// variable shared across every editor this bundle ever mounts on a page,
// which would let one instance's onSave incorrectly resolve a different
// instance's pending forceSave() if more than one is ever mounted at once
// in the same tab) lets forceSave() await the network round trip that
// synthetic keypress triggers, since onSave itself is fire-and-forget from
// the editor's point of view.

const INLINE_CHECK_URL = '/ai-inline-check/';
const INLINE_CHECK_DEBOUNCE_MS = 2500;

// Debounced (Google-Docs-style "save shortly after you stop typing")
// autosave trigger — see scheduleAutosave() in mount() below. Deliberately
// longer than INLINE_CHECK_DEBOUNCE_MS: unlike Google Docs' own autosave,
// forceSave() here exports and uploads the *whole* document on every call
// (no incremental/delta sync — Casual Docs produces a complete .docx per
// save), so debouncing too aggressively on a large document would mean
// frequent multi-MB round trips during a normal typing burst with brief
// pauses between sentences. 4s is long enough to skip past those brief
// pauses while still saving well before the old 30s-only interval would
// have. The 30s periodic timer (still present, see mount()) is now a
// safety-net floor rather than the primary save trigger — it only matters
// if someone types continuously enough that this debounce never gets a
// quiet moment to fire.
const AUTOSAVE_DEBOUNCE_MS = 4000;

// Bounds how long forceSave() will wait for all currently in-flight
// inline checks before giving up and saving anyway — see forceSave()'s
// use of inFlightInlineChecks in mount() below. This closes a real race,
// confirmed live (not theoretical): the inline check's own network round
// trip (a fetch to INLINE_CHECK_URL, which does an embedding + retrieval
// call server-side — see documents/views.py's ai_inline_check) can easily
// take longer than AUTOSAVE_DEBOUNCE_MS, especially with a slow or
// crash-looping Ollama backend. Without waiting, an autosave firing mid
// check captures a document snapshot from *before* the check's
// addComment() calls land, silently saving a version missing the flag —
// confirmed directly server-side across several saves of the same
// document, some with the comment and some without, purely by chance of
// which raced which. This wait is intentionally bounded rather than
// unconditional: a stalled Ollama call shouldn't hang every save
// indefinitely, so if the check hasn't finished by this point, the save
// proceeds anyway rather than blocking forever — same fallback
// philosophy as forceSave()'s own overall 30s timeout below.
const INLINE_CHECK_MAX_WAIT_MS = 8000;

// Cheap non-cryptographic string fingerprint — only used to dedupe "have I
// already flagged this exact paragraph text" client-side, not for anything
// security-sensitive, so djb2 is plenty.
function fingerprint(text) {
    let hash = 5381;
    for (let i = 0; i < text.length; i++) {
        hash = ((hash << 5) + hash + text.charCodeAt(i)) | 0;
    }
    return hash.toString(36);
}

// Originally tried reading paragraph text off the rendered DOM via a
// `data-para-id` attribute found referenced in the built package — turned
// out that's not present on the live editable DOM (confirmed live:
// querySelectorAll found 0 elements on a document with clearly-rendered
// paragraphs), likely only used on a separate export/print rendering path.
// Walking the Document model directly instead — more code (the content
// model is a deeply recursive union of runs/hyperlinks/tracked-changes/
// fields), but it's the actual documented, stable shape, not a DOM
// implementation detail. recurseText flattens any node with a `.text`
// string or a nested `.content` array, which covers Run/Hyperlink/
// Insertion/Deletion generically without needing to special-case every
// union member — good enough for a plain-text similarity check, where
// formatting doesn't matter anyway.
function recurseText(node, out) {
    if (!node) return;
    if (typeof node.text === 'string') {
        out.push(node.text);
        return;
    }
    if (Array.isArray(node.content)) {
        node.content.forEach((child) => recurseText(child, out));
    }
}

// Fixed letterhead lines identical across every document in this system
// (see templates/councilors/view_draft.html / modal_document_viewer.html
// for the same three lines). Individually they clear the 20-char minimum
// despite being pure boilerplate with no legislative content, so without
// this they get sent to the similarity check and match against whatever
// unrelated document happens to be nearest in embedding space purely
// because every document shares this same header text — confirmed live
// (5 different paragraphs all "matched" the same unrelated salary
// ordinance; all 5 were letterhead lines, not real content).
const _BOILERPLATE_PARAGRAPHS = new Set([
    'republic of the philippines',
    'city of san juan, metro manila',
    'office of the sangguniang panlungsod',
]);

// A paragraph freshly created this session (via Enter, before any save)
// may not have a `paraId` yet — that appears to only get assigned at docx
// serialize time — so it's skipped here (nothing to anchor addComment to
// until it exists). Editing text *within* an already-loaded paragraph is
// unaffected, since that paragraph's paraId was already there.
function extractCheckableParagraphs(doc) {
    const out = [];
    const body = doc && doc.package && doc.package.document && doc.package.document.content;
    if (!Array.isArray(body)) return out;
    body.forEach((node) => {
        if (!node || node.type !== 'paragraph' || !node.paraId) return;
        const parts = [];
        (node.content || []).forEach((child) => recurseText(child, parts));
        const text = parts.join('').trim();
        if (text.length >= 20 && !_BOILERPLATE_PARAGRAPHS.has(text.toLowerCase())) {
            out.push({ id: node.paraId, text });
        }
    });
    return out;
}

// Same top-level body walk as extractCheckableParagraphs, but every
// paragraph in document order (no length/boilerplate filtering) — used
// by amending_table.html's original/amended comparison to find the
// nearest paragraph *before* a newly-inserted one that still exists in
// the original panel, so it can show "new section inserted here" at the
// right spot instead of just a generic "this is new" notice. Body-only
// (not table cells), matching what document_original_html's own
// docx.paragraphs walk on the Python side already covers — the two
// sides only ever need to agree on the same subset.
function extractOrderedParaIds(doc) {
    const out = [];
    const body = doc && doc.package && doc.package.document && doc.package.document.content;
    if (!Array.isArray(body)) return out;
    body.forEach((node) => {
        if (node && node.type === 'paragraph' && node.paraId) out.push(node.paraId);
    });
    return out;
}

function CasualDocsApp({ fileUrl, directBuffer, saveUrl, csrfToken, authorName, mode, onChange, onDirtyChange, onSaved, onError, onReady, docxRef, emptyDocumentOptions, saveWaitersRef }) {
    // undefined = still loading, null = confirmed-blank (no docx yet, mount
    // Casual Docs' own new-document state), File/Blob/ArrayBuffer = real
    // docx bytes (fetched from fileUrl, or handed in directly via
    // directBuffer — see the effect below).
    const [buffer, setBuffer] = React.useState(undefined);
    const [loadError, setLoadError] = React.useState(null);

    // mode is a controlled prop (per @casualoffice/docs' own react.d.ts:
    // "when documentMode/mode is a controlled prop the host is expected to
    // react to onModeChange and update the prop"). Without local state
    // here, every re-render of this component (e.g. the buffer finishing
    // its fetch above) re-passes the original hardcoded `mode` prop,
    // silently snapping the editor back to it and making its own built-in
    // "Switch to editing" control appear broken — confirmed live. Seeding
    // from the prop keeps a page's initial requested mode (e.g.
    // 'suggesting' for track-changes pages) while letting the editor's own
    // toggle actually take effect afterward.
    const [currentMode, setCurrentMode] = React.useState(mode || 'editing');

    React.useEffect(() => {
        let cancelled = false;

        // Bytes already in hand (e.g. a file just picked in a local
        // <input type="file">, previewed before it's ever sent to the
        // server) — no fetch needed or possible; there's no fileUrl for
        // this case at all. See barangay.html's upload-measure preview.
        if (directBuffer !== undefined && directBuffer !== null) {
            setBuffer(directBuffer);
            return () => { cancelled = true; };
        }

        fetch(fileUrl, { headers: { 'X-Requested-With': 'XMLHttpRequest' } })
            .then(async (resp) => {
                if (!resp.ok) {
                    const detail = await resp.clone().json().catch(() => null);
                    // A brand-new document with nothing saved yet isn't a
                    // failure — Casual Docs creates its own blank document
                    // client-side when documentBuffer is null (see
                    // documents/wopi.py::NoDocxYet).
                    if (detail && detail.error === 'no_docx') return null;
                    throw new Error(detail && detail.detail ? detail.detail : `Document fetch returned ${resp.status}`);
                }
                return resp.arrayBuffer();
            })
            .then((buf) => { if (!cancelled) setBuffer(buf); })
            .catch((err) => { if (!cancelled) setLoadError(err); });
        return () => { cancelled = true; };
    }, [fileUrl, directBuffer]);

    // Viewing mode has no saveUrl (read-only pages don't wire one up) — the
    // editor is read-only there anyway, so onSave never fires in practice.
    const handleOnSave = React.useCallback((editedBuffer) => {
        if (!saveUrl) return;
        console.log('[CasualDocs] onSave fired, buffer bytes =', editedBuffer && editedBuffer.byteLength);
        const postPromise = postDocx(saveUrl, csrfToken, editedBuffer)
            .then((resp) => {
                console.log('[CasualDocs] save POST response status =', resp.status);
                if (!resp.ok) throw new Error(`Save endpoint returned ${resp.status}`);
                return resp.json().catch(() => null);
            })
            .then((data) => {
                // saved_at is the save endpoint's own record of when this
                // landed (see documents/wopi.py::casualdocs_save and
                // committee_level/views.py::committee_report_save) — not
                // just "the request resolved," so a caller building a
                // "Saved" indicator reflects what the server actually
                // persisted, not merely that the network round trip
                // finished. Missing/unparseable body is a non-fatal
                // no-signal case, not a save failure.
                if (data && data.saved_at && onSaved) onSaved(data.saved_at);
            })
            .catch((err) => {
                console.error('Casual Docs save failed:', err);
                if (onError) onError(err);
                throw err;
            });
        const waiters = saveWaitersRef.current;
        saveWaitersRef.current = [];
        waiters.forEach((resolve) => resolve(postPromise));
    }, [saveUrl, csrfToken, onSaved, onError, saveWaitersRef]);

    // Stable reference, computed unconditionally (hooks can't be called
    // conditionally) but only actually used below when buffer === null —
    // a brand-new document with nothing saved yet. Passing `documentBuffer:
    // null` alone renders Casual Docs' own "No document loaded" empty
    // state rather than creating a blank document (confirmed directly
    // against its behavior, not assumed) — `document: createEmptyDocument()`
    // is the API it actually expects for "start blank" (see
    // documents/wopi.py::NoDocxYet).
    const blankDocument = React.useMemo(() => createEmptyDocument(emptyDocumentOptions), [emptyDocumentOptions]);

    if (loadError) {
        return React.createElement('div', {
            style: { padding: 40, textAlign: 'center', color: '#dc2626', fontSize: 13, fontWeight: 600 },
        }, 'The document failed to load — ', loadError.message);
    }
    if (buffer === undefined) {
        return React.createElement('div', {
            style: { padding: 40, textAlign: 'center', color: '#94a3b8', fontSize: 13, fontWeight: 600 },
        }, 'Loading document…');
    }

    return React.createElement(DocxEditor, {
        ref: docxRef,
        ...(buffer === null ? { document: blankDocument } : { documentBuffer: buffer }),
        author: authorName,
        mode: currentMode,
        onModeChange: setCurrentMode,
        onSave: handleOnSave,
        onChange: onChange,
        onDirtyChange: onDirtyChange,
        onError: onError,
        onReady: onReady,
        style: { width: '100%', height: '100%' },
    });
}

window.CasualDocsEditor = {
    /**
     * Mounts the Casual Docs editor into containerEl.
     * options: { fileUrl, documentBuffer, saveUrl, csrfToken, authorName, mode, pageSetup, onDirtyChange, onSaved, onError }
     *   mode: 'editing' (default) | 'suggesting' (track changes) | 'viewing' (read-only)
     *   documentBuffer: a File/Blob/ArrayBuffer already in hand — skips
     *            fetching fileUrl entirely (fileUrl can be omitted when
     *            this is set). For previewing a file that hasn't been
     *            uploaded anywhere yet, e.g. straight from a local
     *            <input type="file"> — see barangay.html's upload-measure
     *            modal. Always pair with mode: 'viewing' and no saveUrl;
     *            there's nothing to save back to.
     *   onSaved(savedAt): fires after each successful save (autosave or
     *            forceSave) with the ISO timestamp the save endpoint itself
     *            recorded — see documents/wopi.py::casualdocs_save and
     *            committee_level/views.py::committee_report_save. Not
     *            called if the save failed or the response had no
     *            saved_at (older/unrelated endpoints).
     *   saveUrl: omit for 'viewing' pages — no save wiring (including
     *            autosave, below) is set up without it. When present,
     *            autosave fires ~AUTOSAVE_DEBOUNCE_MS after the user stops
     *            typing (Google-Docs-style debounce), with a 30s interval
     *            as a fallback floor for a session that never pauses long
     *            enough for the debounce to fire.
     *   pageSetup: { pageSize: 'A4'|'LETTER'|'LEGAL', orientation: 'PORTRAIT'|'LANDSCAPE',
     *                marginTopCm, marginBottomCm, marginLeftCm, marginRightCm } — only
     *              matters for a brand-new document with nothing saved yet (see
     *              documents/wopi.py::NoDocxYet); ignored once a real docx exists,
     *              since that file already carries its own page setup.
     * Returns a controller: { forceSave(): Promise<void>, destroy() } — same
     * shape as the old CollaboraEditor.mount()'s controller had, so callers
     * can use activeEditor.forceSave() the same way regardless of mode.
     */
    mount(containerEl, options) {
        options = options || {};
        const root = createRoot(containerEl);
        let resolveReady;
        const readyPromise = new Promise((resolve) => { resolveReady = resolve; });
        let isDirty = false;
        let saveInFlight = false;
        let autosaveTimer = null;
        let autosaveDebounceTimer = null;
        let editorApi = null;
        let inlineCheckTimer = null;
        let latestDocument = null;
        // Every inline check currently between its fetch and its
        // addComment() calls — see scheduleInlineCheck below (where each
        // one is added/removed) and forceSave() (where all of them are
        // awaited together, bounded by INLINE_CHECK_MAX_WAIT_MS) to close
        // the save-races-ahead-of-addComment race described at that
        // constant's definition. A Set, not a single variable — typing
        // continuously reschedules the debounce, so an earlier check can
        // still be mid-flight (waiting on a slow Ollama call) when a later
        // one starts; tracking only "the latest one" would let forceSave()
        // stop waiting as soon as the newest check settles while an older
        // one — with its own pending addComment() calls — is still running
        // in the background. Confirmed live: this was still losing
        // comments after the first (single-variable) version of this fix.
        const inFlightInlineChecks = new Set();
        const flaggedParagraphs = new Set(); // `${paraId}::${fingerprint}` already commented this session
        const inlineCheckIndicator = options.saveUrl ? createInlineCheckIndicator() : null;

        // markReady is idempotent and can be triggered by either of two
        // independent signals — whichever fires first wins:
        //   1. the onReady prop callback (the "normal" path)
        //   2. polling docxRef.current, populated by React's own ref
        //      attachment (useImperativeHandle) as soon as DocxEditor
        //      mounts. Added after onReady was observed to sometimes never
        //      fire at all, even after 30+ seconds, on a document that was
        //      visibly loaded and editable the whole time (confirmed live —
        //      both this and forceSave() timed out waiting on it in the
        //      same session). That points at a race in Casual Docs' own
        //      "fire onReady once isLoading flips false" effect, not
        //      something fixable by waiting longer on our end. The ref is
        //      populated by a separate, more basic React mechanism
        //      (useImperativeHandle's own effect), so it isn't subject to
        //      whatever ordering bug affects the onReady-firing effect.
        const docxRef = { current: null };
        // Per-instance save-wait queue — see this file's earlier comment
        // (near postDocx) for why this must not be a module-level variable
        // shared across every mounted editor on the page.
        const saveWaitersRef = { current: [] };
        let readyResolved = false;
        function markReady(api) {
            if (readyResolved) return;
            readyResolved = true;
            editorApi = api;
            resolveReady();
        }
        const readyPollTimer = setInterval(() => {
            if (docxRef.current) {
                console.log('[CasualDocs] editor ready (detected via ref poll).');
                markReady(docxRef.current);
                clearInterval(readyPollTimer);
            }
        }, 300);

        async function runInlineCheck() {
            console.log('[CasualDocs] inline check firing…');
            // Same fix as forceSave() needed earlier: don't check-and-bail
            // on editorApi being set yet, actually wait for it. onReady can
            // take longer than the 2.5s debounce on a bigger document, and
            // without this the very first check after a fresh page load
            // just gives up with no retry — confirmed live (console showed
            // "editor not ready yet, skipping" on a session that had
            // clearly been editing for minutes, meaning the *page* was
            // freshly reloaded even though the underlying draft wasn't).
            const timeout = new Promise((resolve) => setTimeout(resolve, 15000));
            await Promise.race([readyPromise, timeout]);
            if (!editorApi) {
                console.log('[CasualDocs] inline check: editor never became ready, skipping.');
                return;
            }
            const allParas = extractCheckableParagraphs(latestDocument);
            console.log('[CasualDocs] inline check: found', allParas.length, 'checkable paragraph(s) in the document model:', allParas.map((p) => p.id));
            const candidates = allParas.filter((p) => !flaggedParagraphs.has(`${p.id}::${fingerprint(p.text)}`));
            console.log('[CasualDocs] inline check:', candidates.length, 'not-yet-checked candidate(s).');
            if (!candidates.length) return;

            let data;
            if (inlineCheckIndicator) inlineCheckIndicator.show();
            try {
                const resp = await fetch(INLINE_CHECK_URL, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json', 'X-CSRFToken': options.csrfToken },
                    body: JSON.stringify({ paragraphs: candidates }),
                });
                console.log('[CasualDocs] inline check: response status =', resp.status);
                if (!resp.ok) return;
                data = await resp.json();
                console.log('[CasualDocs] inline check: results =', data.results);
            } catch (err) {
                console.error('[CasualDocs] inline check request failed:', err);
                return;
            } finally {
                if (inlineCheckIndicator) inlineCheckIndicator.hide();
            }

            (data.results || []).forEach((r) => {
                const candidate = candidates.find((c) => c.id === r.id);
                if (!candidate) return;
                const key = `${r.id}::${fingerprint(candidate.text)}`;
                flaggedParagraphs.add(key); // mark checked either way — don't re-request an unchanged paragraph that came back clean
                if (!r.matches || !r.matches.length) return;

                const best = r.matches[0];
                const label = best.reference_no ? `${best.title} (${best.reference_no})` : best.title;
                let snippetText = stripHtml(best.snippet);
                if (snippetText.length > 200) snippetText = snippetText.slice(0, 200).trim() + '…';
                const summary = `Possible resemblance to existing legislation — "${label}": "${snippetText}"`;
                console.log('[CasualDocs] inline check: match found for paragraph', r.id, '->', label);
                try {
                    const commentId = editorApi.addComment({ paraId: r.id, text: summary, author: 'Similarity Check (AI)' });
                    console.log('[CasualDocs] inline check: addComment returned', commentId);
                } catch (err) {
                    console.error('[CasualDocs] addComment for inline match failed:', err);
                }
            });
        }

        function scheduleInlineCheck(doc) {
            latestDocument = doc;
            console.log('[CasualDocs] onChange fired, scheduling inline check…');
            if (inlineCheckTimer) clearTimeout(inlineCheckTimer);
            inlineCheckTimer = setTimeout(() => {
                // Added from the moment the debounce actually fires (not
                // from scheduling) — covers the real network round trip +
                // addComment() calls, which is exactly the window forceSave()
                // needs to wait out. Removed once settled, success or not,
                // so a failed/empty check doesn't leave forceSave() waiting
                // on a promise that's already done its job. Uses a stable
                // `check` reference (not `runInlineCheck()` called twice)
                // so the Set holds the exact same promise object add and
                // delete operate on.
                const check = runInlineCheck().finally(() => {
                    inFlightInlineChecks.delete(check);
                });
                inFlightInlineChecks.add(check);
            }, INLINE_CHECK_DEBOUNCE_MS);
        }

        // Debounced save-on-change — the actual "save shortly after you
        // stop typing" trigger now (see AUTOSAVE_DEBOUNCE_MS above); the
        // 30s interval near the end of mount() is only a fallback floor
        // for a session that never has a quiet moment. References
        // controller.forceSave() by closure — controller isn't assigned
        // until after root.render() below, but this only ever actually
        // runs from a later onChange event (a user edit), never
        // synchronously during the initial render, so it's always
        // assigned by the time this fires — same pattern scheduleInlineCheck/
        // runInlineCheck already use for editorApi above.
        function scheduleAutosave() {
            if (autosaveDebounceTimer) clearTimeout(autosaveDebounceTimer);
            autosaveDebounceTimer = setTimeout(() => {
                if (isDirty && !saveInFlight) {
                    console.log('[CasualDocs] debounced autosave firing…');
                    controller.forceSave();
                }
            }, AUTOSAVE_DEBOUNCE_MS);
        }

        // Single onChange handler feeding both debounced behaviors — Casual
        // Docs' DocxEditor only takes one onChange prop, so this is the one
        // place both the inline-check debounce and the autosave debounce
        // restart on every edit.
        function handleEditorChange(doc) {
            scheduleInlineCheck(doc);
            scheduleAutosave();
        }

        const emptyDocumentOptions = computeEmptyDocumentOptions(options.pageSetup);

        root.render(
            React.createElement(CasualDocsApp, {
                fileUrl: options.fileUrl,
                directBuffer: options.documentBuffer,
                saveUrl: options.saveUrl,
                csrfToken: options.csrfToken,
                authorName: options.authorName,
                mode: options.mode,
                emptyDocumentOptions: emptyDocumentOptions,
                // Feeds two independent debounced behaviors (see
                // handleEditorChange above): the live "inline" similarity
                // check — separate from and much lighter than the
                // post-save one (documents/views.py's ai_inline_check: 6s
                // per-paragraph timeout + 5-minute server-side result
                // caching by text hash, purpose-built for being called
                // while someone's actively typing, unlike the post-save
                // check's 20s-timeout embedding calls run once per save) —
                // and the debounced autosave. Only wired when there's a
                // saveUrl — 'viewing' pages have nothing to check and no
                // author actively drafting against.
                onChange: options.saveUrl ? handleEditorChange : undefined,
                onDirtyChange: (dirty) => {
                    isDirty = dirty;
                    if (options.onDirtyChange) options.onDirtyChange(dirty);
                },
                onSaved: options.onSaved,
                onError: options.onError,
                onReady: (api) => {
                    console.log('[CasualDocs] editor reported ready (via onReady callback).');
                    markReady(api);
                },
                docxRef: docxRef,
                saveWaitersRef: saveWaitersRef,
            })
        );

        const controller = {
            async forceSave() {
                if (saveInFlight) {
                    console.log('[CasualDocs] forceSave() called while a save is already in flight — skipping duplicate.');
                    return;
                }
                saveInFlight = true;
                console.log('[CasualDocs] forceSave() called.');
                // One generous timeout around the *whole* sequence (editor
                // readiness -> synthetic Ctrl+S -> save POST), not separate
                // short timeouts on each stage. Two stacked short timeouts
                // (15s + 5s) turned out to race against real completion
                // instead of just bounding a genuine hang: whichever one
                // lost gave up and let the caller call form.submit() while
                // the actual save was still quietly finishing in the
                // background (confirmed live — waiting longer at the
                // browser's "Leave site?" prompt before confirming let the
                // in-flight save land and the edit persisted; leaving
                // immediately raced it and lost the edit). A single wide
                // bound removes that race instead of just widening it.
                const whole = (async () => {
                    // Wait out any in-flight inline check first — see
                    // INLINE_CHECK_MAX_WAIT_MS's definition above for why
                    // this matters (a save landing mid-check can silently
                    // capture the document from before addComment() ran,
                    // confirmed directly server-side). Bounded, and counts
                    // against this same function's overall 30s timeout
                    // below rather than extending it — a slow check just
                    // eats into the same budget the rest of the save
                    // already has.
                    if (inFlightInlineChecks.size > 0) {
                        console.log(`[CasualDocs] forceSave() waiting for ${inFlightInlineChecks.size} in-flight inline check(s) to finish first…`);
                        await Promise.race([
                            Promise.all(Array.from(inFlightInlineChecks)),
                            new Promise((resolve) => setTimeout(resolve, INLINE_CHECK_MAX_WAIT_MS)),
                        ]);
                    }
                    await readyPromise;
                    const waitForSave = new Promise((resolve) => saveWaitersRef.current.push(resolve));
                    console.log('[CasualDocs] editor ready, dispatching synthetic Ctrl+S…');
                    document.dispatchEvent(new KeyboardEvent('keydown', {
                        key: 's', ctrlKey: true, metaKey: true, bubbles: true, cancelable: true,
                    }));
                    const postPromise = await waitForSave;
                    await postPromise;
                })();
                const timeout = new Promise((_, reject) =>
                    setTimeout(() => reject(new Error('Casual Docs save timed out after 30s — the editor may have failed to load, or the save request genuinely hung')), 30000));
                try {
                    await Promise.race([whole, timeout]);
                    console.log('[CasualDocs] forceSave completed successfully.');
                } catch (err) {
                    console.error('[CasualDocs] forceSave failed:', err);
                    if (options.onError) options.onError(err);
                } finally {
                    saveInFlight = false;
                }
            },
            destroy() {
                if (autosaveTimer) clearInterval(autosaveTimer);
                if (autosaveDebounceTimer) clearTimeout(autosaveDebounceTimer);
                if (inlineCheckTimer) clearTimeout(inlineCheckTimer);
                if (inlineCheckIndicator) inlineCheckIndicator.destroy();
                clearInterval(readyPollTimer);
                root.unmount();
            },
            // Current cursor/selection's paraId, or null if the editor isn't
            // ready yet or nothing is selected — used by the original/edited
            // side-by-side comparison (amending_table.html) to highlight the
            // matching paragraph in the read-only panel.
            getSelection() {
                return docxRef.current ? docxRef.current.getSelection() : null;
            },
            // Scrolls the live editor to the paragraph with the given Word
            // w14:paraId. Returns false if the editor isn't ready or no such
            // paragraph exists.
            scrollToParaId(paraId) {
                return docxRef.current ? docxRef.current.scrollToParaId(paraId) : false;
            },
            // Every paragraph's w14:paraId, in document order — lets a host
            // (amending_table.html) work out where a paraId with no match in
            // a separately-loaded "original" snapshot actually sits relative
            // to paragraphs that *do* still exist there, e.g. to point out
            // "new section inserted here" instead of just "this is new".
            getOrderedParaIds() {
                if (!docxRef.current) return [];
                const doc = docxRef.current.getContent ? docxRef.current.getContent() : null;
                return extractOrderedParaIds(doc);
            },
        };

        // Periodic autosave floor — the primary save trigger is now
        // scheduleAutosave()'s debounce (fires ~AUTOSAVE_DEBOUNCE_MS after
        // the user stops typing, see handleEditorChange above), matching
        // Google Docs' "save shortly after a change" feel rather than a
        // blind poll. This interval only still matters as a fallback for
        // a session that types continuously enough that the debounce never
        // gets a quiet moment to fire — without it, that specific case
        // would have no server-side safety net at all (Casual Docs' own
        // autosave, driving the "Unsaved changes" restore banner, is local
        // IndexedDB only — never leaves the browser). Its own isDirty
        // check means it's a genuine no-op whenever the debounce already
        // saved recently, not a redundant duplicate save. Skipped entirely
        // for 'viewing' pages, which never pass a saveUrl. Reuses
        // forceSave() itself rather than a separate save path, so this
        // gets the same race-condition fix and error handling for free.
        if (options.saveUrl) {
            autosaveTimer = setInterval(() => {
                if (isDirty && !saveInFlight) {
                    console.log('[CasualDocs] periodic autosave floor firing…');
                    controller.forceSave();
                }
            }, 30000);
        }

        return controller;
    },
};
