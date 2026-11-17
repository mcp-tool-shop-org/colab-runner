<p align="center">
  <a href="README.md">English</a> | <a href="README.ja.md">日本語</a> | <a href="README.zh.md">中文</a> | <a href="README.es.md">Español</a> | <a href="README.fr.md">Français</a> | <a href="README.hi.md">हिन्दी</a> | <a href="README.it.md">Italiano</a> | <a href="README.pt-BR.md">Português (BR)</a>
</p>

<p align="center">
  <img src="https://raw.githubusercontent.com/mcp-tool-shop-org/brand/main/logos/colab-runner/readme.png" alt="Colab Runner" width="400">
</p>

<p align="center">
  <a href="https://github.com/mcp-tool-shop-org/colab-runner/actions/workflows/ci.yml"><img src="https://github.com/mcp-tool-shop-org/colab-runner/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="MIT License"></a>
  <a href="https://mcp-tool-shop-org.github.io/colab-runner/"><img src="https://img.shields.io/badge/Landing_Page-live-blue" alt="Landing Page"></a>
</p>

# colab-runner

The studio's shared runner for paid Google Colab windows: one place for the code every Colab lane
used to carry its own copy of. It starts a job on a Colab VM in a process that outlives the rig, watches it
inside a spending cap, scrubs tokens from everything it brings back, and always tries to stop the session.

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

## Install

colab-runner is not on PyPI. A lane vendors a pinned release, or installs it from the tag:

```bash
pip install "git+https://github.com/mcp-tool-shop-org/colab-runner@v0.1.0"
```

- **Python** 3.11 or later. The package has no runtime dependencies.
- **The `colab` command line** (Google's Colab CLI), signed in to the account that pays for the window. colab-runner
  calls it; it does not install it or sign in for you. `COLAB_RUNNER_CLI` points at another command line (below).
- **Platforms:** CI runs on Linux with Python 3.11 and 3.12. The studio also runs it from Windows.

## Quick start

The lane opens the session, then hands it to `run`, which ends by stopping it:

```bash
colab new -s my-window            # the lane's own step: open the session (plus the card's flags)
python -m colab_runner run --session my-window --out-dir runs/w1   --script /content/train.sh --wall-s 3600   --max-rate 13.5 --cap 15 --cleanup-bound-s 600
echo "exit $?"                    # 0 done and stopped; see the exit codes below
```

The figures are placeholders. Set `--max-rate` to the card's expected units per hour and `--cap` to the window's
budget in compute units (see Pricing).

`python -m colab_runner --help` lists every command; `run --help` lists every flag.

## Using it from a lane (0.1.0)

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
- `phase.txt`: absent, empty or `setup` while installing, anything else once running. Jobs started with `--hold-s` skip the setup phase.
- `setup.log`: during setup, a run whose `setup.log` stops growing for `--setup-stall-s` (default 600) ends `setup_stall`.
- **Progress:** after setup, a run ends `progress_stall` when nothing it can see moves for `--progress-stall-s` (default 900).
  What it sees is the sum of the first integer in each file matching `CR_PROGRESS_GLOB` (default `<out>/progress/*`), the count
  of files matching `CR_UNITS_GLOB`, and the hold heartbeats.
- A run with no good poll for `--unreachable-polls` polls in a row (default 10), or for `--unreachable-s` (default 1800), ends
  `unreachable`.

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
  fails is removed, and a subdirectory that cannot be listed is noted. The first such error is raised once all the remaining
  files have been scrubbed. Only regular files are read. A symlink, to a file or to a directory, is unlinked, never
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

**Pricing.** The window is priced at `max(--max-rate, the highest stable window rate)` times elapsed, through the vendored
library's rate and cap functions. A stable rate above `--max-rate` trips `RATE_ABOVE_MAX`, so every window that keeps running is
priced at `--max-rate`: it is a floor, not a loose ceiling. Set it to the card's expected rate; a loose `--max-rate` trips the cap
early. The account balance is recorded and never prices the window.

## Security and threat model

**What it touches.**
- The Colab session the lane names: it uploads cell files, starts and kills the job it started, downloads the files
  it was asked for, and stops the session. It never sends a signal to a process that belongs to another run of colab-runner (`CR_NOT_OURS`).
- The out dir (`--out-dir`) and its sibling quarantine `<out-dir>.quarantine/` on the rig. A bare `Colab.download`
  with no quarantine dir uses a new `colab-runner-quarantine-*` folder under the system temp dir.
- The environment variables the lane names with `--env`, which it passes to the VM inside the cell file.

**What it does not touch.** No files outside the out dir, its quarantine and the temp quarantine. No other Colab
session. No account settings, credentials or billing: it reads the account's rate and balance through the `colab`
command line and never changes them.

**Network.** Only through the `colab` command line, to Google Colab. colab-runner opens no connection of its own.

**Secrets.** A secret passed as `--env NAME` stays off every command line. Every output line, error line, traceback
and downloaded text file is scrubbed of tokens; a binary file with a token in it is quarantined unchanged and the
download is refused (above). colab-runner never deletes the quarantine: the calling lane must delete it, and must never ship it.

**Permissions.** It runs as the user who runs it, with the `colab` sign-in that user already has. It needs no
elevation.

**No telemetry.** colab-runner collects and sends nothing.

Reporting a vulnerability: see [SECURITY.md](SECURITY.md).

## What is not here

No bucket names, account names, launch wrappers or local paths. A caller passes those by flag or
environment variable; tests use placeholders.

## Verify

```bash
bash verify.sh
```

## Licence

MIT. See [LICENSE](LICENSE).

---

<p align="center">Built by <a href="https://mcp-tool-shop.github.io/">MCP Tool Shop</a></p>
