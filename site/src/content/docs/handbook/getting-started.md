---
title: Getting started
description: Install colab-runner and run a first Colab window with it.
sidebar:
  order: 1
---

## Requirements

- **Python 3.11 or later.** The package has no runtime dependencies.
- **The `colab` command line** (Google's Colab CLI), signed in to the account that pays for the window. colab-runner
  calls it; it does not install it or sign in for you.
- **Platforms:** CI runs on Linux with Python 3.11 and 3.12. The studio also runs it from Windows.

## Install

colab-runner is not on PyPI. Vendor a pinned release into the lane, or install it from the tag:

```bash
pip install "git+https://github.com/mcp-tool-shop-org/colab-runner@v0.1.4"
python -m colab_runner --version
```

## A first window

The lane opens the session. `run` takes it over and ends by stopping it:

```bash
colab new -s my-window            # the lane's own step (plus the card's flags)
python -m colab_runner run --session my-window --out-dir runs/w1 \
  --script /content/train.sh --wall-s 3600 \
  --max-rate 13.5 --cap 15 --cleanup-bound-s 600
echo "exit $?"
```

The figures are placeholders. Set `--max-rate` to the card's expected units per hour and `--cap` to the window's budget
in compute units.

## Reading the end

The last line on stdout says how the run ended:

```text
RUN_END reason=done rc=0 stopped=True
```

| Exit | What to do |
| --- | --- |
| 0 | Nothing: the job finished and the session is stopped. |
| 1 | A refusal prints a `REFUSED:` line and its hint; a usage error prints `usage: …` and an `error:` line. Nothing was started, so if the lane opened a session, stop it with `colab stop -s S`. |
| 2 | Read `RUN_END` (or `RUN_ERROR:`) and `runner_result.json`. If `session_open.json` is still in the out dir, run `python -m colab_runner stop --out-dir D`. |
| 3 | The job finished, but the stop was not confirmed. Run `python -m colab_runner stop --out-dir D`. |

Next: [Using it from a lane](../lanes/).
