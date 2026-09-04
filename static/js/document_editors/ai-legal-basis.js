/*
 * Shared "AI Legal Basis" button + modal wiring — was originally only in
 * templates/documents/draft.html, meaning reopening an existing draft via
 * templates/councilors/view_draft.html lost access to the feature even
 * though it's still the same drafting phase, just resuming instead of
 * starting fresh. Extracted here so both pages (and any future one) share
 * one implementation instead of duplicating ~100 lines of modal-rendering
 * logic per page, same reasoning as casualdocs-mount.js.
 *
 * Requires the including page to already have:
 *   - a button with id="aiCheckBtn"
 *   - the modal markup from templates/documents/_ai_legal_basis_modal.html
 *     (include it directly — {% include 'documents/_ai_legal_basis_modal.html' %})
 *
 * Usage:
 *   <script src="{% static 'js/document_editors/ai-legal-basis.js' %}"></script>
 *   <script>
 *     AiLegalBasis.init({
 *       titleInput: titleInput,           // the page's own title <input>
 *       getDocId: () => docIdField.value, // however the page tracks its doc id
 *       csrfToken: csrfToken,
 *       urls: {
 *         autosave: "{% url 'autosave_draft' %}",
 *         legalBasis: "{% url 'ai_legal_basis' %}",
 *       },
 *       getActiveEditor: () => activeEditor, // for forceSave() before the check
 *     });
 *   </script>
 */
(function (global) {
    'use strict';

    function closeModal() {
        const modal = document.getElementById('aiModal');
        if (modal) modal.classList.replace('flex', 'hidden');
    }

    function escapeHtml(s) {
        return String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
    }

    function isHttpUrl(u) {
        return typeof u === 'string' && /^https?:\/\//i.test(u);
    }

    function renderCitations(citations) {
        const lawsEl = document.getElementById('nationalLaws');
        if (!citations.length) {
            lawsEl.innerHTML = '<p class="text-xs text-slate-400 italic">No related national legislation found for this measure.</p>';
            return;
        }
        const cardStyle = 'display:block;padding:14px 18px;background:white;border-left:3px solid #6366f1;border-radius:0 8px 8px 0;margin:0 0 10px;';
        const badge = (c) => c.status === 'enacted'
            ? '<span style="display:inline-block;padding:1px 6px;font-size:9px;font-weight:700;letter-spacing:.03em;color:#166534;background:#dcfce7;border-radius:3px;">ENACTED LAW</span>'
            : '<span style="display:inline-block;padding:1px 6px;font-size:9px;font-weight:700;letter-spacing:.03em;color:#92400e;background:#fef3c7;border-radius:3px;">PENDING BILL &middot; NOT YET LAW</span>';
        lawsEl.innerHTML = citations.map((c) => {
            const body = `
                <div style="margin:0 0 3px;">${badge(c)}</div>
                <p style="margin:0;font-weight:700;color:#1e293b;">${escapeHtml(c.law_title) || 'Unknown'}</p>
                ${c.reason ? `<p style="margin:2px 0 0;color:#64748b;line-height:1.5;">${escapeHtml(c.reason)}</p>` : ''}
            `;
            if (isHttpUrl(c.url)) {
                return `<a href="${escapeHtml(c.url)}" target="_blank" rel="noopener noreferrer"
                            style="${cardStyle}text-decoration:none;cursor:pointer;">
                            ${body}
                            <span style="display:inline-block;margin-top:4px;font-size:10px;font-weight:700;color:#6366f1;">OPEN SOURCE DOCUMENT &rarr;</span>
                        </a>`;
            }
            return `<div style="${cardStyle}">${body}</div>`;
        }).join('');
    }

    function init(opts) {
        opts = opts || {};
        const btn = document.getElementById('aiCheckBtn');
        if (!btn) return;

        btn.addEventListener('click', async function () {
            const title = opts.titleInput.value.trim();
            // "Untitled Draft" is the ghost's default placeholder, not a
            // real title — running the legal-basis search against it
            // produces a meaningless keyword extraction and a
            // near-random candidate pool (confirmed live: it returned
            // plastic-waste laws for a diaper-assistance ordinance,
            // because "Untitled Draft" carries no real subject to search
            // on). Treat it the same as blank.
            if (!title || title.toLowerCase() === 'untitled draft') {
                alert("Please fill in the Title first — the document's own in-body heading isn't the same field as this one.");
                return;
            }

            btn.disabled = true;
            document.getElementById('aiModal').classList.replace('hidden', 'flex');
            document.getElementById('aiResults').classList.add('hidden');
            document.getElementById('aiLoading').classList.remove('hidden');

            try {
                const docId = opts.getDocId();
                const activeEditor = opts.getActiveEditor ? opts.getActiveEditor() : null;
                // ai_legal_basis reads title/content straight from the DB
                // record, not from this request — so without syncing
                // first it can run against a stale title and stale
                // content if nothing's been saved yet.
                await Promise.all([
                    (activeEditor && activeEditor.forceSave) ? activeEditor.forceSave() : Promise.resolve(),
                    fetch(opts.urls.autosave, {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json', 'X-CSRFToken': opts.csrfToken },
                        body: JSON.stringify({ doc_id: docId, title: title }),
                    }).catch((err) => console.debug('title sync before legal-basis check failed:', err)),
                ]);

                const res = await fetch(opts.urls.legalBasis, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json', 'X-CSRFToken': opts.csrfToken },
                    body: JSON.stringify({ pk: docId }),
                });
                const data = await res.json();

                document.getElementById('aiLoading').classList.add('hidden');
                document.getElementById('aiResults').classList.remove('hidden');

                if (!res.ok || data.error) {
                    document.getElementById('nationalLaws').innerHTML =
                        `<p class="text-xs text-red-500 font-bold">${data.error || 'Unknown error.'}</p>`;
                    return;
                }

                renderCitations(Array.isArray(data.national_laws) ? data.national_laws : []);
            } catch (err) {
                document.getElementById('aiLoading').classList.add('hidden');
                document.getElementById('aiResults').classList.remove('hidden');
                document.getElementById('nationalLaws').innerHTML = '<p class="text-xs text-red-500 font-bold">Could not reach AI assistant. Try again.</p>';
                console.error('ai_legal_basis error:', err);
            } finally {
                btn.disabled = false;
            }
        });
    }

    global.AiLegalBasis = { init, close: closeModal };
})(window);
