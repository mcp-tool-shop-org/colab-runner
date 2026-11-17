# Ship Gate

> No repo is "done" until every applicable line is checked.
> Copy this into your repo root. Check items off per-release.

**Tags:** `[all]` every repo · `[npm]` `[pypi]` `[vsix]` `[desktop]` `[container]` published artifacts · `[mcp]` MCP servers · `[cli]` CLI tools

---

## A. Security Baseline

- [x] `[all]` SECURITY.md exists (report email, supported versions, response timeline) — executed by `npx @mcptoolshop/shipcheck security-docs` (A1: present + reporting contact, not an empty stub) (2026-11-17) — `shipcheck security-docs` passed
- [x] `[all]` README includes threat model paragraph (data touched, data NOT touched, permissions required) — executed by `npx @mcptoolshop/shipcheck security-docs` (A2: trust/threat-model section present + non-empty; *quality* is not machine-checkable) (2026-11-17) — README "Security and threat model"; `shipcheck security-docs` passed
- [x] `[all]` No secrets, tokens, or credentials in source or diagnostics output — executed by `npx @mcptoolshop/shipcheck secrets` (scans every publishable tarball; matches redacted; not a manual attestation) (2026-11-17) — `shipcheck secrets` finds no npm tarball to scan, so the whole tree went through the org identity/secret scan on the PR head; every CLI and error line is scrubbed (README, Secrets)
- [x] `[all]` No telemetry by default — state it explicitly even if obvious (2026-11-17) — none collected or sent (README, SECURITY.md)

### Default safety posture

- [ ] `[cli|mcp|desktop]` SKIP: the only kill and stop act on the run's own job and session, which is the tool's purpose; another run's job is never signalled (`CR_NOT_OURS`) and the runner never deletes a quarantine
- [x] `[cli|mcp|desktop]` File operations constrained to known directories (2026-11-17) — writes only `--out-dir`, its sibling `<out-dir>.quarantine/` and a temp `colab-runner-quarantine-*`; quarantine moves are checked to stay inside (`QUARANTINE_ESCAPE`)
- [ ] `[mcp]` SKIP: not an MCP server
- [ ] `[mcp]` SKIP: not an MCP server

## B. Error Handling

- [x] `[all]` Errors follow the Structured Error Shape: `code`, `message`, `hint`, `cause?`, `retryable?` (2026-11-17) — `colab_runner/errors.py` CODES: code, message, hint, retryable
- [x] `[cli]` Exit codes: 0 ok · 1 user error · 2 runtime error · 3 partial success (2026-11-17) — README exit-code table
- [x] `[cli]` No raw stack traces without `--debug` (2026-11-17) — tracebacks only with `--debug`, scrubbed
- [ ] `[mcp]` SKIP: not an MCP server
- [ ] `[mcp]` SKIP: not an MCP server
- [ ] `[desktop]` SKIP: not a desktop app
- [ ] `[vscode]` SKIP: not a VS Code extension

## C. Operator Docs

- [x] `[all]` README is current: what it does, install, usage, supported platforms + runtime versions (2026-11-17) — install, quick start, Python 3.11+, Linux CI and Windows use
- [x] `[all]` CHANGELOG.md (Keep a Changelog format) (2026-11-17)
- [x] `[all]` LICENSE file present and repo states support status (2026-11-17) — MIT; support status in SECURITY.md
- [x] `[cli]` `--help` output accurate for all commands and flags (2026-11-17) — the handbook Reference page is generated from it
- [x] `[cli|mcp|desktop]` Logging levels defined: silent / normal / verbose / debug — secrets redacted at all levels (2026-11-17) — `--quiet` / normal / `--verbose` / `--debug`, scrubbed at every level
- [ ] `[mcp]` SKIP: not an MCP server
- [x] `[complex]` HANDBOOK.md: daily ops, warn/critical response, recovery procedures (2026-11-17) — the Starlight handbook (Getting started: reading the end and recovery with `stop --out-dir`; Using it from a lane; Reference; Security)

## D. Shipping Hygiene

- [x] `[all]` `verify` script exists (test + build + smoke in one command) (2026-11-17) — `bash verify.sh` (lint, tests, smoke)
- [x] `[all]` Version in manifest matches git tag — executed by `npx @mcptoolshop/shipcheck manifest` (D2: manifest version not behind the newest released tag; `--expect <ver>` for a strict release-time match) (2026-11-17) — pyproject 0.1.0; the v0.1.0 tag goes on this PR's merge
- [x] `[all]` Dependency scanning runs in CI (ecosystem-appropriate) — executed by `npx @mcptoolshop/shipcheck ci` (D3: a recognized scanner is *configured* in CI, or dependabot is present) (2026-11-17) — pip-audit, and the site's npm audit through the allowlist gate; `shipcheck ci` passed
- [x] `[all]` No known high/critical vulnerabilities in any dependency tree, and Dependabot alerts are enabled — executed by `npx @mcptoolshop/shipcheck deps` (the OUTCOME: audits **every** tree incl. subtrees, not just the root; `ci` only proves a scanner is configured) (2026-11-17) — the Python package has no dependencies; the site tree carries picomatch GHSA-fc5q-39xc-vjpf (every version affected, no fix yet) under the Director's dated exception of 2026-11-17, in `.github/audit-allowlist.json`, expiring 2026-12-17 and removed in the PR that bumps picomatch; any other advisory fails CI
- [ ] `[all]` SKIP: the org rule allows no Dependabot unless requested; advisories are caught by the CI audits
- [ ] `[npm]` SKIP: not an npm package
- [ ] `[npm]` SKIP: not an npm package
- [x] `[npm]` `engines.node` set · `[pypi]` `python_requires` set — executed by `npx @mcptoolshop/shipcheck manifest` (D6, checked per publishable package) (2026-11-17) — `requires-python = ">=3.11"`; `shipcheck manifest` engines passed
- [x] `[npm]` Lockfile committed · `[pypi]` Clean wheel + sdist build — lockfile executed by `npx @mcptoolshop/shipcheck manifest` (D7); the pypi wheel/sdist build is not yet executed (2026-11-17) — `uv.lock` (dev tools; no runtime dependencies); wheel and sdist built clean with `uv build`
- [ ] `[vsix]` SKIP: not a VS Code extension
- [ ] `[desktop]` SKIP: not a desktop app

## E. Identity (soft gate — does not block ship)

- [x] `[all]` Logo in README header (2026-11-17) — brand `logos/colab-runner/readme.png`
- [ ] `[all]` Translations (polyglot-mcp, 8 languages)
- [x] `[org]` Landing page (@mcptoolshop/site-theme) (2026-11-17) — https://mcp-tool-shop-org.github.io/colab-runner/
- [ ] `[all]` GitHub repo metadata: description, homepage, topics

---

## Gate Rules

**Hard gate (A–D):** Must pass before any version is tagged or published.
If a section doesn't apply, mark `SKIP:` with justification — don't leave it unchecked.

**Soft gate (E):** Should be done. Product ships without it, but isn't "whole."

**Executed vs attested.** `shipcheck audit` only *counts these checkboxes* — it does not read your repo, so a box can be green while the fact is false. The lines that say **"executed by `npx @mcptoolshop/shipcheck <gate>`"** are backed by a command that reads the real artifact and exits 1 on the real defect. Run those gates (they are wired into shipcheck's own `verify`); don't just tick their boxes. Executed today: **A1/A2** (`security-docs`), **A3** (`secrets`), **D2/D6/D7** (`manifest`), **D3-config + OIDC/provenance** (`ci`), **real vulnerabilities + alerting** (`deps`), **D5** (`pack`), plus front-door (`front-door`) and dogfood freshness (`dogfood`). Every other line is still an attestation you are vouching for. Note the two dependency layers: `ci` proves a scanner is *configured*; `deps` proves there are *no known vulnerabilities* — a repo can pass the first while failing the second.

**Checking off:**
```
- [x] `[all]` SECURITY.md exists (2026-02-27)
```

**Skipping:**
```
- [ ] `[pypi]` SKIP: not a Python project
```
