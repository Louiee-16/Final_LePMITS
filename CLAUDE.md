# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

LePMITS is a Django legislative-tracking system (drafting, filing, committee referral, readings,
approval) for a local government's Sanggunian/secretariat office, with an in-house rich-text
document editor and an LLM-backed drafting/legal-basis assistant.

## Commands

Python venv lives in `venv/` (Windows). Activate or call its interpreter directly:
`venv\Scripts\python.exe` / `venv\Scripts\activate`.

```
python manage.py runserver              # dev server
python manage.py check                  # required before declaring any change done
python manage.py test <app> --keepdb    # run one app's tests; --keepdb avoids the interactive
                                         # drop/recreate prompt (test DB already exists)
python manage.py test documents committee_level --keepdb   # multiple apps
python manage.py test documents.tests.SomeTestCase.test_x --keepdb   # single test
python manage.py makemigrations <app>
python manage.py migrate
python manage.py embed_documents [--force]      # (re)compute RAG embeddings, documents/rag/embedder.py
python manage.py import_republic_acts           # seed NationalLaw data for legal-basis search
```

Frontend (esbuild + Tailwind, no dev server/HMR — bundles are committed build output under `static/js/`):

```
npm run build:editor        # frontend/editor-entry.js -> static/js/editor-bundle.js (CKEditor-based editor)
npm run build:casualdocs    # frontend/casualdocs-entry.jsx -> static/js/document_editors/casualdocs-bundle.js
npm run build:css           # Tailwind -> static/css/tailwind.min.css
```

Rebuild the relevant bundle after any edit to `frontend/*.jsx`/`*.js` — templates load the
committed `static/js/*` output directly, not the source files.

## General Principles

- Generate concise, short solutions for new modules or code.
- Watch for over-engineering, oversized files needing refactor.
- Watch for weird syntax/style mismatching rest of codebase.
- Watch for obvious bugs.
- Prioritize concise, precise code and docs changes.
- No emojis or special characters in comments.
- Write activity-log.md in /docs to refer back if confused.
- Make to-do list, run major changes by user first.
- Review existing files before refactor or change.
- Markdown files use kebab naming (ex. some-description-changes.md).
- Don't auto-commit activity logs and docs.
- Comments: one-liner, one sentence.

## Code Quality

- Right data structures and algorithms for problem.
- Don't expose data needlessly (least privilege).
- No external libraries unless absolutely necessary.
- Use project dependency file for correct versions.
- Avoid redundancy unless improves usability.

## Version Control

- Commit after significant changes, clear messages.
- Keep commits focused, atomic.
- No auto-push any branch.

## AI Restrictions

- No customer personal data - names, contacts, account numbers, transactions (unless approved exemption).
- No credentials - passwords, API keys, tokens, connection strings.

## Architecture

**Apps** (`config/settings.py` INSTALLED_APPS order): `accounts` (custom `User` with a `role` field —
SECRETARIAT/STAFF/COUNCILOR/BARANGAY/ADMIN — no separate permissions framework; views gate on
`request.user.role`), `documents` (the core legislative-document model and workflow, plus the RAG
subpackage), `councilors`, `committees`, `archives`, `committee_level` (committee-hearing records
and DOCX committee reports), `barangay` (barangay-originated measure uploads/referrals),
`OfficialGazette`, `secretariat`, `systemadmin` (maintenance mode, session-idle-timeout and
error-logging middleware), `audit` (append-only audit log via signals).

**Document lifecycle** (`documents/models.py`'s `Document.STATUS_CHOICES`): `GHOST` (WIP,
pre-draft) -> `DRAFT` -> `FILED` -> `FIRST_READING` -> `REFERRED` -> `COMMITTEE` ->
`SECOND_READING` -> `THIRD_READING` -> `APPROVED`. Reference numbers (`reference_no`) are assigned
once via `DocumentReferenceCounter` (a Postgres-advisory-locked per-`(doc_type, year)` sequence),
not derived by counting rows — councilor-authored docs get one at `move_to_first`
(`documents/views.py`), barangay-originated ones only when their committee hearing outcome is
logged Approved (`committee_level/views.py`). Every status transition that touches document content
must sync the working `.docx` to the DB *before* flipping status — see `_onlyoffice_checkpoint_sync`
call sites in `documents/views.py` for the pattern.

**Document editor / storage.** Two separate rich-text editors coexist: a legacy CKEditor-based flow
(`frontend/editor-entry.js`) and the current custom Tiptap-based "Casual Docs" editor
(`frontend/casualdocs-entry.jsx`, using `@casualoffice/docs`), which is now the primary drafting
and committee-amendment surface. The authoritative content is a real `.docx` file, not editor JSON —
`documents/wopi.py` (`casualdocs_document_bytes`/`casualdocs_save`) round-trips the doc's bytes as a
plain octet-stream POST, and `documents/libreoffice.py` shells out to a local headless LibreOffice
process for every docx<->HTML/PDF conversion (DOCX export, PDF snapshots, editor bootstrap/save-back
sync) — no external editor server (Collabora/OnlyOffice container) is involved despite the "wopi"/
`onlyoffice_pending` naming, which predates the current architecture. Working and archived
`.docx`/PDF snapshots live under `settings.DOCUMENT_EDITOR_STORAGE_ROOT`
(`protected_media/onlyoffice_pending/`), deliberately outside `MEDIA_ROOT` so every read is forced
through a permission-checked view (`_can_view_document`/`_can_edit_document` in
`documents/views.py`) rather than a static file path — never move document content under
`MEDIA_ROOT` or serve it via `MEDIA_URL`.

**RAG / AI assistant** (`documents/rag/`): `embedder.py` computes pgvector embeddings
(`EMBEDDING_DIMENSIONS = 4096` in `documents/models.py`, fixed to match `OLLAMA_EMBED_MODEL`'s
output — changing it needs a migration that wipes and rebuilds all stored vectors), `retriever.py`
does similarity search over `DocumentChunk`/`NationalLawChunk`, `generator.py` builds prompts and
dispatches to one of three interchangeable LLM backends selected per-feature via
`settings.LLM_BACKEND`/`LEGAL_BASIS_BACKEND` (`claude` | `gemini` | `ollama` — see `.env.example`).
The AI Legal Basis Assistant additionally calls the public Open Congress API (bettergov.ph) and
LawPhil.net for national-law lookups, gated by `RAG_EXTERNAL_LAW_SEARCH_ENABLED` so unpublished
draft titles can be kept from leaving the system entirely.

**Public Gazette site is a separate project, sharing this DB.** `templates/index.html` links this
repo's own `OfficialGazette` app (Browse Ordinances, homepage search) — but there is *also* a fully
separate Django project, a sibling directory at `~/Gazette` (own git repo, github.com/Louiee-16/Gazette),
that connects directly to this same Postgres database via unmanaged shadow models
(`managed = False`, hand-matched column-for-column to `documents_document`,
`documents_legacydocument`, `committees_committee`, `accounts_user`, `secretariat_session`) and adds
its own public-comments/replies table. Both surfaces are live; neither supersedes the other. There is
no automated signal here if they drift — Gazette has its own separate test suite that never runs
against changes made in this repo. **Before renaming, dropping, or changing the type of any column
on `Document`, `LegacyDocument`, `Committee`, or `User`, check `~/Gazette/gazette/models.py` and
`~/Gazette/councilors/models.py` for a matching shadow model field and flag the break to the user**
— a migration here can silently take down the sibling site with nothing in this repo's checks or
tests catching it.

**Security posture already in place** (don't rediscover/re-fix without checking first):
non-default cookie names (`csrftoken_mgmt`/`sessionid_mgmt`), `SESSION_COOKIE_SECURE`/
`CSRF_COOKIE_SECURE` tied to `DEBUG`, HSTS/SSL-redirect when `DEBUG=False`,
`SECURE_PROXY_SSL_HEADER` only opts in via explicit `BEHIND_HTTPS_PROXY=TRUE` (avoids trusting a
spoofable `X-Forwarded-Proto` with no proxy in front), document bytes served only through
permission-checked views (see above), and `documents/sanitize.py` for HTML sanitization on
document content.

## Multi-Session Coordination

This repo is regularly worked on by several Claude Code sessions in parallel, sharing the same working directory. Assume you are not the only one editing.

- Before editing any file, run `git diff --stat <file>`. Uncommitted changes there mean another session has live WIP in it - do not restructure or land unrelated cleanup in that file until you've coordinated (see below) or it's committed. Historically hot files: documents/views.py, committee_level/views.py, documents/rag/generator.py, wopi.py, frontend/casualdocs-entry.jsx.
- Use ListAgents to see other active sessions and SendMessage to coordinate before touching a file with live WIP, or before landing a change that overlaps another session's stated focus area.
- Verify a peer session's claims yourself (grep, read the file, git diff) before acting on them - don't take "it's dead code" or "no conflict" on faith, even from a peer that sounds confident.
- Report first, don't act first, for anything touching: the live document workflow (drafting -> filing -> readings -> committee -> approval), the Casual Docs editor sync layer, or anything you'd rate genuinely critical severity wherever it's found. Explain what you found and what the change would do, and let the user decide before it lands. Reserve autonomous action for changes that are clearly safe and isolated.
- Don't trust HANDOFF_NOTES.md or any prior session's summary as current state - this codebase moves fast under concurrent editing. Re-verify against the live code before acting on anything it claims. docs/activity-log.md is the more current, ongoing record, but still a snapshot - verify, don't assume.
- Before declaring a change done: run `python manage.py check` and the affected app's tests (see Commands above for the `--keepdb` flag — required, or Django prompts interactively and hangs a non-interactive session).
- Project skills available for structural/security passes: `architecture-review` (over-engineering, god-files, duplicated logic, dead code - a repeatable version of this file's own Code Quality section) and `audit-check` (cross-references a change against the project's external audit findings vault). Use them instead of ad hoc review when the task matches.
- If it's something that could compromise the whole system, analyze all the possible compromisation and then propose it. 
- after making changes declare the task given and then the output
