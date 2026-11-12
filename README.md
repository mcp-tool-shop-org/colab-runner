# colab-runner

The studio's shared runner for paid Google Colab windows: one place for the code every Colab lane
used to carry its own copy of.

**Status: scaffold.** Nothing runs yet. What lands here, in order:

1. **The detached runner.** Start a job on the VM with a status file, poll it with backoff, salvage
   whatever finished when a window stops early, a `--hold-s` heartbeat mode, and a flag that switches to a
   kernel-resident loop.
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

## What is not here

No bucket names, account names, launch wrappers or local paths. A caller passes those by flag or
environment variable; tests use placeholders.

## Verify

```bash
bash verify.sh
```

## Licence

MIT. See [LICENSE](LICENSE).
