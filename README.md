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

**The session is the lane's.** The lane opens the Colab session and passes its name. `run` stops it on every exit once it has
written `session_open.json`. Before that (a refusal, exit 3, or a usage error, exit 2), the lane must stop it itself.

**Required flags of `run`:** `--session S --out-dir D (--script P | --hold-s N) --wall-s N --max-rate R --cap C
--cleanup-bound-s N`.
- `--out-dir` must not exist yet, or be empty (`OUT_DIR_NOT_EMPTY` otherwise).
- `--cleanup-bound-s` must be at least the runner's own cleanup bound (`CLEANUP_BOUND_TOO_SMALL` otherwise). At the default
  call timeouts that bound is 466 s, or 986 s with `--resident` (`Runner.own_cleanup_bound_s`).
- Pass `--pre-open-rate` and `--pre-open-assignments` together, read just before the session opened, or neither. Without them
  the whole account rate is attributed to this window, so a neighbour session can trip the rate limit.

**Exit codes.** `run`: 0 for `done` with a confirmed stop, 1 for any other end, a failed stop or an error, 2 for a usage error,
3 for a refusal. `stop --out-dir D`: 0 for a confirmed stop or nothing to stop, 1 for a failed stop, 3 for a marker that names
no session.

**How a run ends:** `done`, `failed` (non-zero exit), `oom`, `wall`, `cap`, `setup_stall`, `progress_stall`, `dead`,
`unreachable`, `start_failed`, `interrupted` or `error`. The last line on stdout is `RUN_END reason=… rc=… stopped=…`.

**What the job must write, so the stall guards can see it.** Paths are on the VM; the out dir is `CR_OUT_DIR`, by default
`<content>/<job>_out`.
- `phase.txt`: absent, empty or `setup` while installing, anything else once running. Hold jobs skip the setup phase.
- `setup.log`: during setup, a run whose `setup.log` stops growing for `--setup-stall-s` (default 600) ends `setup_stall`.
- **Progress:** after setup, a run ends `progress_stall` when nothing it can see moves for `--progress-stall-s` (default 900).
  What it sees is the sum of the first integer in each file matching `CR_PROGRESS_GLOB` (default `<out>/progress/*`), the count
  of files matching `CR_UNITS_GLOB`, and the hold heartbeats.
- A run with no good poll for `--unreachable-polls` polls in a row (default 10), or for `--unreachable-s` (default 1800), ends
  `unreachable`.

**Secrets.** Pass a secret as `--env NAME`, with NAME set in the lane's environment. `--env NAME=VALUE` puts the value in the
runner's own command line. Values reach the VM inside the cell file, never as arguments to the `colab` CLI. Every CLI output
line, error line and downloaded file is scrubbed with the vendored library's `scrub_colab_tokens`.

**Salvage** is a Python-API hook only: `Runner.run(..., salvage=callable)` runs it on every end but `done`, before the stop. The
CLI has no salvage.

**Pricing.** The window is priced at its own rate times elapsed, through the vendored library's rate and cap functions. A
window that cannot be priced is priced at `--max-rate`. The account balance is recorded and never prices the window.

## What is not here

No bucket names, account names, launch wrappers or local paths. A caller passes those by flag or
environment variable; tests use placeholders.

## Verify

```bash
bash verify.sh
```

## Licence

MIT. See [LICENSE](LICENSE).
