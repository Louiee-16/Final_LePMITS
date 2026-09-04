---
name: audit-check
description: Cross-checks a code change in this LePMITS project against the known findings recorded in the project's Obsidian knowledge vault (audit findings, RAG pipeline notes, AI feature critiques). Use this whenever the user asks to review, verify, or sanity-check a fix touching access control, authentication/role checks, the document approval workflow, CSRF/HTTP-method handling, the AI Legal Basis Assistant, the AI Inline Check feature, or the RAG/embedding pipeline (documents/, signals.py, views.py, documents/rag/). Also trigger whenever the user asks things like "is this fixed", "did that close the bug", "does this still have the XSS issue", mentions a specific audit finding by name or severity, or has just edited security-sensitive code in this project and wants confirmation before considering it done. Don't wait for the user to say "audit" or "vault" explicitly — infer this from the area of code being discussed.
---

# Audit Check

## Why this exists

This project went through a full code audit (2026-08-19, 58 findings) and has an ongoing set of product/design decisions recorded in an Obsidian vault external to this repo. Both are easy to lose track of during day-to-day editing — a fix can look complete (code runs, tests pass) while still leaving the actual described vulnerability or design flaw in place, or while quietly breaking something else the vault already documented. This skill's job is to close that gap: connect whatever code is being touched right now back to what's already known about it, verify the fix is real (not surface-level), and check it hasn't reopened or contradicted something else on record.

## Where the knowledge lives

The vault is **outside this repo**, at:
```
C:\Users\ASUS\Documents\LePMITS\LePMITS\LePMITS Instructions\
```
It is not embedded or synced into this project — read the files directly from that path each time. If access is denied, ask the user to approve read access to that folder (see this project's `CLAUDE.md` for the intended always-loaded entry point).

| Vault note | Read it when the change touches... |
|---|---|
| `00 - Start Here (System Prompt).md` | Anything — orients on the whole system if you're unsure where to start. |
| `01 - System Architecture.md` | Any app/model, to understand what a piece of code is supposed to do in context. |
| `02 - AI Legal Basis Assistant.md` | `ai_legal_basis`, `generate_legal_basis()`, `_build_prompt()`, Open Congress API calls. |
| `03 - AI Inline Check.md` | `ai_inline_check`, `draft.html`'s Quill/RAG-highlight JS, anything in the paragraph-similarity flow. |
| `04 - RAG Pipeline and Data State.md` | `documents/rag/embedder.py`, `retriever.py`, `generator.py`, `DocumentChunk`, `NationalLawChunk`, model/backend config. |
| `05 - Code Audit Findings (2026-08-19).md` | The primary reference — full findings list by section and severity. Read this for almost every check. |
| `06 - Product Decisions and Roadmap.md` | Anything about credibility strategy, model choice, or whether a feature/direction was already discussed and decided. |

Treat the vault as a point-in-time snapshot, not ground truth about the *current* state of the code — it says so itself in note 00. Its value is architectural context and the original problem description; whether a problem is *actually* fixed now has to be verified against the live source, not assumed from the vault.

## What to actually do

### 1. Identify the relevant finding(s)
Look at what changed (a diff, a file the user is discussing, or a description of what they just did) and search `05 - Code Audit Findings (2026-08-19).md` for findings whose file path or description overlaps. A single change often maps to more than one finding — e.g. a fix in `documents/views.py` around line 397 touches both the CSRF-adjacent precedence bug *and* the access-control finding that references the same line. Don't stop at the first match.

If nothing in the vault matches, say so plainly — not every change is audit-related, and forcing a connection where there isn't one wastes the user's time.

### 2. Verify the fix actually closes the gap — don't just check it runs

This is the part that's easy to shortcut and matters most. "The code changed" and "the vulnerability is gone" are different claims. For each finding pattern, the thing to actually verify is the underlying condition, not the presence of code that looks related:

- **Missing auth/role check** (e.g. `user_management`, `ai_inline_check`, the five domain-app views with no `@login_required`) — confirm the specific decorator is present *and* correctly ordered relative to other decorators, and that it checks the right role, not just "a login is required." A `@login_required` alone doesn't close a finding that specifically required a role check (`@user_passes_test(is_admin)` or equivalent).
- **Operator precedence bugs** (`... and X or Y` letting the wrong role through on GET) — don't eyeball it, mentally evaluate the boolean expression against Python's actual `and`/`or` binding, or better, check whether the fix added explicit parentheses or restructured the condition into separate checks. If it's still one unparenthesized `and`/`or` chain, be suspicious even if the specific values changed.
- **HTTP-method / CSRF gaps** (state changes reachable via GET) — confirm the view now actually checks `request.method == "POST"` (or uses `@require_POST`) as a *hard gate* before any mutation, not just as one clause in a broader boolean that something else can short-circuit.
- **Stored XSS / `|safe` usage** — confirm content is sanitized *before* it's saved (not just at render time), and check whether the fix covers *all* the templates the audit listed (`modal_document_viewer.html`, `document_pdf.html` ×2, `amending_table.html`, `view_draft.html`, `document_view.html` ×2, `floor_amendments.html`) — a fix that sanitizes on save but leaves even one of those templates rendering old, already-stored unsanitized content with `|safe` is incomplete. Check for the migration that was supposed to backfill-sanitize existing rows, if the vault or repo mentions one.
- **Approval-workflow bypass** (`approve_measure` skipping readings) — confirm the fix checks the document actually reached the required prior status (e.g. `THIRD_READING`), not just that it isn't already `APPROVED`.
- **`get_or_create` duplication bug** (Session finalization) — confirm the fix moved the always-fresh value (like `timezone.now()`) out of the lookup kwargs and into `defaults`, not just renamed a variable.
- **False privacy claims** (RAG docstring vs. actual behavior) — confirm the docstring and the actual data sent externally now agree with each other; either the code changed to match the claim, or the claim changed to match the code. Flag it if only one side moved.

When a finding doesn't fit one of these shapes, reason about it the same way: restate what the finding says can go wrong, then check whether that specific bad path is actually now blocked — not whether the code "looks fixed."

### 3. Check for regressions against other things the vault documents

Some fixes are locally correct but conflict with something else on record:
- A change to `retriever.py`'s boilerplate exclusion (`_BOILERPLATE_CHUNK_TYPES`) should be checked against the confirmed false-positive case in `03 - AI Inline Check.md` (enacting clause / title section false-flagged) — does the fix actually cover that case, or just the ones explicitly named in the audit?
- A change to the RAG/embedding pipeline should be checked against the actual current data state noted in `04 - RAG Pipeline and Data State.md` (as of the vault's writing: 0 approved documents, 7 chunks all from one legacy document) — a fix that assumes a populated corpus may behave differently than expected until real data exists; note this rather than assuming the fix is fully validated.
- A change near `ai_legal_basis` or `ai_inline_check` should be checked against the design critique in their respective notes — e.g. does a "fix" to `ai_inline_check` address the underlying design problems (unauthenticated, no explanation of matches, resource contention) noted there, or only the specific bug reported?

### 4. Report back

Keep it concise — a table works well:

| Finding | Status | Evidence |
|---|---|---|
| Short finding name, cite the vault note | Resolved / Still Open / Partially Fixed / Not Applicable | One line: what you actually checked in the code that supports this |

After the table, call out anything from step 3 (regressions or reintroduced patterns) separately, since those are easy to miss in a table skim. If a finding is "Partially Fixed," say exactly what's left — don't leave that vague.

If you're confident findings are genuinely resolved, offer (don't just do it) to update their status in `05 - Code Audit Findings (2026-08-19).md` so the vault stays a living reference instead of a stale snapshot — but only with the user's go-ahead, since that file is meant to preserve the original point-in-time audit record too.
