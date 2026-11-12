"""Entry point: `python -m colab_runner --version` and `python -m colab_runner run ...`."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from colab_runner import __version__
from colab_runner.colab_cli import cli_from_env
from colab_runner.runner import Runner
from colab_runner.spend import SpendGuard


def _run_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="colab_runner run", description="Start a detached job on a Colab VM and poll it.")
    p.add_argument("--session", required=True)
    p.add_argument("--out-dir", required=True)
    p.add_argument("--job", default="job")
    p.add_argument("--content", default="/content")
    what = p.add_mutually_exclusive_group(required=True)
    what.add_argument("--script", help="bash script path on the VM")
    what.add_argument("--hold-s", type=float, help="heartbeat-only job for this many seconds")
    p.add_argument("--heartbeat-s", type=float, default=30.0)
    p.add_argument("--resident", action="store_true", help="kernel-resident fallback: the start cell stays busy")
    p.add_argument("--wall-s", type=float, required=True)
    p.add_argument("--poll-s", type=float, default=60.0)
    p.add_argument("--progress-stall-s", type=float, default=900.0)
    p.add_argument("--setup-stall-s", type=float, default=600.0)
    p.add_argument("--rate", type=float, help="this window's priced rate per hour (with --cap)")
    p.add_argument("--cap", type=float)
    p.add_argument("--margin", type=float, default=0.0)
    p.add_argument("--env", action="append", default=[], help="K=V passed to the VM cells (repeatable)")
    return p


def run_command(argv: list[str]) -> int:
    args = _run_parser().parse_args(argv)
    env = dict(kv.split("=", 1) for kv in args.env if "=" in kv)
    guard = SpendGuard(args.rate, args.cap, args.margin) if args.rate is not None and args.cap is not None else None
    runner = Runner(
        cli_from_env(), args.session, Path(args.out_dir), job=args.job, content=args.content,
        poll_s=args.poll_s, log=lambda s: print(s, file=sys.stderr),
    )
    result = runner.run(
        script=args.script, hold_s=args.hold_s, heartbeat_s=args.heartbeat_s, env=env, wall_s=args.wall_s,
        cap_check=guard.check if guard else None, progress_stall_s=args.progress_stall_s,
        setup_stall_s=args.setup_stall_s, resident=args.resident,
    )
    print("RUN_END reason=%s rc=%s" % (result.reason, result.rc))
    return 0 if result.reason == "done" and result.rc == 0 else 1


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args in ([], ["--help"], ["-h"]):
        print("usage: python -m colab_runner --version | run --session S --out-dir D (--script P | --hold-s N) --wall-s N")
        return 0
    if args == ["--version"]:
        print(__version__)
        return 0
    if args[0] == "run":
        return run_command(args[1:])
    print(f"colab-runner: unknown arguments: {' '.join(args)}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
