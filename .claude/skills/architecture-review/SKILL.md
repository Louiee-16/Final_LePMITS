---
name: architecture-review
description: Runs a structural health pass over this codebase — oversized files, overengineering, duplicated logic, missing abstractions, and general messiness — separate from bug-hunting or security auditing. Use this whenever the user asks to review architecture, clean up the codebase, look for overengineering, check for messy or bloated code, find duplicated/copy-pasted logic across views or templates, or wants a general "how healthy is this code" pass rather than a specific bug fix. Also trigger when the user says things like "does this need refactoring", "is this file too big", "check for tech debt", or asks for a to-do list of cleanup work. Not for security/access-control findings (use audit-check for known audit findings, or code-review for correctness bugs) — this skill is about structure and clarity, not vulnerabilities.
---

# Architecture Review

## What this is for

This project's own `CLAUDE.md` already asks for exactly this kind of attention on every change ("watch for over-engineering, oversized files needing refactor... weird syntax/style mismatching the rest of the codebase... obvious bugs"). This skill exists to turn that from a passive reminder into an actual repeatable pass — something invoked deliberately for a dedicated cleanup sweep, not just kept in mind opportunistically while doing something else.

## Ground rule: verify live, don't trust existing docs

This codebase changes fast — multiple sessions work on it in parallel, sometimes hours apart. Any existing notes (`docs/activity-log.md`, `HANDOFF_NOTES.md` if present, prior conversation summaries) may already be stale by the time you read them. Use them as a *lead* for where to look, never as a substitute for reading the actual current file. If a note claims something is "still broken" or "not yet done," check the live code before repeating that claim — it may already be fixed by a parallel session.

## What to actually check

Work through these, but don't treat it as a rigid checklist to complete mechanically — use judgment about what's actually worth flagging in *this* codebase, a live Django app for legislative document tracking, not a toy project. A finding only earns a place in the report if fixing it would genuinely make the code easier to work in, not just different.

**1. Oversized / god-files.** Find files that have grown past doing one coherent job — a rough signal is line count (`wc -l` across `.py` files is a fast first pass), but the real test is: does this file mix unrelated concerns (e.g. one file handling both PDF export *and* AI features *and* legacy OCR import)? Note what natural seams already exist for splitting it, not just that it's long.

**2. Duplicated logic.** Especially likely across templates that each reimplement the same JS/wiring pattern (mount logic, save handlers, modal behavior), or views that each hand-roll the same permission check slightly differently. Search for the same 10+ line pattern appearing more than twice — that's a strong signal it should be a shared helper/partial, not three independent copies that will inevitably drift.

**3. Missing centralization for things that are conceptually one policy.** The clearest example in a system like this: status-transition/role checks scattered per-view (`if request.user.role not in [...]`) instead of one place that defines the actual state machine. When each view reimplements "who can do this transition," they drift out of sync with each other over time, and nothing makes the real policy legible in one place. Flag this pattern wherever you see it, even if each individual instance looks locally correct.

**4. Overengineering — the opposite failure.** Abstractions, config options, or generality that nothing actually uses. Dead code (functions defined but never called — grep for the function name across the repo to confirm), unused settings/env vars, dependencies installed but never imported, speculative flexibility (a backend-selection system with 3 options when only 1 is ever configured) that adds cognitive load without paying for itself. This project's own audit history has real examples of this pattern (a fully-built retrieval path superseded by a better data source but never removed) — worth checking if anything similar has accumulated since.

**5. Inconsistency / weird syntax.** Code that doesn't match the surrounding style — different error-handling conventions in neighboring functions, imports scattered mid-file instead of at the top, a naming convention used everywhere except one spot. These are low-severity individually but compound into a codebase that's harder to read confidently, because nothing looks reliably "normal."

**6. Obvious bugs spotted along the way.** Not the primary target (that's `code-review`'s job), but if something looks wrong while reading for structure, don't ignore it just because it's off-topic — note it.

## How to report back

Match this project's existing convention — a **prioritized to-do list**, not a wall of prose, per `CLAUDE.md`'s "make a to-do list, run major changes by user first." Structure:

```
## [Area/file] — [one-line problem statement]
**Why it matters:** [concrete cost — drift risk, onboarding difficulty, bug surface — not just "it's long"]
**Suggested fix:** [the natural next step, not a fully worked-out design]
**Risk if touched:** [low/medium/high — does this sit under live legislative workflow, does it have test coverage already]
```

Order by actual leverage (how much confusion/risk it removes relative to how risky the change itself is), not just by severity or file size. A large, well-isolated, well-tested cleanup can rank above a small, terrifying one sitting under live status transitions.

**Don't fix things automatically as part of this pass** — this is a survey, and per `CLAUDE.md`, major changes get run by the user first. If something is a genuinely trivial, safe, isolated fix (e.g. moving a stray mid-file import to the top), it's fine to just do it and note it. Anything touching shared logic, permission checks, or multiple call sites goes on the list for the user to prioritize, not straight into a diff.

If a cleanup does get carried out as a result of this review, log it in `docs/activity-log.md` following the file's existing format (dated section, concise engineering-log tone, what/why/how-verified) — matching how other work in this project is already tracked.
