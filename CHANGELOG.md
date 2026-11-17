# Changelog

## 0.1.0 (2026-11-17)

The first release for lanes to vendor (PR #1, merged as b3703db0). How a lane calls it is in the README, "Using it from a lane".

- **The detached runner.** `python -m colab_runner run` starts a job on a Colab session the lane has already opened, in a process
  that keeps running on the VM if the rig-side process dies. It polls with backoff and follows the job's log. A run ends as one
  of: `done`, `failed` (the job exited non-zero), `oom`, `wall`, `cap`, `setup_stall`, `progress_stall`, `dead`, `unreachable`
  (a sustained poll blackout), `start_failed`, `interrupted` (SIGINT/SIGTERM) or `error`. There is a hold job (`--hold-s`), and a
  kernel-resident fallback (`--resident`) that polls the job's status file by download and downloads the job's log once, when
  the job has ended.
- **Stopping the session.** Once `run` has written `session_open.json` in the out dir, every exit tries to stop the session,
  and the marker is removed only after a confirmed stop. A stop that fails leaves the marker, and
  `python -m colab_runner stop --out-dir D` stops that session later. A refusal or a usage error happens before the marker is
  written; the session the lane opened is then the lane's to stop.
- **Exit codes.** `run`: 0 for `done` with a confirmed stop, 1 for any other end (or a failed stop, or an error), 2 for a usage
  error, 3 for a refusal. `stop`: 0 for a confirmed stop or nothing to stop, 1 for a failed stop, 3 for a marker that names no
  session.
- **Spend guard.** The window is priced through the vendored library's rate and cap functions, never a copy of them. Among the
  inputs refused by name (exit 3) before any `colab` call:
  - a missing or invalid `--max-rate`, `--cap` or `--cleanup-bound-s`;
  - an invalid `--wall-s`, `--margin`, `--poll-s`, `--heartbeat-s`, stall or unreachable setting;
  - a cleanup bound below the runner's own;
  - a cap too small for the margin plus the cleanup reserve (the library's budget rule);
  - pre-open values given one without the other.

  A missing `--wall-s` is a usage error (exit 2, from argparse), not a named refusal.
- **Tokens.** Every `colab` CLI output and error line, every runner error line, and every downloaded file is scrubbed with the
  library's `scrub_colab_tokens`. `--env` values reach the VM inside the cell file, never as arguments to the `colab` CLI. A lane
  should pass a secret as `--env NAME` with NAME set in its environment: `--env NAME=VALUE` puts the value in the runner's own
  command line.
- **The window library is vendored byte for byte** (`colab_runner/window_lib/`, see its `PROVENANCE.md`), from the aspire-colab
  kit at 1cee476. A test fails if any vendored file drifts from its recorded sha256.

## 0.0.1 (unreleased)

- Scaffold: package stub, `verify.sh`, CI (paths-gated push, PR, dispatch, concurrency), Atlas map at the
  fleet pin, coverage floor 90 at the first measurement.
