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
2. **Watches it inside a cap.** Stops a running window only for cap (`own rate x elapsed`), wall, or a dead/stalled job,
   logging flaky account reads or mid-run rate drift without aborting healthy work.
3. **Scrubs and publishes.** Scrubs every log and downloaded file (quarantining binaries with token hits), and publishes
   `public`/`synthetic` `done` outputs private-first to Hugging Face after an identity scan.
4. **Always tries to stop & book.** Stops sessions on every exit, records spend by `line` and `purpose`, runs unattended
   multi-job queues (`queue run`), and summarizes progress in `board` and `budget`.

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
