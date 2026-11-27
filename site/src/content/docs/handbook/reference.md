---
title: Reference
description: Every command, flag, exit code, end reason and error code of colab-runner.
sidebar:
  order: 3
---

## Commands

```text
usage: python -m colab_runner --version
       python -m colab_runner launch --spec SPEC --out-dir D [--dry | --local-cpu] [options] [--quiet | --verbose | --debug]
       python -m colab_runner queue (validate | run) --queue SPEC [--dry | --local-cpu] [options]
       python -m colab_runner status --out-dir D [--json] [--quiet | --verbose | --debug]
       python -m colab_runner run --session S --out-dir D (--script P | --hold-s N) --wall-s N --max-rate R --cap C --cleanup-bound-s N [options] [--quiet | --verbose | --debug]
       python -m colab_runner stop --out-dir D [--quiet | --verbose | --debug]   (stops the session a killed run left in D/session_open.json)
exit codes: 0 ok, 1 refusal or usage error, 2 runtime error, 3 done but the session's stop not confirmed
```

### `launch` and `status`

- `launch --spec SPEC --out-dir D [--dry | --local-cpu] [--allow-sealed]`: validates the spec (refusing unknown keys/tables with `SPEC_INVALID`), card/shape rules, circuit breaker, study card lock, input sandbox, verified `[inputs.sealed]` pre-registration gate, and `SpendGuard` + open-phase budget; runs offline (`--dry` / `--local-cpu`) or opens a Colab session under a global lock, bounds the open phase from `colab new` against `--cap` and `--wall-s`, uploads flat into `/content`, supervises execution via `Runner`, downloads declared outputs (streaming tars and chunked `.partNNNN` transfers with SHA-256 verification), confirms the session stopped, and writes `outcome.json` on every exit. Live GPU/TPU launches require explicit `--cap` and `--max-rate` on the CLI. A `line` is a short label that groups related runs (for example `feature-eval` or `canary`) in `outcome.json` and `QUEUE_END`.
- `status --out-dir D [--json]`: inspects `dry_run_plan.json`, `window_meta.json` (reporting `orphaned` if `launcher_pid` died while `session_open.json` exists), `session_open.json`, `stop_confirmed.json`, `stop_pending.json`, `outcome.json`, and the log tail without calling Colab.

### `run`

```text
usage: colab_runner run [-h] --session SESSION --out-dir OUT_DIR [--job JOB]
                        [--content CONTENT] (--script SCRIPT |
                        --hold-s HOLD_S) [--heartbeat-s HEARTBEAT_S]
                        [--resident] --wall-s WALL_S [--poll-s POLL_S]
                        [--progress-stall-s PROGRESS_STALL_S]
                        [--setup-stall-s SETUP_STALL_S]
                        [--unreachable-polls UNREACHABLE_POLLS]
                        [--unreachable-s UNREACHABLE_S] [--max-rate MAX_RATE]
                        [--cap CAP] [--margin MARGIN]
                        [--cleanup-bound-s CLEANUP_BOUND_S]
                        [--pre-open-rate PRE_OPEN_RATE]
                        [--pre-open-assignments PRE_OPEN_ASSIGNMENTS]
                        [--env ENV] [--quiet | --verbose | --debug]

Start a detached job on a Colab VM and poll it.

options:
  -h, --help            show this help message and exit
  --session SESSION
  --out-dir OUT_DIR     must not exist yet, or be empty
  --job JOB
  --content CONTENT
  --script SCRIPT       bash script path on the VM
  --hold-s HOLD_S       heartbeat-only job for this many seconds
  --heartbeat-s HEARTBEAT_S
  --resident            kernel-resident fallback: the start cell stays busy
  --wall-s WALL_S
  --poll-s POLL_S
  --progress-stall-s PROGRESS_STALL_S
  --setup-stall-s SETUP_STALL_S
  --unreachable-polls UNREACHABLE_POLLS
                        this many failed polls in a row end the run
  --unreachable-s UNREACHABLE_S
                        this long without a good poll ends the run
  --max-rate MAX_RATE   REQUIRED: the highest rate per hour this window may
                        bill; prices it
  --cap CAP             REQUIRED: the window's spend cap, in compute units
  --margin MARGIN
  --cleanup-bound-s CLEANUP_BOUND_S
                        REQUIRED: worst-case seconds from a trip to a
                        confirmed stop (kill, salvage, stop); reserved from
                        the cap by the library's rule, cleanup_reserve =
                        cleanup_bound_s / 3600 x priced rate
  --pre-open-rate PRE_OPEN_RATE
                        the account's usage rate read just before the session
                        opened
  --pre-open-assignments PRE_OPEN_ASSIGNMENTS
                        the account's active assignments read just before then
  --env ENV             NAME=VALUE, or NAME alone to pass NAME's value from
                        this process's environment, which keeps a secret off
                        every command line (repeatable). Values reach the VM
                        inside the cell file, never as arguments. CR_RUN_ID is
                        reserved (RUN_ID_RESERVED).
  --quiet               only the end lines
  --verbose             also one line per poll
  --debug               also the traceback of an error (scrubbed)
```

### `stop`

```text
usage: colab_runner stop [-h] --out-dir OUT_DIR [--quiet | --verbose |
                         --debug]

Stop the session a run left open in D.

options:
  -h, --help         show this help message and exit
  --out-dir OUT_DIR
  --quiet            only the end lines
  --verbose          also one line per poll
  --debug            also the traceback of an error (scrubbed)
```

## Exit codes, refusals and log levels

**Exit codes** (the org convention), for `run` and `stop --out-dir D`:

| Exit | Meaning | `run` | `stop` |
| --- | --- | --- | --- |
| 0 | ok | `done`, and the stop confirmed | a confirmed stop, or nothing to stop |
| 1 | user error | a refusal (`REFUSED: …`) or a usage error, before the session marker was written | a marker that names no session (`SESSION_MARKER_INVALID`), or a usage error |
| 2 | runtime error | any end but `done` (whatever its stop did), or `RUN_ERROR` | a stop that was not confirmed, or `STOP_ERROR` |
| 3 | partial success | `done`, but the stop was not confirmed: `session_open.json` is still there | (not used) |

On exit 3 the job's work is in the out dir, and the session must still be stopped with `python -m colab_runner stop --out-dir D`.

A refusal after the marker is not an exit 1. A reused `--job`, for one, ends exit 2: the runner owned the session by
then, so it stopped it, and the last line is `RUN_END reason=start_failed … code=JOB_ALREADY_EXISTS` with a hint naming
`--job` on stderr. No `REFUSED:` line is written.

**Refusals and errors** have a code, a message and a hint, and every line is scrubbed:
- A refusal writes `REFUSED: CODE: message` as the first line on stderr, then `hint: …` (what to change).
- An error during a run writes `RUN_ERROR: CODE: message`, then `hint: …`. The code is the error's own
  (`BINARY_DOWNLOAD_TOKEN`, `QUARANTINE_ESCAPE`), or `UNEXPECTED_ERROR` with the exception's type in the message. `stop` writes `STOP_ERROR: …`
  the same way.
- A run that ends on a named refusal after the marker (`JOB_ALREADY_EXISTS`, `BINARY_DOWNLOAD_TOKEN`,
  `QUARANTINE_ESCAPE`) records it in
  `runner_result.json` as `refusal: {code, message, hint, retryable}`. `retryable` is false for every refusal of the
  caller's input: the same call fails the same way again.
- A `cap` end is not a refusal, but it records its trip the same way, in `runner_result.json` as
  `cap_trip: {code, message, hint, retryable}`, and writes the hint on stderr after `RUN_END`. The code is the trip's
  leading `CODE:` (`CAP_GUARD_TRIPPED`, `COLAB_USAGE_PARSE_FAILED`, `RATE_ABOVE_MAX` or `ASSIGNMENTS_UNREADABLE`), or
  `CAP_CEILING_REACHED` when the clock reached the ceiling before the guard tripped. `detail` keeps the trip string.
  Every other end has `cap_trip: null`.
- Every code and its hint is in `colab_runner/errors.py` (`CODES`).

**Log levels** (`run` and `stop`): `--quiet` writes only the end lines (`RUN_END`, `STOP_END` or `STOP_NOTHING`, and a
refusal or an error with its hint). The default adds the runner's log on stderr. `--verbose` adds one line per poll. `--debug` adds an error's
traceback. A token is scrubbed at every level, the traceback included.

## End reasons

| Reason | When |
| --- | --- |
| `oom` | The job's status file is in, and it shows rc 137 or the job's log has an out-of-memory kill line (whatever the rc). |
| `failed` | The job's status file shows a non-zero rc other than 137, and its log has no out-of-memory kill line. |
| `done` | The job's status file shows rc 0, and its log has no out-of-memory kill line. |
| `wall` | The run reached `--wall-s`. |
| `cap` | The spend guard tripped: local priced spend (`own_rate x elapsed`) reached the cap less the margin and the cleanup reserve, or open-time rate went above `--max-rate` (`RATE_ABOVE_MAX`). |
| `setup_stall` | `setup.log` stopped growing for `--setup-stall-s` during setup. |
| `progress_stall` | Nothing the runner can see moved for `--progress-stall-s` after setup. |
| `dead` | The job's process is not alive and it left no status file. |
| `unreachable` | No good poll for `--unreachable-polls` in a row, or for `--unreachable-s`. |
| `start_failed` | The job's start was never confirmed, or it was refused (`JOB_ALREADY_EXISTS`). |
| `interrupted` | SIGINT or SIGTERM reached the runner. |
| `error` | An exception, a refused download included. It prints `RUN_ERROR:` and no `RUN_END`. |

## Error codes

Every code is in `colab_runner/errors.py` (`CODES`). `Retryable` says whether the same call, unchanged, is worth trying
again.

| Code | Retryable | Hint |
| --- | --- | --- |
| `CAP_REQUIRED` | no | pass --cap C, the window's spend cap in compute units |
| `CAP_INVALID` | no | pass --cap as a finite number greater than 0 |
| `MAX_RATE_REQUIRED` | no | pass --max-rate R, the card's expected rate per hour; it prices the window |
| `MAX_RATE_INVALID` | no | pass --max-rate as a finite number greater than 0 |
| `MARGIN_INVALID` | no | pass --margin as a finite number of 0 or more |
| `CLEANUP_BOUND_REQUIRED` | no | pass --cleanup-bound-s N, at least the runner's own cleanup bound (466 s, or 986 s with --resident, at the default timeouts) |
| `CLEANUP_BOUND_INVALID` | no | pass --cleanup-bound-s as a finite number of 0 or more |
| `CLEANUP_BOUND_TOO_SMALL` | no | raise --cleanup-bound-s to at least the bound the message names |
| `CAP_TOO_SMALL_FOR_CLEANUP` | no | raise --cap, or lower --margin or --cleanup-bound-s: the cap must exceed the margin plus the cleanup reserve |
| `PRE_OPEN_RATE_INVALID` | no | pass --pre-open-rate as a finite number of 0 or more, read before the session opened |
| `PRE_OPEN_ASSIGNMENTS_INVALID` | no | pass --pre-open-assignments as a whole number of 0 or more |
| `PRE_OPEN_UNPAIRED` | no | pass --pre-open-rate and --pre-open-assignments together, from one reading, or neither |
| `WALL_INVALID` | no | pass --wall-s as a finite number of seconds greater than 0 |
| `POLL_INVALID` | no | pass --poll-s as a finite number of seconds greater than 0 |
| `HEARTBEAT_INVALID` | no | pass --heartbeat-s as a finite number of seconds (greater than 0 with --hold-s) |
| `HOLD_INVALID` | no | pass --hold-s as a finite number of seconds greater than 0 |
| `PROGRESS_STALL_INVALID` | no | pass --progress-stall-s as a finite number of seconds greater than 0 |
| `SETUP_STALL_INVALID` | no | pass --setup-stall-s as a finite number of seconds greater than 0 |
| `UNREACHABLE_INVALID` | no | pass --unreachable-polls as a whole number of 1 or more, and --unreachable-s as a finite number of seconds greater than 0 |
| `DEAD_CONFIRM_INVALID` | no | pass dead_confirm as a whole number of 1 or more |
| `SETTING_INVALID` | no | pass the Runner setting the message names as a finite number in its range |
| `NOTHING_TO_RUN` | no | pass script= (--script) or hold_s= (--hold-s) |
| `RESIDENT_MAX_INVALID` | no | pass CR_RESIDENT_MAX_S as a finite number of seconds greater than 0, or leave it out (the window's length is the default) |
| `OUT_DIR_NOT_EMPTY` | no | pass an --out-dir that does not exist yet, or is empty: a new one for each run |
| `OUT_DIR_INVALID` | no | pass an --out-dir that can be created and listed, below a filesystem root (its quarantine is a sibling folder) |
| `CLI_INVALID` | no | fix COLAB_RUNNER_CLI: it is split with POSIX shell quoting, so close every quote (or unset it to use `colab`) |
| `ENV_INVALID` | no | pass --env as NAME=VALUE or NAME, with NAME a variable name |
| `ENV_UNSET` | no | set the variable in this environment before the run, or pass --env NAME=VALUE |
| `RUN_ID_RESERVED` | no | drop CR_RUN_ID from --env: the runner gives every run its own id |
| `SESSION_MARKER_INVALID` | no | stop the session by name with `colab stop -s S`: this session_open.json does not name one |
| `STOP_NOT_CONFIRMED` | yes | run `python -m colab_runner stop --out-dir D` again; session_open.json stays until a stop is confirmed |
| `JOB_ALREADY_EXISTS` | no | pass a distinct --job for each step that shares a --content directory |
| `BINARY_DOWNLOAD_TOKEN` | no | keep tokens out of the job's binary outputs; delete the quarantine named in runner_result.json and never ship it |
| `QUARANTINE_ESCAPE` | no | report it: the file was removed rather than moved outside its quarantine; pass a plain --out-dir (or a download path) without `..` |
| `CAP_GUARD_TRIPPED` | no | the window's priced spend reached its ceiling (the cap less --margin and the cleanup reserve): raise --cap, or give the job less to do in one window |
| `CAP_CEILING_REACHED` | no | the clock reached the cap's ceiling at the priced rate (the cap less --margin and the cleanup reserve): raise --cap, or give the job less to do in one window |
| `RATE_ABOVE_MAX` | no | the session's open-time rate exceeded --max-rate: pass the card's real rate as --max-rate (and a matching --cap) |
| `PLATFORM_UNSUPPORTED` | no | run colab-runner on Linux or inside WSL |
| `UNEXPECTED_ERROR` | no | read runner_result.json; if session_open.json is still in the out dir, run `python -m colab_runner stop --out-dir D`; rerun with --debug for the traceback |
| `SPEC_NOT_FOUND` | no | pass --spec PATH pointing to an existing .toml or .json window spec |
| `SPEC_INVALID` | no | fix the window spec file (TOML or JSON) at the field named in the message |
| `INPUT_ROOT_INVALID` | no | set inputs.root to an existing directory on the local filesystem |
| `INPUT_PATH_OUTSIDE_ROOT` | no | keep every inputs.files src and dst relative and inside inputs.root without '..' |
| `INPUT_SYMLINK_REFUSED` | no | replace the symlink or junction in inputs.files with a regular file inside inputs.root |
| `INPUT_FILE_MISSING` | no | ensure every file listed in inputs.files exists as a regular file before launching |
| `SEALED_DATA_REFUSED` | no | declare a verified [inputs.sealed] pre-registration (read-only file, sha256, study id, exact paths) or remove the sealed/outside-repo path |
| `SEALED_PREREG_INVALID` | no | provide an existing read-only pre-registration file matching preregistration_sha256, this study id, and exact non-glob sealed_path entries |
| `SEALED_DESTINATION_REFUSED` | no | launch only to a card and host allowed by both [inputs.sealed] and the pre-registration text |
| `ALLOW_SEALED_REQUIRED` | no | pass --allow-sealed on the command line to confirm a live launch carrying pre-registered sealed inputs |
| `OUTPUT_PATH_INVALID` | no | list outputs.collect and outputs.salvage as relative paths without '..' |
| `SESSIONS_READ_FAILED` | yes | check colab sessions (sign-in, network); active sessions must be readable before opening or verifying a window |
| `CARD_UNKNOWN` | no | choose a known Colab card from the catalog (CPU, T4, L4, G4, A100) |
| `CARD_UNAVAILABLE` | no | choose an available card (CPU, T4, L4, G4, A100); this card is unavailable on the plan |
| `CARD_CIRCUIT_OPEN` | no | two allocation or card refusals already tripped this card's circuit breaker today (UTC); wait for the next UTC day or start a new study on an available card |
| `CARD_MIX_REFUSED` | no | keep one card type for the whole study: do not pool G4 and A100 or switch card mid-study |
| `SHAPE_REQUIRED` | no | set compute.shape (or --shape) to 'standard' or 'high-mem' for a GPU card |
| `SHAPE_INVALID` | no | set compute.shape (or --shape) to 'standard' or 'high-mem' |
| `SHAPE_NOT_FOR_CPU` | no | omit compute.shape (and --shape) for a CPU card; CPU always opens with --high-mem |
| `SHAPE_HIGH_MEM_UNAVAILABLE` | no | use shape='standard' for L4; Colab offers L4 only in Standard shape |
| `SHAPE_STANDARD_UNAVAILABLE` | no | use shape='high-mem' for this accelerator; Colab offers it only in High-RAM shape |
| `OPEN_LOCK_TIMEOUT` | yes | another window held the open lock past the timeout; wait for concurrent opens to finish and retry |
| `SESSION_ALREADY_OPEN` | no | pass a fresh --session name, or stop the existing session first |
| `POOL_CEILING_REACHED` | yes | close an active Colab session to free a slot below the plan's pool ceiling, then retry |
| `SESSION_OPEN_FAILED` | yes | Colab refused or failed to assign the requested session or card; check availability or circuit-breaker state before retrying |
| `SHAPE_MISMATCH` | yes | Colab assigned a different machine shape than requested; the session was stopped |
| `UPLOAD_FAILED` | yes | check session connectivity and file size; colab upload into /content failed and the session was stopped |
| `CARD_MISMATCH` | yes | Colab assigned a GPU that failed the requested card's name, VRAM, or compute-capability floor; the session was stopped |
| `STATUS_DIR_MISSING` | no | pass --out-dir pointing to an existing run or launch output directory |

## Environment variables

| Name | Where | What |
| --- | --- | --- |
| `COLAB_RUNNER_CLI` | local | The `colab` command line to call, split with POSIX quoting (default `colab`). |
| `CR_DOWNLOAD_CHUNK_BYTES` | local | Chunk threshold in bytes (default `268435456`, 256 MiB) for splitting oversized output files/tars into `.partNNNN` slices. |
| `CR_RUN_ID` | VM | The run's own id. Reserved: a caller may not set it. |
| `CR_OUT_DIR` | VM | The job's out dir (default `<content>/<job>_out`). |
| `CR_PROGRESS_GLOB` | VM | Files whose first integer counts as progress (default `<out>/progress/*`). |
| `CR_UNITS_GLOB` | VM | Files whose count counts as progress. |
| `CR_RESIDENT_MAX_S` | VM | With `--resident`, how long the resident loop may run (default: the window's length). |
