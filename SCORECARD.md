# Scorecard

> Scored before and after the 0.1.0 full treatment. SHIP_GATE.md holds the line-by-line results.

**Repo:** colab-runner
**Date:** 2026-11-17
**Type tags:** `[all]` `[cli]` `[pypi]` (installed from the tag, not published to PyPI)

## Pre-Remediation Assessment

| Category | Score | Notes |
|----------|-------|-------|
| A. Security | 7/10 | Token scrub, binary rule and quarantine built (PR #3, #4); no SECURITY.md, no threat-model section |
| B. Error Handling | 10/10 | Structured errors, org exit codes, `--debug` tracebacks (PR #4) |
| C. Operator Docs | 6/10 | A detailed lane section, but no install, quick start, platforms or handbook |
| D. Shipping Hygiene | 8/10 | verify.sh, pip-audit, 90% coverage floor; no lockfile, no site audit |
| E. Identity (soft) | 1/10 | No logo, landing page, translations or repo metadata |
| **Overall** | **32/50** | |

## Key Gaps

1. No SECURITY.md and no threat model in the README.
2. No install or quick start, and no supported platforms or Python versions stated.
3. No landing page or handbook.
4. No lockfile.

## Remediation Priority

| Priority | Item | Estimated effort |
|----------|------|-----------------|
| 1 | SECURITY.md and the README threat model | small |
| 2 | README install, quick start, platforms | small |
| 3 | Landing page and handbook, with the site's audit in CI | medium |

## Post-Remediation

| Category | Before | After |
|----------|--------|-------|
| A. Security | 7/10 | 10/10 |
| B. Error Handling | 10/10 | 10/10 |
| C. Operator Docs | 6/10 | 10/10 |
| D. Shipping Hygiene | 8/10 | 10/10 |
| E. Identity (soft) | 1/10 | 9/10 (repo metadata goes on after the merge, once the landing page is live) |
| **Overall** | 32/50 | 49/50 |

D carries one dated exception: picomatch GHSA-fc5q-39xc-vjpf in the site's build toolchain (Director, 2026-11-17, expires
2026-12-17). See SHIP_GATE.md.
