---
title: Reference
description: Every command, flag, exit code, end reason and error code of colab-runner 0.1.0.
sidebar:
  order: 3
---

## Commands

```text
usage: python -m colab_runner --version
       python -m colab_runner run --session S --out-dir D (--script P | --hold-s N) --wall-s N --max-rate R --cap C --cleanup-bound-s N [options] [--quiet | --verbose | --debug]
       python -m colab_runner stop --out-dir D [--quiet | --verbose | --debug]   (stops the session a killed run left in D/session_open.json)
exit codes: 0 ok, 1 refusal or usage error, 2 runtime error, 3 done but the session's stop not confirmed
```

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
| `cap` | The spend guard tripped: the priced spend reached the cap less the margin and the cleanup reserve, or a stable rate went above `--max-rate` (`RATE_ABOVE_MAX`). |
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
| `UNEXPECTED_ERROR` | no | read runner_result.json; if session_open.json is still in the out dir, run `python -m colab_runner stop --out-dir D`; rerun with --debug for the traceback |

## Environment variables

| Name | Where | What |
| --- | --- | --- |
| `COLAB_RUNNER_CLI` | rig | The `colab` command line to call, split with POSIX quoting (default `colab`). |
| `CR_RUN_ID` | VM | The run's own id. Reserved: a lane may not set it. |
| `CR_OUT_DIR` | VM | The job's out dir (default `<content>/<job>_out`). |
| `CR_PROGRESS_GLOB` | VM | Files whose first integer counts as progress (default `<out>/progress/*`). |
| `CR_UNITS_GLOB` | VM | Files whose count counts as progress. |
| `CR_RESIDENT_MAX_S` | VM | With `--resident`, how long the resident loop may run (default: the window's length). |
