---
title: Colab Runner Handbook
description: How the studio runs paid Google Colab windows that always stop, inside a spending cap.
sidebar:
  order: 0
---

**colab-runner** is the studio's shared runner for paid Google Colab windows. Every Colab lane used to carry its own copy
of this code. Now there is one place for it, and lanes vendor a pinned release.

It does four things:

1. **Starts the job detached.** The job runs on the VM in a process that keeps going if the rig-side process dies, with
   a status file and a log the runner follows.
2. **Watches it inside a cap.** It polls with backoff, prices the window from the card's rate, and ends the run at the
   wall, at the cap, or when the job stalls, dies or goes unreachable.
3. **Scrubs what comes back.** Every log line and downloaded text file is scrubbed of tokens. A binary file holding a
   token is quarantined unchanged, and the download is refused.
4. **Always tries to stop the session.** Once it owns the session it stops it on every exit, and it leaves a marker
   until the stop is confirmed, so a lane can always finish the job with `python -m colab_runner stop`.

## Status

**0.2.0** is the current release. It adds declarative on-demand windows (`launch`, `status`, `outcome.json`), card/shape/circuit-breaker guards, strict input staging with sealed-data checks, and an offline `--local-cpu` simulation mode on top of the detached runner:

- **0.2.0** adds `colab_runner.cards`, `colab_runner.manifest`, `colab_runner.launcher`, `launch` (`--dry` and `--local-cpu`), `status`, VM GPU card checks, and `outcome.json` spend records.
- **0.1.4** brought the README, handbook, and landing page up to date.
- **0.1.3** made tests reap every detached job before temporary directory cleanup.
- **0.1.2** kept the last known phase on torn `phase.txt` reads so healthy jobs do not end as `setup_stall`.
- **0.1.1** marked out-of-memory kills as `oom`, unlinked NTFS junctions, and recorded structured cap trips.

The full list is in the [CHANGELOG](https://github.com/mcp-tool-shop-org/colab-runner/blob/main/CHANGELOG.md).

Still to come: VM token-file secrets delivery, storage bucket sync receipts, and a dated `PLATFORM.md`.

## Where to go next

- [Getting started](./getting-started/): install it and run a first window.
- [Using it from a lane](./lanes/): what a lane owns, the flags, the job's side of the contract.
- [Reference](./reference/): every flag, exit code, end reason and error code.
- [Security](./security/): the threat model, the scrub and the quarantine.
