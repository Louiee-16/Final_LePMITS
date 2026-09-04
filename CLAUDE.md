# CLAUDE.md

Guidance for Claude Code working with code in this repo.

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

## Multi-Session Coordination

This repo is regularly worked on by several Claude Code sessions in parallel, sharing the same working directory. Assume you are not the only one editing.

- Before editing any file, run `git diff --stat <file>`. Uncommitted changes there mean another session has live WIP in it - do not restructure or land unrelated cleanup in that file until you've coordinated (see below) or it's committed. Historically hot files: documents/views.py, committee_level/views.py, documents/rag/generator.py, wopi.py, frontend/casualdocs-entry.jsx.
- Use ListAgents to see other active sessions and SendMessage to coordinate before touching a file with live WIP, or before landing a change that overlaps another session's stated focus area.
- Verify a peer session's claims yourself (grep, read the file, git diff) before acting on them - don't take "it's dead code" or "no conflict" on faith, even from a peer that sounds confident.
- Report first, don't act first, for anything touching: the live document workflow (drafting -> filing -> readings -> committee -> approval), the Casual Docs editor sync layer, or anything you'd rate genuinely critical severity wherever it's found. Explain what you found and what the change would do, and let the user decide before it lands. Reserve autonomous action for changes that are clearly safe and isolated.
- Don't trust HANDOFF_NOTES.md or any prior session's summary as current state - this codebase moves fast under concurrent editing. Re-verify against the live code before acting on anything it claims. docs/activity-log.md is the more current, ongoing record, but still a snapshot - verify, don't assume.
- Before declaring a change done: `python manage.py check`, and run the affected app's tests with `--keepdb` (the test DB already exists; without it Django prompts interactively to drop/recreate, which hangs a non-interactive session).
- Project skills available for structural/security passes: `architecture-review` (over-engineering, god-files, duplicated logic, dead code - a repeatable version of this file's own Code Quality section) and `audit-check` (cross-references a change against the project's external audit findings vault). Use them instead of ad hoc review when the task matches.
- If it's something that could compromise the whole system, analyze all the possible compromisation and then propose it. 
- after making changes declare the task given and then the output
