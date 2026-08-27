# AGENTS.md

Engineering rules for coding agents (Codex, ZCode, etc.) working on this public repository.

## Repository Authority

- The Git repository, current working tree, tests, and docs are the source of engineering truth.
- Do not infer project state from chat history. Before starting work, check `git status`, `git branch --show-current`, `git rev-parse HEAD`, pending diffs, and recent history.
- A capability counts as released only if the current code, tests, and README demonstrate it. Never treat plans or prior discussion as evidence of shipped behavior.

## Agent Handoff

- If the working tree is not clean when you start, inspect the existing changes first (`git status`, `git diff`, `git diff --cached`) and preserve them.
- Do not assume you may reset, stash, or discard work left by another agent or session.

## Product Boundaries

- SelfEcho AI Community Edition is a self-hostable public project. selfechoai.com is an independently operated Hosted Service.
- This repository contains none of the Hosted Service's production database, real credentials, real user data, private server configuration, private deployment runbooks, or private Git history.
- Do not describe roadmap or planned features as implemented; state them as future direction until code and tests prove otherwise.

## Change Discipline

- Stay within the scope of your task; do not refactor unrelated code along the way.
- When shared product facts, commands, configuration, or links change, keep `README.md` and `README.zh-CN.md` factually consistent with each other. Outside such changes, mechanical translation syncing is not required.
- `tests/test_frontend_auth_contract.py` contains contract assertions against README and `.env.example`; run it when you change matching content.
- Add new files only when real maintenance needs call for them.

## Git Safety

The following are forbidden by default unless the current task explicitly authorizes them: force push (including `--force-with-lease`), `reset --hard`, rewriting or rebasing shared history, any `git commit --amend`, creating or deleting tags, GitHub Release creation or modification, and destructive branch cleanup.

Whether ordinary commit and push are performed is decided by each task's instructions.

## Secrets and Privacy

- By default, do not open or inspect `.env`, databases, logs, backups, credentials, or other secret-bearing files — including local ones.
- Only when the current task explicitly requires a secrets/privacy audit, a migration inspection, or similar access may you inspect secret-bearing files, and only to the minimum extent necessary; even then, never print, copy into examples, or commit secret values.
- Fixtures and examples use obviously synthetic values only; see [CONTRIBUTING.md](CONTRIBUTING.md) for test-data rules.
- Public documentation must not contain the Hosted Service's real credentials, real user data, private server configuration, private runbooks, or operational secrets. General self-hosting and deployment documentation is welcome.
- Handle vulnerability topics per [SECURITY.md](SECURITY.md); do not disclose exploit details in public issues.

## Testing and Completion

- Run the tests relevant to your change (full suite: `python -m pytest`). Documentation-only changes do not require running the whole suite.
- Report only verification you actually executed. Completion reports distinguish verified facts from assumptions; never fabricate test results.

## References

Current capabilities live in [README](README.md); architecture in [Architecture](docs/ARCHITECTURE.md); stable product boundaries in [Product Principles](docs/PRODUCT_PRINCIPLES.md).
