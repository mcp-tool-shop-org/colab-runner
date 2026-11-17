# Changelog

## 0.1.0 (2026-11-17)

The first release for lanes to vendor (PR #1, merged as b3703db0).

- **The detached runner.** `python -m colab_runner run` starts a job on a Colab session that keeps running if the rig-side
  process dies. It polls with backoff, follows the job's log, and ends the window on done, wall, cap, setup stall, progress
  stall, a dead job, a failed start, or a sustained poll blackout. A hold job and a kernel-resident fallback are included.
- **Every exit stops its own session.** `session_open.json` marks an open session. `python -m colab_runner stop --out-dir D`
  stops one left by a killed process. Exit codes: 0 only for done with a confirmed stop, 1 for any other end, 3 for a refusal.
- **Spend guard.** The window is priced through the vendored library's rate and cap functions, never a copy of them.
  Unbounded or unpaired inputs are refused by name before any `colab` call: wall, cap, margin, cleanup bound, pre-open values.
- **Tokens.** Every CLI output, error line and downloaded file is scrubbed with the library's `scrub_colab_tokens`. Secrets
  reach the VM through the cell file, never through argv.
- **The window library is vendored byte for byte** (`colab_runner/window_lib/`, see its `PROVENANCE.md`), from the aspire-colab
  kit at 1cee476. A test fails if any vendored file drifts from its recorded sha256.

## 0.0.1 (unreleased)

- Scaffold: package stub, `verify.sh`, CI (paths-gated push, PR, dispatch, concurrency), Atlas map at the
  fleet pin, coverage floor 90 at the first measurement.
