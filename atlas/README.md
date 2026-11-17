# colab-runner: how it works

Mapped at 2026-11-17 from commit d5a5373 by Atlas 1.26.0.

## What this is

4 parts, mostly Python (29 files) and shell (2). Work enters through 2 doors; the busiest is CI, which reaches 3 parts.

## What changed since 2026-11-12 (cc633ab)

- colab_runner/ is now read by tests/test_errors.py.
- colab_runner/**/*.py is now read by tests/test_errors.py.
- colab_runner/vm/kill.py is now read by tests/test_window_lib.py.
- And 4 more new writers and readers of places.
- 10 files added and 17 changed content, across 4 parts.

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

People write .github/ and the repository root; 7 writes with paths built at run time may land here.

## Where to start

CI runs no code this map can follow; it only checks code, so there is no path of files to read in order.

## What this map cannot see

- 7 writes and 6 reads use paths built at run time and are not named here.
- 9 writes and 15 reads go to a path their caller passes, not to this repository.
- 1 write goes to a temporary directory or a path its caller passes, not to this repository.
- Statistics confidence is low: fewer than 30 qualifying commits in the window, and fewer than 25 source files reach 10 revisions.

Regenerate with `npx --yes @dogfood-lab/atlas map`.
