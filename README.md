# colab-runner

The studio's shared runner for paid Google Colab windows: one place for the code every Colab lane
used to carry its own copy of.

**Status: 0.1.0.** The detached runner (item 1 below) is built. Items 2 to 5 are not built yet.

1. **The detached runner (0.1.0).** Start a job on the VM with a status file, poll it with backoff, a `--hold-s` heartbeat mode,
   and a flag that switches to a kernel-resident loop. Salvage is a Python-API hook (below).
2. **Boot and card guards.** A card assert on the VM's first step (compute capability and VRAM per card
   type, refusing `CARD_MISMATCH`), a rig-side refusal log with a per-card circuit breaker
   (`CARD_CIRCUIT_OPEN`), and `OMP_NUM_THREADS` set before torch loads.
3. **Secrets.** The token-file route: a 0600 file, read only by the process that needs it, shredded on the
   VM, scrubbed from every kept log.
4. **A Hugging Face Storage Bucket module.** Sync outputs to a per-window staging prefix, verify counts and
   sha256 against a manifest, then write a `done.json` receipt. It never deletes.
5. **`PLATFORM.md` and `facts.json`.** What the Colab platform actually does, each fact dated and sourced,
   so a kit checks its assumptions against them and a wrong one becomes a refusal code, not a paid surprise.

Lanes vendor a pinned release of this repo, one lane per pull request.

## Using it from a lane (0.1.0)

**The session is the lane's.** The lane opens the Colab session and passes its name. Once `run` has written
`session_open.json` in the out dir, every exit tries to stop the session, and the marker is removed only after a confirmed stop.
- **A marker that stays** means the session may still be open: a stop that failed, or a `run` killed outright (SIGKILL, a lost
  rig) before its cleanup ran. The lane then runs `python -m colab_runner stop --out-dir D`.
- **Before the marker exists** (a refusal, exit 3, or a usage error, exit 2), the lane runs `colab stop -s S` itself.

**Required flags of `run`:** `--session S --out-dir D (--script P | --hold-s N) --wall-s N --max-rate R --cap C
--cleanup-bound-s N`.
- `--out-dir` must not exist yet, or be empty (`OUT_DIR_NOT_EMPTY` otherwise).
- `--cleanup-bound-s` must be at least the runner's own cleanup bound (`CLEANUP_BOUND_TOO_SMALL` otherwise). At the default
  call timeouts that bound is 466 s, or 986 s with `--resident` (`Runner.own_cleanup_bound_s`).
- Pass `--pre-open-rate` and `--pre-open-assignments` together, read just before the session opened, or neither. Without them
  the whole account rate is attributed to this window, so a neighbour session can trip the rate limit.
- Pass a distinct `--job` (default `job`) for each step sharing a `--content` directory. How a reused `--job` is refused:
  - Every run has its own run id, `CR_RUN_ID`: a fresh one, unless the lane passes one with `--env` (an empty one is
    replaced). A lane that passes the same `CR_RUN_ID` to two steps turns this refusal off between them.
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
  - A retried start of the same run (one whose reply was lost) is not refused.
- In `--resident` mode, a non-finite or non-positive `CR_RESIDENT_MAX_S` passed via `--env` is refused up front
  (`RESIDENT_MAX_INVALID`, exit 3).

**Exit codes.** `run`: 0 for `done` with a confirmed stop, 1 for any other end, a failed stop or an error, 2 for a usage error,
3 for a refusal. `stop --out-dir D`: 0 for a confirmed stop or nothing to stop, 1 for a failed stop, 2 for a usage error, 3 for
a marker that names no session.

**How a run ends:** `done`, `failed` (non-zero exit), `oom`, `wall`, `cap`, `setup_stall`, `progress_stall`, `dead`,
`unreachable`, `start_failed`, `interrupted` or `error`.
- Every end but `error` writes `RUN_END reason=… rc=… stopped=…` as the last line on stdout.
- An `error` end writes `RUN_ERROR: …` on stderr and no `RUN_END` line. Exits 2 and 3 write to stderr only.
- `runner_result.json` in the out dir records every end after the marker was written, `error` included.

**What `run` writes in the out dir** (`--job` defaults to `job`, `--content` to `/content`):
- `session_open.json` while the session may be open;
- `runner_result.json`, the run's end (including a `quarantined` entry for each binary download quarantined during the run);
- `<job>.run.out`, the job's log (appears only once the job has written log lines, or once `--resident` has collected it);
- `stop_confirmed.json` after a confirmed stop, or `stop_pending.json` after a failed one.

**The `colab` command** is `colab` unless `COLAB_RUNNER_CLI` names another command line (split with `shlex.split`).

**What the job must write, so the stall guards can see it.** Paths are on the VM; the out dir is `CR_OUT_DIR`, by default
`<content>/<job>_out`.
- `phase.txt`: absent, empty or `setup` while installing, anything else once running. Hold jobs skip the setup phase.
- `setup.log`: during setup, a run whose `setup.log` stops growing for `--setup-stall-s` (default 600) ends `setup_stall`.
- **Progress:** after setup, a run ends `progress_stall` when nothing it can see moves for `--progress-stall-s` (default 900).
  What it sees is the sum of the first integer in each file matching `CR_PROGRESS_GLOB` (default `<out>/progress/*`), the count
  of files matching `CR_UNITS_GLOB`, and the hold heartbeats.
- A run with no good poll for `--unreachable-polls` polls in a row (default 10), or for `--unreachable-s` (default 1800), ends
  `unreachable`.

**Secrets & binary-safe download scrub.** Pass a secret as `--env NAME`, with NAME set in the lane's environment. `--env NAME=VALUE`
puts the value in the runner's own command line. Values reach the VM inside the cell file, never as arguments to the `colab` CLI.
Every CLI output line and error line is scrubbed with the vendored library's `scrub_colab_tokens`. Downloaded files and
directories (including partial transfers) are classified by content only (a NUL byte or failed strict UTF-8 decode is binary):
- Text files are scrubbed in place with `scrub_colab_tokens`.
- Clean binary files are left byte-identical. Unreadable files are removed.
- **The binary rule.** A binary file is scanned with byte twins of the library's token patterns.
  - On UTF-8 the twins match exactly what the text patterns match on the decoded text. Every character Python's `\s` matches
    counts as a space, `\x1c`-`\x1f`, NBSP, NEL and EM SPACE included.
  - The one difference is next to a non-ASCII letter, where the `hf_` twin matches and the text pattern does not: more,
    never less.
  - One rule is stricter than text: in a binary, `Bearer` counts only when the value after it is token-shaped. That means
    at least 20 characters of `[A-Za-z0-9._-]`, using at least two of lower case, upper case and digits.
  - So a render whose prompt says "Flame Bearer of the North" comes through byte-identical. A real bearer token (an OAuth
    access token, a JWT, a Hugging Face token) is caught.
  - Text files keep the library's patterns unchanged: the same prose in a text file still becomes `Bearer [REDACTED] the
    North`.
- **A hit is never rewritten.**
  - The file is moved unchanged to quarantine, logged and named in `runner_result.json` (`quarantined`) by path and pattern
    name only.
  - The download is refused with `BinaryDownloadRefused` (`BINARY_DOWNLOAD_TOKEN`). That ends `Runner.run` as `error` (CLI
    exit 1), and the session is still stopped.
- **Where the quarantine is.**
  - Through `Runner` (and so `run`), it is the sibling directory `<out-dir>.quarantine/`. It is outside `--out-dir`, so
    neither `salvage` nor out-dir bundling picks it up.
  - A bare `Colab.download` or `scrub_local` with no `quarantine_dir` moves the file to a new folder
    `colab-runner-quarantine-*` under the system temp dir (`tempfile.gettempdir()`). It never lands next to the file, so
    never inside the folder the file was downloaded into. Its path is in `BinaryDownloadRefused.quarantined`. Pass
    `Colab(quarantine_dir=...)` to choose the place.
  - The runner never deletes a quarantine; **the lane must delete it and never ship it.**

**Salvage** is a Python-API hook only: `Runner.run(..., salvage=callable)` runs it on every end but `done`, before the stop. The
CLI has no salvage.

**Pricing.** The window is priced at `max(--max-rate, the highest stable window rate)` times elapsed, through the vendored
library's rate and cap functions. A stable rate above `--max-rate` trips `RATE_ABOVE_MAX`, so every window that keeps running is
priced at `--max-rate`: it is a floor, not a loose ceiling. Set it to the card's expected rate; a loose `--max-rate` trips the cap
early. The account balance is recorded and never prices the window.

## What is not here

No bucket names, account names, launch wrappers or local paths. A caller passes those by flag or
environment variable; tests use placeholders.

## Verify

```bash
bash verify.sh
```

## Licence

MIT. See [LICENSE](LICENSE).
