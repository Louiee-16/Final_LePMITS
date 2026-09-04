/*
 * Shared "load the Casual Docs bundle, mount the editor, wire the
 * post-save similarity check" helper.
 *
 * Every page that mounts Casual Docs (templates/documents/draft.html,
 * templates/councilors/view_draft.html,
 * templates/documents/referral_drafting_page.html,
 * templates/documents/tracking/floor_amendments.html,
 * templates/documents/committee_level/amending_table.html,
 * templates/documents/modal_document_viewer.html, and
 * templates/documents/tracking/document_view.html) used to reimplement
 * this independently, and had already drifted — some wrapped the bundle
 * script load in a Promise and some didn't, only two of the five editing
 * pages actually wired the post-save similarity check (see
 * documents/wopi.py::similarity_check) via onDirtyChange, and only two
 * passed a pageSetup. This is the one place that logic lives now; fixing
 * it here fixes it everywhere.
 *
 * Usage:
 *   <script src="{% static 'js/document_editors/casualdocs-mount.js' %}"></script>
 *   ...
 *   const controller = await CasualDocsMount.mount(containerEl, {
 *     docId,       // required whenever saveUrl is set — used for the
 *                  // similarity-check POST URL
 *     fileUrl, saveUrl, csrfToken, authorName, mode, pageSetup,
 *     documentBuffer,              // File/Blob/ArrayBuffer already in hand —
 *                                  // skips fetching fileUrl entirely; see
 *                                  // barangay.html's upload preview
 *     cssUrl, jsUrl,               // static URLs for the bundle's assets
 *     checkSimilarity: true|false, // default: true whenever saveUrl is set
 *     onDirtyChange, onSaved, onError,
 *   });
 *
 * Returns the same controller shape CasualDocsEditor.mount() itself
 * returns ({ forceSave(): Promise<void>, destroy() }), or null if the
 * bundle failed to load — the container is left showing an inline error
 * message in that case, same fallback every page already had individually.
 */
(function (global) {
    'use strict';

    // Loads the bundle's CSS+JS once per page, no matter how many times
    // mount() is called (some pages mount more than one viewer instance,
    // e.g. a list of documents each embedding a read-only preview).
    let assetsPromise = null;

    function loadAssets(cssUrl, jsUrl) {
        if (assetsPromise) return assetsPromise;
        const css = document.createElement('link');
        css.rel = 'stylesheet';
        css.href = cssUrl;
        document.head.appendChild(css);
        assetsPromise = new Promise((resolve, reject) => {
            const script = document.createElement('script');
            script.src = jsUrl;
            script.onload = resolve;
            script.onerror = () => reject(new Error('Failed to load the Casual Docs editor bundle'));
            document.body.appendChild(script);
        });
        return assetsPromise;
    }

    // ── Post-save similarity check — see documents/wopi.py::similarity_check
    // (shared with _run_similarity_check_on_docx). Writes native Word
    // comments onto matching paragraphs in the saved .docx; visible next
    // time the document is reopened, not instantly. No auto-dismiss on the
    // toast — a councilor mid-edit shouldn't have to catch it before it
    // disappears. ─────────────────────────────────────────────────────────
    function showSimilarityToast(count) {
        const existing = document.getElementById('similarityToast');
        if (existing) existing.remove();
        const toast = document.createElement('div');
        toast.id = 'similarityToast';
        toast.style.cssText = 'position:fixed;bottom:20px;right:20px;z-index:9999;background:#1e293b;color:#fff;padding:14px 16px;border-radius:12px;font-size:12px;font-weight:600;box-shadow:0 8px 24px rgba(0,0,0,0.25);max-width:340px;';
        toast.innerHTML = `
            <div style="display:flex;align-items:flex-start;gap:10px;">
                <span style="flex:1;line-height:1.5;">
                    ⚠ ${count} passage${count !== 1 ? 's' : ''} may resemble existing legislation.
                    <span style="display:block;font-weight:400;color:#cbd5e1;margin-top:4px;">
                        Reopen this document to review the flagged passages in the Comments panel.
                    </span>
                </span>
                <button style="background:none;border:none;color:#94a3b8;cursor:pointer;font-size:14px;flex-shrink:0;" onclick="this.closest('#similarityToast').remove()">✕</button>
            </div>`;
        document.body.appendChild(toast);
    }

    async function runSimilarityCheck(docId, csrfToken) {
        try {
            const resp = await fetch(`/wopi/similarity-check/${docId}/`, {
                method: 'POST',
                headers: { 'X-CSRFToken': csrfToken },
            });
            const data = await resp.json();
            if (data.new_flags > 0) showSimilarityToast(data.new_flags);
        } catch (err) {
            console.debug('similarity check failed:', err);
        }
    }

    // ── Suppress the library's own cross-document "recover unsaved
    // changes" banner. Casual Docs autosaves into ONE shared IndexedDB
    // slot (db "casual-docs", store "autosave", key "current") with no
    // scoping by document — the banner it renders is just as likely to
    // offer a stranger's content as the document actually being opened.
    // Our own server-side autosave (documents/wopi.py::casualdocs_save /
    // committee_level/views.py::committee_report_save, ~4s debounce)
    // already covers real crash recovery, so we suppress the banner's
    // only doorway into that unscoped data rather than trying to clear
    // the shared slot ourselves — there's no way from outside the
    // library to tell "this snapshot belongs to the document about to
    // open" apart from "it's a stranger's", so clearing it proactively
    // would just as easily destroy a legitimate same-document recovery.
    //
    // Hides rather than removes the node: it's rendered by the library's
    // own React tree (confirmed: no createPortal, it's a plain sibling
    // inside the mounted container), so ripping it out of the DOM
    // directly could desync React's reconciliation if that subtree ever
    // re-renders. display:none leaves the node in place for React's
    // bookkeeping while making it invisible and inert.
    //
    // No documented switch for this (checked DocxEditorProps' own type
    // defs — nothing autosave/recovery-related is exposed).
    // @casualoffice/docs is pinned at 1.4.2; re-verify this still finds
    // the banner (data-testid="autosave-banner") after any version bump.
    function suppressAutosaveBanner(containerEl) {
        const observer = new MutationObserver(() => {
            const banner = containerEl.querySelector('[data-testid="autosave-banner"]');
            if (banner) {
                banner.style.setProperty('display', 'none', 'important');
                observer.disconnect();
            }
        });
        observer.observe(containerEl, { childList: true, subtree: true });
        const giveUpTimer = setTimeout(() => observer.disconnect(), 5000);
        return function stopWatching() {
            clearTimeout(giveUpTimer);
            observer.disconnect();
        };
    }

    async function mount(containerEl, opts) {
        opts = opts || {};
        try {
            await loadAssets(opts.cssUrl, opts.jsUrl);
        } catch (err) {
            const message = opts.mode === 'viewing'
                ? 'Could not load the document viewer.'
                : 'Failed to load the document editor.';
            containerEl.innerHTML =
                `<div style="padding:40px;text-align:center;color:#dc2626;font-size:13px;font-weight:600;">${message}</div>`;
            return null;
        }

        const stopWatchingForBanner = suppressAutosaveBanner(containerEl);

        // Falling-edge trigger: fire the check once a save has actually
        // landed (dirty: true -> false), not on every keystroke. Only
        // wired when there's a saveUrl — 'viewing' pages have nothing to
        // check and no author actively drafting against, same reasoning
        // as the bundle's own live inline check (see
        // frontend/casualdocs-entry.jsx).
        const doSimilarityCheck = !!opts.saveUrl && opts.checkSimilarity !== false;
        let wasDirty = false;

        const controller = global.CasualDocsEditor.mount(containerEl, {
            fileUrl: opts.fileUrl,
            // A File/Blob/ArrayBuffer already in hand — e.g. a file just
            // picked in a local <input type="file">, previewed before it's
            // ever uploaded anywhere. Takes priority over fileUrl (no
            // fetch happens at all when this is set) — see
            // frontend/casualdocs-entry.jsx's CasualDocsApp.
            documentBuffer: opts.documentBuffer,
            saveUrl: opts.saveUrl,
            csrfToken: opts.csrfToken,
            authorName: opts.authorName,
            mode: opts.mode,
            pageSetup: opts.pageSetup,
            onDirtyChange: function (isDirty) {
                if (doSimilarityCheck && wasDirty && !isDirty) runSimilarityCheck(opts.docId, opts.csrfToken);
                wasDirty = isDirty;
                if (opts.onDirtyChange) opts.onDirtyChange(isDirty);
            },
            onSaved: opts.onSaved,
            onError: opts.onError || function (err) { console.error('Casual Docs error:', err); },
        });

        if (controller && typeof controller.destroy === 'function') {
            const originalDestroy = controller.destroy;
            controller.destroy = function (...args) {
                stopWatchingForBanner();
                return originalDestroy.apply(controller, args);
            };
        } else {
            stopWatchingForBanner();
        }

        return controller;
    }

    global.CasualDocsMount = { mount };
})(window);
