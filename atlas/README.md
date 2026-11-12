# colab-runner: how it works

Mapped at 2026-11-12 from commit 8bb60e3 by Atlas 1.26.0.

## What this is

4 parts, mostly Python (4 files) and shell (1). Work enters through 2 doors; the busiest is CI, which reaches 3 parts.

## What changed since the last map

This is the first map.

## What comes in

1. **CI.** On a pull request to main; on a push to main touching 7 paths; or by hand. Runs verify.sh and tests/; checks colab_runner/.
2. **colab_runner** (the package's entry, not published from here). Loads colab_runner/__init__.py.

## What happens through CI

1. The workflow runs verify.sh in the repository root and tests/ in tests; it checks colab_runner/ in colab_runner.
2. It uploads coverage to Codecov.

## Who reads the results

CI writes nothing this map can see.

## The other doors

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

People write .github/ and the repository root. Nothing in this repository writes to them.

## Where to start

.github/workflows/ci.yml → colab_runner/__main__.py

Read those in order to follow one pull request end to end.

## What this map cannot see

- Statistics confidence is low: fewer than 30 qualifying commits in the window, and fewer than 20 source files reach 10 revisions.

Regenerate with `npx --yes @dogfood-lab/atlas map`.
