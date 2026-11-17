"""Entry point: `python -m colab_runner --version`, `... run ...` and `... stop --out-dir D`."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
from pathlib import Path
from typing import Optional

from colab_runner import __version__
from colab_runner.colab_cli import cli_from_env, scrub
from colab_runner.runner import Refused, Runner
from colab_runner.spend import SpendGuard

USAGE = (
    "usage: python -m colab_runner --version\n"
    "       python -m colab_runner run --session S --out-dir D (--script P | --hold-s N) --wall-s N --max-rate R "
    "--cap C --cleanup-bound-s N [options]\n"
    "       python -m colab_runner stop --out-dir D   (stops the session a killed run left in D/session_open.json)"
)
ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _run_parser() -> argparse.ArgumentParser:
    p = ScrubbingParser(prog="colab_runner run", description="Start a detached job on a Colab VM and poll it.")
    p.add_argument("--session", required=True)
    p.add_argument("--out-dir", required=True, help="must not exist yet, or be empty")
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
    p.add_argument("--unreachable-polls", type=int, default=10, help="this many failed polls in a row end the run")
    p.add_argument("--unreachable-s", type=float, default=1800.0, help="this long without a good poll ends the run")
    p.add_argument("--max-rate", type=float, help="REQUIRED: the highest rate per hour this window may bill; prices it")
    p.add_argument("--cap", type=float, help="REQUIRED: the window's spend cap, in compute units")
    p.add_argument("--margin", type=float, default=0.0)
    p.add_argument(
        "--cleanup-bound-s", type=float,
        help="REQUIRED: worst-case seconds from a trip to a confirmed stop (kill, salvage, stop); reserved from the cap "
             "by the library's rule, cleanup_reserve = cleanup_bound_s / 3600 x priced rate",
    )
    p.add_argument("--pre-open-rate", type=float, help="the account's usage rate read just before the session opened")
    p.add_argument("--pre-open-assignments", type=int, help="the account's active assignments read just before then")
    p.add_argument(
        "--env", action="append", default=[],
        help="NAME=VALUE, or NAME alone to pass NAME's value from this process's environment, which keeps a secret off "
             "every command line (repeatable). Values reach the VM inside the cell file, never as arguments.",
    )
    return p


def refuse_unbounded(max_rate, cap) -> Optional[tuple[str, str]]:
    """An uncapped window must be impossible to start: no cap, a non-positive or non-finite cap, or a missing or
    non-positive max-rate is refused with a named code before anything runs."""
    if cap is None:
        return "CAP_REQUIRED", "--cap is required: a window without a cap cannot be started"
    if not math.isfinite(cap) or cap <= 0:
        return "CAP_INVALID", "--cap must be a finite number greater than 0 (got %r)" % cap
    if max_rate is None:
        return "MAX_RATE_REQUIRED", "--max-rate is required: it prices the window when no stable rate can be attributed"
    if not math.isfinite(max_rate) or max_rate <= 0:
        return "MAX_RATE_INVALID", "--max-rate must be a finite number greater than 0 (got %r)" % max_rate
    return None


def kit_env(items: list[str]) -> dict:
    """`--env` entries: NAME=VALUE, or NAME for the value of NAME in this process's environment. A malformed entry or
    an unset NAME is refused; a value is never echoed."""
    env = {}
    for item in items:
        name, eq, value = item.partition("=")
        if not ENV_NAME_RE.fullmatch(name):
            raise Refused("ENV_INVALID", "--env needs NAME=VALUE or NAME, with NAME a variable name")
        if not eq:
            if name not in os.environ:
                raise Refused("ENV_UNSET", "--env %s names a variable that is not set in this environment" % name)
            value = os.environ[name]
        env[name] = value
    return env


def _err(line: str) -> None:
    """Every line this CLI writes to stderr (refusals, errors, the runner's log) goes through the library's scrub."""
    print(scrub(line), file=sys.stderr)


def _out(line: str) -> None:
    print(scrub(line))


_log = _err


class ScrubbingParser(argparse.ArgumentParser):
    """argparse, with its usage and error messages scrubbed too: an error echoes the bad value it was given."""

    def _print_message(self, message, file=None):
        super()._print_message(scrub(message) if message else message, file)


def run_command(argv: list[str]) -> int:
    args = _run_parser().parse_args(argv)
    try:
        refusal = refuse_unbounded(args.max_rate, args.cap)
        if refusal:
            raise Refused(*refusal)
        if args.cleanup_bound_s is None:
            raise Refused("CLEANUP_BOUND_REQUIRED", "--cleanup-bound-s is required: the cap reserves the cleanup's cost")
        env = kit_env(args.env)
        guard = SpendGuard(
            max_rate=args.max_rate, cap=args.cap, margin=args.margin, cleanup_bound_s=args.cleanup_bound_s,
            pre_open_rate=args.pre_open_rate, pre_open_assignments=args.pre_open_assignments,
        )
        runner = Runner(cli_from_env(), args.session, Path(args.out_dir), job=args.job, content=args.content,
                        poll_s=args.poll_s, log=_log)
        result = runner.run(
            script=args.script, hold_s=args.hold_s, heartbeat_s=args.heartbeat_s, env=env, wall_s=args.wall_s,
            guard=guard, progress_stall_s=args.progress_stall_s, setup_stall_s=args.setup_stall_s,
            resident=args.resident, unreachable_polls=args.unreachable_polls, unreachable_s=args.unreachable_s,
        )
    except Refused as exc:
        _err("REFUSED: %s: %s" % (exc.code, exc.why))
        return 3
    except Exception as exc:  # the run has stopped its session already; its message is scrubbed like any output
        _err("RUN_ERROR: %s: %s" % (type(exc).__name__, exc))
        return 1
    _out("RUN_END reason=%s rc=%s stopped=%s" % (result.reason, result.rc, result.stopped))
    return 0 if result.reason == "done" and result.stopped else 1


def stop_command(argv: list[str]) -> int:
    """The compensator for a run killed outright: stop the session its `session_open.json` names, with the same
    confirmed-marker rule (the marker goes only after a confirmed stop). Nothing to stop is a success."""
    p = ScrubbingParser(prog="colab_runner stop", description="Stop the session a run left open in D.")
    p.add_argument("--out-dir", required=True)
    args = p.parse_args(argv)
    out_dir = Path(args.out_dir)
    marker = out_dir / "session_open.json"
    if not marker.is_file():
        _out("STOP_NOTHING: no session_open.json in %s" % out_dir)
        return 0
    try:
        session = json.loads(marker.read_text(encoding="utf-8"))["session"]
        if not isinstance(session, str) or not session:
            raise ValueError("no session name")
    except (ValueError, KeyError, TypeError) as exc:
        _err("REFUSED: SESSION_MARKER_INVALID: %s does not name a session (%s)" % (marker, exc))
        return 3
    ok = Runner(cli_from_env(), session, out_dir, log=_log).stop_session()
    _out("STOP_END session=%s confirmed=%s" % (session, ok))
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args in ([], ["--help"], ["-h"]):
        print(USAGE)
        return 0
    if args == ["--version"]:
        print(__version__)
        return 0
    if args[0] == "run":
        return run_command(args[1:])
    if args[0] == "stop":
        return stop_command(args[1:])
    _err("colab-runner: unknown arguments: %s" % " ".join(args))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
