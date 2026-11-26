# Worked example: `merchant-league` local model seats

This directory shows how a project declares a Colab GPU window once in `colab-window.toml` and runs it through `colab-runner` without writing or editing a per-window shell script.

- Target public repo: `mcp-tool-shop-org/merchant-league` (`seats.json` with `backend: "local"`).
- Card & shape: `G4` (`standard`), capped at `8.0` compute units and `1800` seconds wall-clock time.
- Synthetic inputs only (`seats.json`, `wrapper.md`, `response_schema.json`, `run_seats.py`).

## Plan only (zero `colab` calls)

```bash
python -m colab_runner launch --spec examples/merchant_league_seats/colab-window.toml --out-dir runs/ml-dry --dry
```

Smoke plan preview on T4 (`min_vram_mib_by_card` selects 14000 MiB for T4):

```bash
python -m colab_runner launch --spec examples/merchant_league_seats/colab-window.toml --out-dir runs/smoke-t4-dry --dry --card T4 --shape standard --max-rate 1.5 --wall-s 180 --cap 2.5
```

## Offline CPU dry run (zero `colab` calls)

Runs the full staging, VM cell loop (`start.py`, `poll.py`, `lib.py`), local OpenAI-compatible stub (`POST /tokenize` and `POST /v1/chat/completions`), output copy-back, token scrub, and `outcome.json` booking record locally:

```bash
python -m colab_runner launch --spec examples/merchant_league_seats/colab-window.toml --out-dir runs/ml-cpu --local-cpu
python -m colab_runner status --out-dir runs/ml-cpu
```

## Live GPU smoke (with in-process loopback stub)

Pass `--env ML_SMOKE_STUB=1` to run the in-process loopback stub during `gpu-live` (`CR_*` env names are reserved by `colab-runner` and refused on `--env`):

```bash
python -m colab_runner launch --spec examples/merchant_league_seats/colab-window.toml --out-dir runs/smoke-t4-live --card T4 --shape standard --max-rate 1.5 --wall-s 180 --cap 2.5 --env ML_SMOKE_STUB=1
```
