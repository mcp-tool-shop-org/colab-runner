# Changelog

## [Unreleased]

- **Streaming directory tars, chunked `.partNNNN` downloads (`outputs.download_parts`, `CR_DOWNLOAD_CHUNK_BYTES`), strict spec keys, shared queue `state_dir`, and `board` removal (`colab_runner/launcher.py`, `colab_runner/manifest.py`, `colab_runner/queue.py`, `colab_runner/__main__.py`, `colab_runner/errors.py`).** Streamed directory archives into `.tar.partNNNN` slices bounded by `CR_DOWNLOAD_CHUNK_BYTES` (default 256 MiB) with per-part SHA-256 verification, `outputs.download_parts` scaling both VM tar timeout and `cleanup_outputs_bound_s`, `OUTPUT_PARTS_EXCEEDED`/`OUTPUT_DOWNLOAD_FAILED` per-output isolation, sibling staging, and partial direct-file cleanup; refused unknown spec keys/tables (`SPEC_INVALID`, naming `[publish]` and `project.data_class` removal); defaulted `QueueSpec` to `launch`'s per-user state dir and `open.lock`; and removed `board`.
- **Remove in-runner Hugging Face publishing, `LineLedger`, and `project.data_class` (`colab_runner/publish.py`, `colab_runner/queue.py`, `colab_runner/manifest.py`, `colab_runner/launcher.py`, `colab_runner/__main__.py`).** Deleted `colab_runner/publish.py`, `[publish]`, `LineLedger` (`line_ledger.json`), and `project.data_class` (`DATA_CLASSES`). `board` (`build_board`) now computes per-line spend and four-way counts (`done`, `failed-ours`, `failed-Google`, `refused-capacity`) directly by scanning `outcome.json` under `--out-root` / `--out-dir`, the runner keeps only `[inputs.sealed]` + `--allow-sealed` vs unsealed, and `docs/reference.md` documents uploading collected outputs via the Hugging Face CLI (`hf repos create <id> --private` / `hf upload`).
- **Continuous work engine (`colab_runner/queue.py`, `colab_runner/launcher.py`, `colab_runner/manifest.py`, `colab_runner/__main__.py`).** Added unattended multi-job queues (`queue validate` / `queue run` enforcing stated purpose, pool ceilings via `launch_window`, failure isolation, and four-way `outcome_bucket` classification into `done`, `failed-ours`, `failed-Google`, `refused-capacity`) and at-a-glance `board` reporting.
- **Three stop reasons and net-negative `window_lib` shrink (`colab_runner/spend.py`, `colab_runner/runner.py`, `colab_runner/launcher.py`, `colab_runner/errors.py`, `colab_runner/window_lib/`).** Removed `PRE_OPEN_READ_FAILED`, post-open `SESSIONS_READ_FAILED` stops, and mid-run `ASSIGNMENTS_UNREADABLE`, `COLAB_USAGE_PARSE_FAILED`, `POST_OPEN_RATE_UNREAD`, `MAX_RATE_BELOW_FLOOR`, and mid-run `RATE_ABOVE_MAX` kill paths so running windows stop only for `cap`, `wall`, or dead/stalled jobs (`RATE_ABOVE_MAX` stays an open-time check); deleted `colab_runner/window_lib/colab_window.sh` and `colab_runner/window_lib/remote_runner.py`, and trimmed `colab_runner/window_lib/launch_guards.py` (removing `scrub_file_in_place` and unused helpers).
- **Refuse native Windows launches and preserve PR #21 follow-ups (`colab_runner/launcher.py`, `colab_runner/colab_cli.py`, `colab_runner/errors.py`, `docs/reference.md`).** Refused native `win32` launches up front (`PLATFORM_UNSUPPORTED`, `"colab-runner runs on Linux or WSL"`), raised `BinaryDownloadRefused` with `.failures`/`.removed` whenever any binary token hit occurred even if quarantine moves fail, skipped missing-output checks when `collect` never ran (such as a resident-log refusal), and documented new flags and workflows in `docs/reference.md` and the handbook.

- **Strict Markdown fence-closer matching in pre-registration parsing (`colab_runner/manifest.py`).** Only lines matching `^ {0,3}` followed by at least as many characters of the opening fence character (and optional whitespace) close a fenced code block; indented `    ``` ` lines inside a fenced block stay treated as content so nested fenced card examples cannot leak into `allowed_cards`.
- **Re-verify moved `.reclaim` guard owner bytes after rename (`colab_runner/launcher.py`).** `_try_acquire_reclaim_guard` saves the owner bytes read during the staleness check, inspects the moved guard's `owner.json` after `os.rename`, and restores a live guard (returning `False` without deleting it) if another contender replaced the stale guard before the rename landed.
- **Test coverage for overrun-kill cleanup clamping (`tests/test_launcher.py`).** Exercises `Runner._end` when a simulated kill overruns its bound so subsequent cleanup downloads receive clamped timeouts or skip cleanly to preserve stop attempts inside `--cap`.
- **Shared job environment helper across live and `--local-cpu` (`colab_runner/launcher.py`, `colab_runner/runner.py`, `colab_runner/vm/lib.py`, `colab_runner/manifest.py`, `colab_runner/cards.py`).** Added `build_job_env` and removed `LocalColab._cell_env` so `--local-cpu` runs the exact rendered job script under `set -euo pipefail` with zero local-only defaults, exported `CR_OUT_DIR`, `CR_PROGRESS_GLOB`, `CR_UNITS_GLOB`, `CR_POLL_EVERY_S`, and `CR_NVIDIA_SMI` via `build_job_env` / `Runner._env` / `cr_start_job`, replaced `${CR_DRY_RUN:-0}` with `"$CR_DRY_RUN"`, and fetched `vm_card.json` in normal collect cleanup as well as salvage.
- **Reserved `CR_*` user env check, persistent `Runner._job_env`, and directory output tar collection (`colab_runner/errors.py`, `colab_runner/colab_cli.py`, `colab_runner/launcher.py`, `colab_runner/runner.py`, `colab_runner/vm/lib.py`, `colab_runner/manifest.py`).** Refused `CR_*` variables in `job.env` and `--env` (`ENV_RESERVED`, including `CR_SMOKE_STUB`; the worked example now uses `--env ML_SMOKE_STUB=1`), persisted `Runner._job_env` across `start`/`poll`/`kill`/`resident`/`collect`/`salvage` cells with relative glob resolution in `cr_glob_files`, archived declared directory outputs to `<out>/<rel>.tar` on the VM (preserving unchanged text line endings), downloaded tars unscrubbed into a private temp directory outside `out_dir` before `tarfile` `filter="data"` extraction and per-file tree `scrub_local` (matching `LocalColab.download` refusing raw directory downloads), recorded per-output errors (`output_errors`, `OUTPUT_TAR_CORRUPT`, `OUTPUT_TAR_UNSAFE`), `missing_outputs`, and `status="partial"` when any declared collect output is missing, required `CR_DRY_RUN` with a colab-runner hint (`${CR_DRY_RUN:?...}`), and tightened `.reclaim` ownerless stale mtime verification via prep-dir rename.
- **Directory output binary-token refusal propagation and scrub error handling (`colab_runner/errors.py`, `colab_runner/launcher.py`).** Propagated `BinaryDownloadRefused` and `QuarantineEscape` from `_extract_dir_tar` onto `runner._download_refusal` (preserving collection of remaining outputs and `missing_outputs` via `prior_reason == "done"`) so real binary token hits in directory outputs end with `status="error"`, exit code `2`, and `refusal.code` matching `README.md`; added `OUTPUT_SCRUB_FAILED` for non-refusal `OSError` failures during directory scrub; and staged extracted directories under `<stage_root>/<bucket>/<rel>` so quarantine relative paths keep their `collected/` or `salvage/` bucket prefix.

## [0.2.0] - 2026-11-26

On-demand Colab GPU windows from a declarative spec: plan, launch, watch, stop, and record without per-window shell wrappers.

- **Declarative window spec and strict input sandbox (`colab_runner/manifest.py`).** Projects declare `[project]`, `[compute]`, `[inputs]`, `[job]`, and `[outputs]` in TOML (`tomllib`) or JSON with zero runtime dependencies. `project.job` must match `[A-Za-z0-9_-]+`, and `compute.min_vram_mib_by_card` supports per-card VM VRAM floors. Inputs are staged explicitly from `inputs.files` into a deterministic tarball; symlinks, NTFS junctions, `..` traversal, missing files, whole-path sealed `inputs.root` directories, paths outside the spec's git repository (or specs with no `.git` repository above them), and sealed/restricted path patterns (including project `inputs.deny_globs` such as `staging*` or holdout names) require a verified `[inputs.sealed]` pre-registration (single-read UTF-8/SHA-256 verification, single unfenced declarations outside fenced code blocks of `study`, `allowed_cards`, `allowed_host`, and exact non-glob `sealed_paths` matched by resolved path against the pre-registration directory, recording resolved paths and per-file digests in `sealed`) and `--allow-sealed` on live launches (`SEALED_DATA_REFUSED`, `SEALED_PREREG_INVALID`, `SEALED_DESTINATION_REFUSED`, `ALLOW_SEALED_REQUIRED`, `INPUT_ROOT_INVALID`, `INPUT_PATH_OUTSIDE_ROOT`, `INPUT_SYMLINK_REFUSED`, `INPUT_FILE_MISSING`, `OUTPUT_PATH_INVALID`).
- **Platform card catalog, shape enforcement, study ledger, and circuit breaker (`colab_runner/cards.py`).** Catalogs Ultimate plan pool ceilings (20 total; 3 each for A100 40 GB `Standard`, A100 80 GB `High-RAM`, G4, L4, and T4; 5 CPU; `H100` unavailable and refused up front with `CARD_UNAVAILABLE`; TPUs `V5E1`/`V6E1` mapped to `--tpu` and `Shape: Standard` matching `google-colab-cli`), enforces per-card catalog rate floors (`MAX_RATE_BELOW_FLOOR`) and machine shapes (`SHAPE_REQUIRED`, `SHAPE_INVALID`, `SHAPE_NOT_FOR_CPU`, `SHAPE_HIGH_MEM_UNAVAILABLE`, `SHAPE_STANDARD_UNAVAILABLE`), reserves study card locks under the open lock before `colab new` and releases the reservation on failed opens only once stop confirms (preserving locks on unstopped failures and legacy pre-0.2.0 entries) (`CARD_MIX_REFUSED`), opens a per-card UTC-day circuit breaker after 2 allocation or confirmed shape/card failures (`CARD_CIRCUIT_OPEN`), and injects a VM-side `nvidia-smi` guard asserting GPU name, per-card VRAM floor, and compute capability (`CARD_MISMATCH`) alongside `OMP_NUM_THREADS`.
- **End-to-end window launcher and booking record (`colab_runner/launcher.py`, `colab_runner/__main__.py`).**
  - `python -m colab_runner launch --spec SPEC --out-dir D [--dry | --local-cpu]`:
    - `--dry` validates the spec, input hashes, sealed pre-registration metadata, card/shape rules, outputs-inclusive cleanup reserve (`vm_card.json`, `salvage`, and `collect` bounds), and `SpendGuard` budget (`worst_case_total_spend <= cap`, reporting launcher death as `unbounded until stop --out-dir` and open-phase duration as a diagnostic bound), writing `dry_run_plan.json` with zero `colab` calls.
    - `--local-cpu` runs the staged inputs and generated VM script against `LocalColab`, exercising `Runner`, VM cells, output download, binary-safe token scrub/quarantine, and `outcome.json` booking offline.
    - Live GPU/TPU launches require explicit `--cap` and `--max-rate` on the CLI, default state and open-lock storage to a persistent per-user state directory, acquire one global cross-process open lock (`OPEN_LOCK_TIMEOUT`, with atomic rename stale-lock and dead `.reclaim`-guard recovery on Windows), require readable `colab sessions` (`SESSIONS_READ_FAILED`) parsed by exact `Hardware:` and `Shape:` columns, enforce pool ceilings (`SESSION_ALREADY_OPEN`, `POOL_CEILING_REACHED`), attribute pre-open and post-open usage rates (`PRE_OPEN_READ_FAILED`, `POST_OPEN_RATE_UNREAD`, `RATE_ABOVE_MAX`), retry transient `colab sessions` reads after open without counting transient reads against the circuit breaker (`SHAPE_MISMATCH`), count elapsed cap and wall time from immediately before `colab new` while clamping open-phase calls/sleeps and cleanup `vm_card.json`/`salvage`/`collect` downloads inside remaining cleanup/cap headroom, keep `SIGINT`/`SIGTERM`/`SIGHUP` armed through open-phase cleanup and the `Runner._arm_signals` handoff, write ledger/breaker updates under the open lock, always attempt `stop_session` on failed opens keeping `session_open.json` until stop confirms, fetch `vm_card.json` even if `outputs.salvage` is empty, and guarantee `outcome.json` (with `sealed`, `spend_ccu` booked up to confirmed stop, and non-pricing `account_balance`) is written on every exit including failed opens, interrupts, and exceptions.
  - `python -m colab_runner status --out-dir D [--json]` inspects dry-run, live, `orphaned` (dead launcher PID with open session), `stop_pending`, and completed windows without touching the VM (`STATUS_DIR_MISSING`).
  - `python -m colab_runner stop --out-dir D` calls `reconcile_stopped_outcome` to re-book `elapsed_s` and `spend_ccu` up to confirmed close time (even when `opened` was false), or synthesizes `outcome.json` from `window_meta.json` (`max_rate_x_elapsed_orphan_reconciled`) when an orphan launcher left none, updating the study ledger under lock.
- **`Colab` CLI and `Runner` additions (`colab_runner/colab_cli.py`, `colab_runner/runner.py`).** Added `Colab.new_session` (`--gpu`, `--tpu`, `--high-mem`), `Colab.upload`, `Colab.sessions`, and exact-column `parse_sessions_output`. `Runner` adds optional `pre_opened`, `collect` (with `collect_confirmed.json` and binary-safe quarantine preservation), and `opened_monotonic` (so the spend guard charges from `colab new`), arms `SIGHUP` alongside `SIGINT`/`SIGTERM`, and preserves `0.1.4`'s `OUT_DIR_NOT_EMPTY` refusal when `out_dir` is an existing file with `pre_opened=False`.
- **Worked example (`examples/merchant_league_seats/`).** Demonstrates `mcp-tool-shop-org/merchant-league` synthetic local OpenAI-compatible model seats with per-card `min_vram_mib_by_card` (`G4`, `L4`, `T4`), an in-process `CR_SMOKE_STUB=1` mode for `gpu-live`, and passing `--dry` and `--local-cpu` checks.

## [0.1.4] - 2026-11-19

Docs release: the README, handbook and landing page caught up with 0.1.1 to 0.1.3. No code changes.

- **Install lines pin `v0.1.4`.** `README.md`, the 7 translated READMEs, `handbook/getting-started.md` and the landing page
  had pinned `v0.1.0`, which predates the 0.1.2 `phase.txt` fix.
- **Status shows 0.1.4.** The README status line and the "Using it from a lane" heading in all 8 READMEs (the version
  number only; no translated sentence changed), the landing-page badge, and the handbook's status section, which now
  summarizes 0.1.1 to 0.1.4 and links here.

## [0.1.3] - 2026-11-19

Test-hygiene and PR #7 review follow-ups; no changes to the runner or VM cells.

- **Reap detached test jobs before temp-dir cleanup.** `tests/test_vm_cells.py` (`VmCellTest`, `EndToEndTest`) now waits briefly for every detached job it starts, terminates lingering process groups (`SIGTERM`, then `SIGKILL`), reaps direct children, and asserts all started processes and groups are gone before `TemporaryDirectory.cleanup()` runs — eliminating the `test_a_stale_pid_without_status_is_restarted` `Directory not empty` race on Python 3.12 and replacing `pkill -f` in `test_resident_cell_gives_up_at_its_max`. `tests/test_library.py` (`ProcessGroupTest`) adds the same process-group cleanup and exit assertion for detached grandchildren.
- **Write `phase.txt` atomically in the tests.** `tests/test_vm_cells.py` uses `os.replace` (`test_poll_reports_phase_progress_units_setup_bytes_and_log_chunks`) and `printf 'run\n' > "$CR_OUT_DIR/phase.tmp" && mv …` (`test_lost_start_reply_and_lost_polls_still_complete_with_one_job`), matching the README.
- **Torn `phase.txt` regression coverage.** `tests/test_torn_phase.py` adds `test_hold_mode_torn_read_preserves_setup_state_and_progress_clock` (torn reads under `hold_mode` change neither setup state nor the progress-stall clock) and `test_torn_read_during_run_does_not_reset_progress_clock` (a transient torn `""` or `None` read during `run` never resets or extends the progress-stall window).

## [0.1.2] - 2026-11-18

A torn `phase.txt` read after setup never ends a healthy running job as `setup_stall`.

- **Never fall back to setup on a torn `phase.txt` read.** Once the job has left setup, an empty (`""`) or missing (`None`) `phase` read in `Runner._loop` keeps the last known phase (`last_phase`) instead of re-entering setup. An empty or missing read during setup keeps setup unchanged.
- **Reset the setup clock on any real return to setup.** If `phase.txt` genuinely reads `"setup"` again after a run phase (`not last_phase_setup`), `Runner._loop` resets `last_setup` and `setup_t` at that transition, so a stale setup clock can never fire on its own.
- **Docs and translations.** `README.md`, the kit handbook (`site/src/content/docs/handbook/lanes.md`), and the 7 translated READMEs (`README.es.md`, `README.fr.md`, `README.hi.md`, `README.it.md`, `README.ja.md`, `README.pt-BR.md`, `README.zh.md`, with `.polyglot-pins.json` updated for the 5 pinnable languages) document the phase rule and add shell (`printf … > phase.tmp && mv …`) and Python (`os.replace`) atomic-write snippets for `phase.txt`.

## [0.1.1] - 2026-11-17

The follow-ups the reviews of PRs #4 and #5 left for after 0.1.0. Exit codes and `detail` are as they were.

- **An OOM kill line makes a `--resident` run `oom` whatever the rc, in the cleanup fallback too.** When the job's run log is first collected in cleanup (less than `MIN_CALL_S`, one second, was left in the window, or the in-loop download failed), a run that had ended `failed` on a non-zero rc other than 137 now ends `oom` if the log has an out-of-memory kill line, as one that had ended `done` already did. One with no such line stays `failed`.
- **NTFS junctions in a download are unlinked, never walked, on every supported Python (3.11 included).** A junction inside a downloaded directory, as the download's root, or broken, is removed like a symlink, with `_unlink_link`: the junction only, never its target or anything in it. It is found with `os.path.isjunction` (Python 3.12 and later), and on 3.11 on Windows by its mount-point reparse tag (`os.lstat(p).st_reparse_tag == IO_REPARSE_TAG_MOUNT_POINT`). Only that tag counts: any other reparse kind (a deduplicated file, a cloud placeholder) is read and scrubbed as a regular file, and a symlink is still found by `is_symlink()` first.
- **A `QUARANTINE_ESCAPE` while the run log is collected in the window marks the log collected**, as `BINARY_DOWNLOAD_TOKEN` already did, so cleanup never downloads `<job>.run.out` a second time.
- **A `cap` end records its trip in `runner_result.json` as `cap_trip: {code, message, hint, retryable}`**, and prints the hint on stderr after `RUN_END`. The code is the trip string's leading `CODE:` (`CAP_GUARD_TRIPPED`, `COLAB_USAGE_PARSE_FAILED`, `RATE_ABOVE_MAX` or `ASSIGNMENTS_UNREADABLE`), or `CAP_CEILING_REACHED`, a code new in 0.1.1, when the clock reached the ceiling before the guard tripped (its `detail` stays `the cap was reached`, byte for byte as in 0.1.0); a trip string with no leading code (a stand-in guard's) has a null code. Every other end has `cap_trip: null`. The hints are in `colab_runner/errors.py` (`CODES`); `ASSIGNMENTS_UNREADABLE` and `COLAB_USAGE_PARSE_FAILED` are retryable.
- **The `RunResult.refusal` comment names `QUARANTINE_ESCAPE`**, beside `JOB_ALREADY_EXISTS` and `BINARY_DOWNLOAD_TOKEN`.

## [0.1.0] - 2026-11-17

The first release for lanes to vendor. The v0.1.0 tag goes on the merge of PR #5, and carries PR #1 (the detached runner,
merged as b3703db0), PR #2 (this version and changelog), PR #3 (the binary-safe download scrub, the `--job` refusal and the
0.1.0 follow-ups), PR #4 (the ship gate: structured errors, the org's exit codes, log levels, pip-audit in CI, and the
reviews' follow-ups) and PR #5 (the full treatment: the logo, the landing page and handbook, the threat model,
SECURITY.md and the filled ship gate). How a lane calls it is in the README, "Using it from a lane".

- **The detached runner.** `python -m colab_runner run` starts a job on a Colab session the lane has already opened, in a process
  that keeps running on the VM if the rig-side process dies. It polls with backoff and follows the job's log (`<job>.run.out`,
  which appears once the job writes log lines). Each step sharing `--content` needs a distinct `--job`. A run ends as one of: `done`, `failed` (the job exited non-zero), `oom`, `wall`, `cap`, `setup_stall`, `progress_stall`, `dead`,
  `unreachable` (a sustained poll blackout), `start_failed`, `interrupted` (SIGINT/SIGTERM) or `error`. There is a hold job
  (`--hold-s`), and a kernel-resident fallback (`--resident`, using `math.ceil(left)` for its default `CR_RESIDENT_MAX_S`) that
  polls the job's `<job>.poll.json` by download and collects the job's log when the job has ended, or at the end of a run that
  tripped. `COLAB_RUNNER_CLI` is split with `shlex.split`, POSIX quoting (on Windows, forward slashes or a quoted path);
  one that cannot be split is refused (`CLI_INVALID`).
- **The `--job` refusal, by run id.**
  - Every run has its own id, `CR_RUN_ID`, a fresh one each time; every cell carries it. The name is reserved: a lane's
    `--env CR_RUN_ID=…` is refused (`RUN_ID_RESERVED`), and so is `CR_RUN_ID` in `Runner.run(env=...)`. A record's run id
    is compared as a string, so a JSON `null` never matches. `Runner.start`, called directly, strips the id it is given
    and makes a fresh one for a blank or missing (`None`) id.
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
    `RUN_ERROR: CODE: message` (`BINARY_DOWNLOAD_TOKEN`, `QUARANTINE_ESCAPE`, or `UNEXPECTED_ERROR` with the exception's
    type), then `hint: …`, and the stop hint too when `session_open.json` is still there; `stop` prints `STOP_ERROR: …`
    the same way. Every line is scrubbed.
  - A run that ends on a named refusal after the marker (`JOB_ALREADY_EXISTS`, `BINARY_DOWNLOAD_TOKEN`,
    `QUARANTINE_ESCAPE`) records `refusal: {code, message, hint, retryable}` in `runner_result.json`. A
    `JOB_ALREADY_EXISTS` end prints `RUN_END … code=JOB_ALREADY_EXISTS` and exits 2. The two download refusals end
    `error`, which prints `RUN_ERROR:` and no `RUN_END`.
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
  - an out dir that is a filesystem root, or that cannot be created or listed (`OUT_DIR_INVALID`);
  - a `COLAB_RUNNER_CLI` that cannot be split (`CLI_INVALID`).

  A missing `--wall-s` is a usage error (exit 1, from argparse), not a named refusal.
- **Tokens and binary-safe download scrub.** Every `colab` CLI output and error line is scrubbed with the library's
  `scrub_colab_tokens`. Downloaded files and directories (including partial, failed, or interrupted transfers) decide text vs
  binary by content (NUL byte or failed strict UTF-8 decode).
  - Text files are scrubbed in place with `scrub_colab_tokens`. Clean binaries are left byte-identical. Unreadable files are
    removed.
  - A downloaded symlink, to a file or to a directory, is unlinked, never followed (the vendored `scrub_file_in_place`
    followed it and rewrote its target). Only regular files are read: a FIFO, a socket or a device is unlinked, never read.
  - A directory is scrubbed file by file, and one failure never stops the rest: a hit whose move to quarantine fails is
    removed, and a subdirectory that cannot be listed is noted; the first such error is raised once the other files are
    scrubbed.
  - A binary is scanned with `BYTE_TOKEN_PATTERNS`, byte twins derived from the sources of `TOKEN_SCRUB_PATTERNS`. On UTF-8
    they match exactly what the text patterns match, with every str `\s` character as a space (`\x1c`-`\x1f`, NBSP, NEL, EM
    SPACE and the rest); next to a non-ASCII letter the `hf_` twin matches more, never less. The derivation refuses what it
    cannot translate span for span: a negated class not quantified with a greedy `+` or `*` (a reluctant `+?` or `*?`
    included), an unescaped `.`, `\S`/`\w`/`\d` and their negations, a non-ASCII class member, and a non-ASCII literal.
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
    system temp dir, never next to the file. Every quarantine path is resolved and checked to stay inside its folder
    before the move; a file that would land outside it is removed instead (`QuarantineEscape`, `QUARANTINE_ESCAPE`). The
    lane must delete a quarantine and never ship it.
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
