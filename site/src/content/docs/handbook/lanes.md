---
title: Using it from a lane
description: What a lane owns, the flags run needs, the job's side of the contract, and how a run ends.
sidebar:
  order: 2
---

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

Exit codes, refusals, error codes and log levels are on the [Reference](../reference/) page.

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

Secrets, the download scrub and the quarantine are on the [Security](../security/) page.

**Salvage** is a Python-API hook only: `Runner.run(..., salvage=callable)` runs it on every end but `done`, before the stop. The
CLI has no salvage.

**Pricing.** The window is priced at `max(--max-rate, the highest stable window rate)` times elapsed, through the vendored
library's rate and cap functions. A stable rate above `--max-rate` trips `RATE_ABOVE_MAX`, so every window that keeps running is
priced at `--max-rate`: it is a floor, not a loose ceiling. Set it to the card's expected rate; a loose `--max-rate` trips the cap
early. The account balance is recorded and never prices the window.
