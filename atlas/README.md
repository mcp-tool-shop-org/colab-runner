# colab-runner: how it works

Mapped at 2026-11-19 from commit a9abaff by Atlas 1.26.0.

## What this is

4 parts, mostly Python (32 files), JavaScript (3), CSS (2), TypeScript (2), shell (2) and Astro (1). Work enters through 3 doors; the busiest is CI, which reaches 3 parts. It deploys a site to GitHub Pages.

## What changed since 2026-11-19 (b1a522a)

Nothing structural changed since 2026-11-19; 15 files changed content.

## What comes in

1. **CI.** On a pull request to main; on a push to main touching 10 paths; or by hand. Runs scripts/check-audit-allowlist.mjs, scripts/check-audit-allowlist.test.mjs, verify.sh and 21 more; checks colab_runner/.
2. **Deploy site to GitHub Pages.** On a push to main touching 2 paths; or by hand. Runs site/astro.config.mjs and site/src/.
3. **colab_runner** (the package's entry, not published from here). Loads colab_runner/__init__.py.

## What happens through CI

1. The workflow runs verify.sh in the repository root, tests/ in tests, scripts/check-audit-allowlist.mjs, scripts/check-audit-allowlist.test.mjs, site/astro.config.mjs and site/src/; it checks colab_runner/ in colab_runner.
2. It writes to site/audit-report.json, which is not tracked.
3. It uploads coverage to Codecov.

## Who reads the results

CI writes only to site/audit-report.json, which is not tracked.

## The other doors

**Deploy site to GitHub Pages** runs site/astro.config.mjs and site/src/, and deploys the site on main.

**colab_runner** (the package's entry, not published from here) loads colab_runner/__init__.py.

## What breaks what

- **colab_runner** is imported only from tests, by 1 part (tests), and sits on the path of 2 doors.

## What tends to change together

No two source files changed together often enough to name.

Window: 180 days; a pair counts from 3 shared commits, since the window holds fewer than 30 qualifying commits.

## What no test touches

Every code part is imported by at least one test.

## Written but never read

No place this map can see is written, so none goes unread.

## Helpers that look duplicated

No two parts export a helper that looks alike.

## Generated, never hand-edited

Nothing in this repository writes to a tracked place this map can see.

## Hand-authored

People write .github/ and the repository root; 7 writes with paths built at run time may land here.

## Where to start

CI runs no code this map can follow; it only checks code, so there is no path of files to read in order.

## What this map cannot see

- 7 writes and 6 reads use paths built at run time and are not named here.
- 1 write goes to places this repository does not track, so it is not listed as generated.
- 9 writes and 15 reads go to a path their caller passes, not to this repository.
- 1 write goes to a temporary directory or a path its caller passes, not to this repository.
- 18 files belong to no part: scripts/check-audit-allowlist.mjs, scripts/check-audit-allowlist.test.mjs, site/astro.config.mjs and 15 more.
- Statistics confidence is low: fewer than 30 qualifying commits in the window, and fewer than 25 source files reach 10 revisions.

Regenerate with `npx --yes @dogfood-lab/atlas map`.
