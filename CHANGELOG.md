# Changelog

## 0.1.0 (2026-11-17)

The first release for lanes to vendor. The v0.1.0 tag goes on the merge of the ship-gate pull request, and carries PR #1
(the detached runner, merged as b3703db0), PR #2 (this version and changelog), PR #3 (the binary-safe download scrub, the
`--job` refusal and the 0.1.0 follow-ups) and the ship gate (structured errors, the org's exit codes, log levels, pip-audit
in CI, and the reviews' follow-ups). How a lane calls it is in the README, "Using it from a lane".

- **The detached runner.** `python -m colab_runner run` starts a job on a Colab session the lane has already opened, in a process
  that keeps running on the VM if the rig-side process dies. It polls with backoff and follows the job's log (`<job>.run.out`,
  which appears once the job writes log lines). Each step sharing `--content` needs a distinct `--job`. A run ends as one of: `done`, `failed` (the job exited non-zero), `oom`, `wall`, `cap`, `setup_stall`, `progress_stall`, `dead`,
  `unreachable` (a sustained poll blackout), `start_failed`, `interrupted` (SIGINT/SIGTERM) or `error`. There is a hold job
  (`--hold-s`), and a kernel-resident fallback (`--resident`, using `math.ceil(left)` for its default `CR_RESIDENT_MAX_S`) that
  polls the job's `<job>.poll.json` by download and collects the job's log when the job has ended, or at the end of a run that
  tripped. `COLAB_RUNNER_CLI` is split with `shlex.split`, POSIX quoting (on Windows, forward slashes or a quoted path).
- **The `--job` refusal, by run id.**
  - Every run has its own id, `CR_RUN_ID`, a fresh one each time; every cell carries it. The name is reserved: a lane's
    `--env CR_RUN_ID=…` is refused (`RUN_ID_RESERVED`). A record's run id is compared as a string, so a JSON `null` never
    matches, and `Runner.start` strips the id it stores, as `run` does.
  - The VM writes it to `<job>.run_id` before the job starts. The job's status file and every poll record carry it.
  - If any of the job's files (`<job>.pid`, `.status.json`, `.run.out`, `.poll.json`, `.run_id`) is there under another run
    id (or under none), the start is refused and the run ends `start_failed` with `JOB_ALREADY_EXISTS`. That holds whether the other job's
    process is alive or dead, and with or without a status file.
  - A refused start touches none of those files. A kill cell for another run's job answers `CR_NOT_OURS` and signals
    nothing. A refused resident cell writes `<job>.refused.json`, never `<job>.poll.json`.
  - The runner takes only records carrying its own run id, so a record another run left (step 1's final `<job>.poll.json`)
    never ends a run. In `--resident` mode, seeing only another run's records within the setup window ends `start_failed`
    with `JOB_ALREADY_EXISTS`, and that run's job is neither killed nor has its log collected. Without `--resident`, such a
    record is a failed poll.
  - A `--resident` step that trips before any record is seen (the wall, say) gets `CR_NOT_OURS` from its kill cell when the
    `--job` is another run's, and then never collects that run's `<job>.run.out` either.
  - A retried start of the same run id is not refused.
- **Stopping the session.** Once `run` has written `session_open.json` in the out dir, every exit tries to stop the session,
  and the marker is removed only after a confirmed stop. A stop that fails leaves the marker, and
  `python -m colab_runner stop --out-dir D` stops that session later. A refusal or a usage error happens before the marker is
  written; the session the lane opened is then the lane's to stop.
- **Exit codes, the org convention.** 0 ok; 1 a user error (a refusal or a usage error, before any marker); 2 a runtime
  error; 3 partial success.
  - `run`: 0 for `done` with a confirmed stop; 1 for a refusal or a usage error; 2 for any other end, whatever its stop did,
    and for `RUN_ERROR`; 3 for `done` whose stop was not confirmed (`session_open.json` is left for
    `python -m colab_runner stop`).
  - `stop`: 0 for a confirmed stop or nothing to stop; 1 for a marker that names no session (`SESSION_MARKER_INVALID`) or
    a usage error; 2 for a stop that was not confirmed, and for `STOP_ERROR`.
- **Structured errors.** Every refusal and error has a code, a message, a one-line hint and `retryable` (`CODES` in
  `colab_runner/errors.py`; a test reads every code from the source and checks each has its entry).
  - A refusal prints `REFUSED: CODE: message`, then `hint: …`, on stderr. An error during a run prints
    `RUN_ERROR: CODE: message` (`BINARY_DOWNLOAD_TOKEN`, or `UNEXPECTED_ERROR` with the exception's type), then `hint: …`;
    `stop` prints `STOP_ERROR: …` the same way. Every line is scrubbed.
  - A run that ends on a named refusal after the marker (`JOB_ALREADY_EXISTS`, `BINARY_DOWNLOAD_TOKEN`) records
    `refusal: {code, message, hint, retryable}` in `runner_result.json`, and its `RUN_END` line ends ` code=CODE`.
  - A binary download refused during cleanup turns the end into `error` and keeps the end it replaced (`prior_reason`).
- **Log levels.** `--quiet`, `--verbose` and `--debug` on `run` and `stop`; normal is the default. Quiet writes only the end
  lines; normal adds the runner's log; verbose adds a line per poll; debug adds an error's traceback. Every level is
  scrubbed, the traceback included.
- **Spend guard.** The window is priced through the vendored library's rate and cap functions, never a copy of them. When a
  `colab usage` read or retry is skipped at the window bound (`< MIN_CALL_S` left), the end reason names the bound that actually
  bit (`wall` or `cap`). Among the inputs refused by name (exit 1) before any `colab` call:
  - a missing or invalid `--max-rate`, `--cap` or `--cleanup-bound-s`;
  - an invalid `--wall-s`, `--margin`, `--poll-s`, `--heartbeat-s`, stall or unreachable setting, or a non-finite/non-positive
    `CR_RESIDENT_MAX_S` in `--resident` mode (`RESIDENT_MAX_INVALID`);
  - a cleanup bound below the runner's own;
  - a cap too small for the margin plus the cleanup reserve (the library's budget rule);
  - pre-open values given one without the other;
  - an out dir that is a filesystem root (`OUT_DIR_INVALID`).

  A missing `--wall-s` is a usage error (exit 1, from argparse), not a named refusal.
- **Tokens and binary-safe download scrub.** Every `colab` CLI output and error line is scrubbed with the library's
  `scrub_colab_tokens`. Downloaded files and directories (including partial, failed, or interrupted transfers) decide text vs
  binary by content (NUL byte or failed strict UTF-8 decode).
  - Text files are scrubbed in place with `scrub_colab_tokens`. Clean binaries are left byte-identical. Unreadable files are
    removed.
  - A downloaded symlink is unlinked, never followed (the vendored `scrub_file_in_place` followed it and rewrote its
    target). Only regular files are read: a FIFO, a socket or a device is unlinked, never read.
  - A directory is scrubbed file by file, and one failure never stops the rest: a hit whose move to quarantine fails is
    removed, and a subdirectory that cannot be listed is noted; the first such error is raised once the other files are
    scrubbed.
  - A binary is scanned with `BYTE_TOKEN_PATTERNS`, byte twins derived from the sources of `TOKEN_SCRUB_PATTERNS`. On UTF-8
    they match exactly what the text patterns match, with every str `\s` character as a space (`\x1c`-`\x1f`, NBSP, NEL, EM
    SPACE and the rest); next to a non-ASCII letter the `hf_` twin matches more, never less. The derivation refuses what it
    cannot translate span for span: a negated class not quantified with `+` or `*`, an unescaped `.`, `\S`/`\w`/`\d` and
    their negations, and a non-ASCII class member.
  - In a binary, `Bearer` counts only with a token-shaped value: at least 20 characters of `[A-Za-z0-9._-]`, using at least two of
    lower case, upper case and digits (`binary_token_hit`). A render whose prompt says "Flame Bearer of the North" comes
    through byte-identical.
  - A hit is moved unchanged to quarantine, recorded in `runner_result.json` (`quarantined`) by path and pattern name only,
    and refused with `BinaryDownloadRefused` (`BINARY_DOWNLOAD_TOKEN`). That ends `Runner.run` as `error` (exit 2), and the
    session is still stopped. A quarantine from a `Runner.download` made after `run()` returned rewrites
    `runner_result.json` with it.
  - Through `Runner` the quarantine is `<out-dir>.quarantine/`, named from the resolved out dir (so an out dir ending in
    `..` gets its real folder's sibling), outside `--out-dir`, so `salvage` and bundles never see it. A
    bare `Colab.download` or `scrub_local` with no `quarantine_dir` uses a new `colab-runner-quarantine-*` folder under the
    system temp dir, never next to the file. The lane must delete a quarantine and never ship it.
  - `--env` values reach the VM inside the cell file, never as arguments to the `colab` CLI. A lane should pass a secret as
    `--env NAME` with NAME set in its environment: `--env NAME=VALUE` puts the value in the runner's own command line.
- **The window library is vendored byte for byte** (`colab_runner/window_lib/`, see its `PROVENANCE.md`), from the aspire-colab
  kit at 1cee476. A provenance test hashes the vendored files and `window_lib/__init__.py`, refuses duplicate table rows, and
  verifies the wheel (`pip wheel --no-build-isolation`) ships `window_lib/*` and `vm/*`. Coverage includes `colab_runner/vm/*`.
- **Dependency scanning.** CI runs a pinned `pip-audit` (2.10.1) over the dev dependencies and what installing the package
  pulled in, in one matrix leg. The package itself has no runtime dependencies.

## 0.0.1 (unreleased)

- Scaffold: package stub, `verify.sh`, CI (paths-gated push, PR, dispatch, concurrency), Atlas map at the
  fleet pin, coverage floor 90 at the first measurement.
