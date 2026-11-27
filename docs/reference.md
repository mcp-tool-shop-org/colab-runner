# colab-runner reference

The full behaviour of every command, for callers who need the detail. Start with the [README](../README.md).

### Option B: Attach to an already-open session (`run` / `stop`)

For callers that open a session separately, `run` takes it over and ends by stopping it:

```bash
colab new -s my-window            # caller's own step: open the session (plus the card's flags)
python -m colab_runner run --session my-window --out-dir runs/w1 --script /content/train.sh --wall-s 3600 --max-rate 13.5 --cap 15 --cleanup-bound-s 600
echo "exit $?"                    # 0 done and stopped; see the exit codes below
```

The figures are placeholders. Set `--max-rate` to the card's expected units per hour and `--cap` to the window's
budget in compute units (see Pricing).

`python -m colab_runner --help` lists every command; `launch --help`, `status --help`, and `run --help` list every flag.

## Using it from a lane

**The session is the lane's.** The lane opens the Colab session and passes its name. Once `run` has written
`session_open.json` in the out dir, every exit tries to stop the session, and the marker is removed only after a confirmed stop.
- **A marker that stays** means the session may still be open: a stop that failed, or a `run` killed outright (SIGKILL, a lost
  rig) before its cleanup ran. The lane then runs `python -m colab_runner stop --out-dir D`.
- **Before the marker exists** (a refusal or a usage error, both exit 1), the lane runs `colab stop -s S` itself.

**Required flags of `run`:** `--session S --out-dir D (--script P | --hold-s N) --wall-s N --max-rate R --cap C
--cleanup-bound-s N`.
- `--out-dir` must not exist yet, or be empty (`OUT_DIR_NOT_EMPTY` otherwise). A filesystem root is refused
  (`OUT_DIR_INVALID`): the quarantine is a sibling of the out dir, named from its resolved path. So is an out dir that
  cannot be created or listed.
- `--cleanup-bound-s` must be equal to or greater than the runner's own cleanup bound (`CLEANUP_BOUND_TOO_SMALL` otherwise). At the default
  call timeouts that bound is 466 s, or 986 s with `--resident` (`Runner.own_cleanup_bound_s`).
- Pass `--pre-open-rate` and `--pre-open-assignments` together, read just before the session opened, or neither. Without them
  the whole account rate is attributed to this window, so a neighbour session can trip the rate limit.
- Pass a distinct `--job` (default `job`) for each step sharing a `--content` directory. How a reused `--job` is refused:
  - Every run has its own run id, `CR_RUN_ID`, a fresh one each time. The name is reserved: `--env CR_RUN_ID=…` (or
    `CR_RUN_ID` in `Runner.run(env=...)`) is refused (`RUN_ID_RESERVED`), since one id reused across steps would turn
    this refusal off between them.
  - The VM writes the run id to `<job>.run_id` before the job starts. The job's status file and every poll record carry it.
  - The job's files are `<job>.pid`, `.status.json`, `.run.out`, `.poll.json` and `.run_id`. If any of them is there under
    another run id (or under none), the step ends `start_failed` with `JOB_ALREADY_EXISTS`. That holds whether the other job's process is
    alive or dead, and with or without a status file.
  - A refused step touches none of the other run's files, never kills its job, and still stops its own session. In
    `--resident` mode the refusal goes to `<job>.refused.json`, never to `<job>.poll.json`.
  - The runner takes only records that carry its own run id. A record another run left under the same `--job` (step 1's
    final `<job>.poll.json`, say) never ends a step. In `--resident` mode, if only another run's records appear within
    `--setup-stall-s`, the step ends `start_failed` with `JOB_ALREADY_EXISTS`. Without `--resident`, such a record counts as
    a failed poll.
  - A trip before any record was seen (the wall, say, while another run's job holds the `--job`) still kills nothing:
    the kill cell answers `CR_NOT_OURS`, and then the other run's `<job>.run.out` is not collected either.
  - A retried start of the same run (one whose reply was lost) is not refused.
- In `--resident` mode, a non-finite or non-positive `CR_RESIDENT_MAX_S` passed via `--env` is refused up front
  (`RESIDENT_MAX_INVALID`, exit 1).

**Exit codes** (the org convention), for `launch`, `run`, and `stop --out-dir D`:

| Exit | Meaning | `launch` / `run` | `stop` |
| --- | --- | --- | --- |
| 0 | ok | `done` (and all declared outputs collected), and the stop confirmed | a confirmed stop, or nothing to stop |
| 1 | user error | a refusal (`REFUSED: …`) or a usage error, before the session marker was written | a marker that names no session (`SESSION_MARKER_INVALID`), or a usage error |
| 2 | runtime error | any end other than `done` — including `partial` (missing or refused declared outputs), `failed`, trips, or `RUN_ERROR` | a stop that was not confirmed, or `STOP_ERROR` |
| 3 | partial success | `done` (with all declared outputs collected), but `colab stop` was not confirmed: `session_open.json` remains in `--out-dir` | (not used) |

On exit 3 the job's outputs are in the out dir, and the session must still be stopped with `python -m colab_runner stop --out-dir D` (which removes `session_open.json` once stop confirms).

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

**How a run ends:** `done`, `failed` (non-zero exit), `oom`, `wall`, `cap`, `setup_stall`, `progress_stall`, `dead`,
`unreachable`, `start_failed`, `interrupted` or `error`.
- Every end but `error` writes `RUN_END reason=… rc=… stopped=…` as the last line on stdout, with ` code=CODE` added when
  the run ended on a named refusal. A hint follows on stderr for such a refusal, and for a stop that was not confirmed.
- An `error` end writes `RUN_ERROR: …` on stderr and no `RUN_END` line; when `session_open.json` is still in the out dir,
  the hint for a stop that was not confirmed follows. A refusal or a usage error (exit 1) writes to stderr only.
- `runner_result.json` in the out dir records every end after the marker was written, `error` included. A binary download
  refused during cleanup turns the end into `error` and keeps the end it replaced in `prior_reason` (and in the detail).

**What `run` writes in the out dir** (`--job` defaults to `job`, `--content` to `/content`):
- `session_open.json` while the session may be open;
- `runner_result.json`, the run's end. Its `quarantined` lists each binary download quarantined during the run, and a
  quarantine from a later `Runner.download` through the same `Runner` rewrites the file with it;
- `<job>.run.out`, the job's log (appears only once the job has written log lines, or once `--resident` has collected it);
- when `--resident` is set, `<job>.poll.json` (the job's latest poll record, as the runner downloaded it) and `<job>.refused.json`, this run's
  own refusal record, when its refused resident cell wrote one (a `JOB_ALREADY_EXISTS` that only timed out on another
  run's records has none);
- `stop_confirmed.json` after a confirmed stop, or `stop_pending.json` after a failed one.

**The `colab` command** is `colab` unless `COLAB_RUNNER_CLI` names another command line. It is split with `shlex.split`,
which uses POSIX quoting: on Windows, write the path with forward slashes or quote it (`"C:\tools\colab.exe"`), since an
unquoted backslash is dropped. One that cannot be split (an unclosed quote) is refused (`CLI_INVALID`, exit 1).

**What the job must write, so the stall guards can see it.** Paths are on the VM; the out dir is `CR_OUT_DIR`, by default
`<content>/<job>_out`.
- `phase.txt`: absent, empty or `setup` while installing, anything else once running. Once the job leaves setup,
  colab-runner keeps the last known phase on an empty or missing read of `phase.txt` and never returns to setup; if
  `phase.txt` reads `setup` again after a run phase, colab-runner resets the setup stall clock. Jobs started with
  `--hold-s` skip the setup phase.
- `setup.log`: during setup, a run whose `setup.log` stops growing for `--setup-stall-s` (default 600) ends `setup_stall`.
- **Progress:** after setup, a run ends `progress_stall` when nothing it can see moves for `--progress-stall-s` (default 900).
  What it sees is the sum of the first integer in each file matching `CR_PROGRESS_GLOB` (default `<out>/progress/*`), the count
  of files matching `CR_UNITS_GLOB`, and the hold heartbeats.
- A run with no good poll for `--unreachable-polls` polls in a row (default 10), or for `--unreachable-s` (default 1800), ends
  `unreachable`.

**Write `phase.txt` atomically.** The kit writes `phase.txt`; colab-runner reads it. Rewriting `phase.txt` in place
(for example `echo run > phase.txt`) truncates the file first, so colab-runner can read an empty file. Write the new phase
to a temporary file in the same directory, then rename the temporary file to `phase.txt`:

```bash
printf 'run\n' > "$CR_OUT_DIR/phase.tmp" && mv "$CR_OUT_DIR/phase.tmp" "$CR_OUT_DIR/phase.txt"
```

```python
import os
from pathlib import Path

out = Path(os.environ["CR_OUT_DIR"])
tmp = out / "phase.tmp"
tmp.write_text("run\n", encoding="utf-8")
os.replace(tmp, out / "phase.txt")
```

**Secrets & binary-safe download scrub.** Pass a secret as `--env NAME`, with NAME set in the lane's environment. `--env NAME=VALUE`
puts the value in the runner's own command line. Values reach the VM inside the uploaded notebook cell file, never as arguments to the `colab` CLI.
Every CLI output line and error line is scrubbed with the vendored library's `scrub_colab_tokens`. Downloaded files and
directories (including partial transfers) are classified by content only (a NUL byte or failed strict UTF-8 decode is binary):
- Text files are scrubbed in place with `scrub_colab_tokens`.
- Clean binary files are left byte-identical. Unreadable files are removed.
- **The binary rule.** A binary file is scanned with byte twins of the library's token patterns.
  - On UTF-8 the twins match exactly what the text patterns match on the decoded text. Every character Python's `\s` matches
    counts as a space, `\x1c`-`\x1f`, NBSP, NEL and EM SPACE included.
  - The one difference is next to a non-ASCII letter, where the `hf_` twin matches and the text pattern does not. So the
    binary rule can catch more than the text rule, never less.
  - One rule is stricter than text: in a binary, `Bearer` counts only when the value after it is token-shaped. That means
    at least 20 characters of `[A-Za-z0-9._-]`, using at least two of lower case, upper case and digits.
  - So a render whose prompt says `Flame Bearer of the North` comes through byte-identical. A real bearer token (an OAuth
    access token, a JWT, a Hugging Face token) is caught.
  - Text files keep the library's patterns unchanged: the same prose in a text file still becomes `Bearer [REDACTED] the
    North`.
- **A hit is never rewritten.**
  - The file is moved unchanged to quarantine, logged and named in `runner_result.json` (`quarantined`) by path and pattern
    name only.
  - The download is refused with `BinaryDownloadRefused` (`BINARY_DOWNLOAD_TOKEN`). That ends `Runner.run` as `error` (CLI
    exit 2), and the session is still stopped.
- **A directory download** is scrubbed of tokens file by file, and one failure never stops the rest: a hit whose move to quarantine
  fails is removed, and a subdirectory that cannot be listed is noted. At the end, `QuarantineEscape` raises first if triggered;
  otherwise any binary token hit still raises `BinaryDownloadRefused` (`BINARY_DOWNLOAD_TOKEN`, with `failures` and `removed`
  attached) so a quarantine move failure cannot downgrade a token hit into `partial`, and any other `OSError` raises first.
  Only regular files are read. A symlink, to a file or to a directory, is unlinked, never
  followed, and any other entry that is not a regular file or a directory (a FIFO, a socket, a device) is unlinked, never
  read.
- **Where the quarantine is.**
  - Through `Runner` (and so `run`), it is the sibling directory `<out-dir>.quarantine/`. It is outside `--out-dir`, so
    neither `salvage` nor out-dir bundling picks it up.
  - A bare `Colab.download` or `scrub_local` with no `quarantine_dir` moves the file to a new folder
    `colab-runner-quarantine-*` under the system temp dir (`tempfile.gettempdir()`). It never lands next to the file, so
    never inside the folder the file was downloaded into. Its path is in `BinaryDownloadRefused.quarantined`. Pass
    `Colab(quarantine_dir=...)` to choose the place.
  - Every quarantine path is resolved and checked to stay inside its quarantine folder before the move. A file that
    would land outside it is removed instead, and the download fails with `QuarantineEscape` (`QUARANTINE_ESCAPE`).
  - colab-runner never deletes a quarantine. **The calling lane must delete it and must never ship it.**

**Salvage** is a Python-API hook only: `Runner.run(..., salvage=callable)` runs it on every end but `done`, before the stop. The
CLI has no salvage.

**Pricing & three stop reasons.** A running paid window stops for only three reasons: (a) **cap** (`own_rate x elapsed >= ceiling`),
(b) **wall** (`--wall-s`), or (c) **dead job** (`dead` after `dead_confirm` polls, `setup_stall`, `progress_stall`, or
`unreachable`). Mid-run `colab usage` parse failures, missing `Active assignments` counts, rates below catalog floor, and
mid-run rate drift are logged rather than killing the window; `--max-rate` is enforced against the session's rate only at
open (`RATE_ABOVE_MAX`). The account balance is recorded and never prices the window.

## Platform requirement (`PLATFORM_UNSUPPORTED`)

`launch` and `queue run` refuse native Windows (`sys.platform == "win32"`) before opening directories or writing state
files with exit code 1 (`REFUSED: PLATFORM_UNSUPPORTED: colab-runner runs on Linux or WSL`). Run `colab-runner` on Linux
or inside WSL.

## Continuous work engine (`purpose`, `line`, `queue`, `budget`, `board`)

- **Purpose, line of work, and data class in `[project]`:** optional `purpose` (falls back to `description`, and is
  required non-empty for every queued job), `line` (falls back to `study`), and `data_class` (`"public"`, `"synthetic"`,
  `"private"`, or `"sealed"`, defaulting to `"sealed"` when `[inputs.sealed]` is declared and `"private"` otherwise).
  Sealed manifests with `data_class` set to `"public"` or `"synthetic"` are refused (`SEALED_DATA_REFUSED`).
- **Standing budgets (`budget set` / `budget show`):** set and inspect per-line standing CCU caps in `line_ledger.json`.
  Windows book spend idempotently by `window_id` and classify into `done`, `failed-ours`, `failed-Google`, or
  `refused-capacity` (`outcome_bucket`).
- **Unattended queues (`queue validate` / `queue run`):** declare `[[jobs]]` in a TOML/JSON queue manifest with per-job
  `spec`, `purpose`, `line`, `cap`, `max_rate`, and `wall_s`. `queue validate` verifies every spec and enforces single card
  per study (`CARD_MIX_REFUSED`). `queue run` defers jobs whose `cap` would exceed the line's standing budget, respects
  account/card pool ceilings (20 total, 3 per GPU tier), isolates per-job failures (stopping & booking before advancing to
  the next job, halting only if a session stop cannot be confirmed), and writes `queue_state.json`.
- **At-a-glance `board`:** `python -m colab_runner board [--out-root D | --out-dir D ...] [--state-dir S] [--json]`
  shows line budgets, ran/running windows (purpose, card, status, spend, artifacts, HF link), upcoming queued jobs, and
  totals with four-way counts.

## Hugging Face publish (`[publish]`)

When `project.data_class` is `"public"` or `"synthetic"` and `[inputs.sealed]` is absent, a live window ending `done`
stops the Colab session first, then runs `publish_window_outputs`: creates the Hugging Face repo (default org
`mcp-tool-shop`, default `repo_type = "dataset"`) as **private**, verifies it is private, uploads `collected/` artifacts
plus a generated `README.md` card, runs a full identity/token scan over every file and card, flips the repo public, and
records `repo_url` and `flipped_public_at` in `outcome.json`. Scan or upload failure leaves any created repo private and
marks the outcome `partial` (`exit_code = 2`).


