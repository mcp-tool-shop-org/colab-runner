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

**0.1.4** is the current release. It ships the detached runner (0.1.0) with these fixes since:

- **0.1.1** marks an out-of-memory kill as `oom`, never walks an NTFS junction in a download, and records a cap trip in
  `runner_result.json` with its code and hint.
- **0.1.2** keeps the last known phase when a read of `phase.txt` comes back empty, so a healthy running job is never
  ended as `setup_stall`. A kit should still write `phase.txt` in one step: see [Using it from a lane](./lanes/).
- **0.1.3** makes the tests reap every job they start, which ends an intermittent CI failure on Python 3.12.
- **0.1.4** brings the README, this handbook and the landing page up to date.

The full list is in the [CHANGELOG](https://github.com/mcp-tool-shop-org/colab-runner/blob/main/CHANGELOG.md).

Still to come: boot and card guards, the token-file secrets route, a Hugging Face
Storage Bucket module, and a dated `PLATFORM.md` of what Colab actually does.

## Where to go next

- [Getting started](./getting-started/): install it and run a first window.
- [Using it from a lane](./lanes/): what a lane owns, the flags, the job's side of the contract.
- [Reference](./reference/): every flag, exit code, end reason and error code.
- [Security](./security/): the threat model, the scrub and the quarantine.
