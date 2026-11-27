"""Entry point: `python -m colab_runner --version`, `... run ...` and `... stop --out-dir D`.

Exit codes (the org convention):
  0  ok: `run` ended `done` and its stop was confirmed; `stop` confirmed a stop, or found nothing to stop
  1  user error: a refusal by name (`REFUSED: CODE: message`, then `hint: ...`) or a usage error. Nothing was started
     and no session marker was written, so a session the lane opened is the lane's to stop
  2  runtime error: a run that ended other than `done`, an error during the run (`RUN_ERROR: CODE: message`, then
     `hint: ...`), or a stop that was not confirmed (`stop`)
  3  partial success: `run` ended `done`, but its stop was not confirmed (`session_open.json` is still in the out
     dir). The job's work is there, and `python -m colab_runner stop --out-dir D` must still stop the session

Log levels (`run` and `stop`): `--quiet` prints only the end lines (RUN_END / STOP_END on stdout, a refusal or an error
and its hint on stderr); normal (the default) adds the runner's log on stderr; `--verbose` adds one line per poll;
`--debug` adds the traceback of an error. Every line, at every level, goes through the library's token scrub."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import traceback
from pathlib import Path
from typing import Callable, Optional

from colab_runner import __version__
from colab_runner.cards import resolve_card
from colab_runner.colab_cli import cli_from_env, scrub
from colab_runner.errors import CODES, Refused, error_record, lookup
from colab_runner.launcher import (
    launch_window,
    reconcile_stopped_outcome,
    status_window,
)
from colab_runner.manifest import WindowSpec
from colab_runner.queue import QueueSpec, run_queue
from colab_runner.runner import RunInterrupted, Runner
from colab_runner.spend import SpendGuard

EXIT_OK, EXIT_USER, EXIT_RUNTIME, EXIT_PARTIAL = 0, 1, 2, 3
QUIET, NORMAL, VERBOSE, DEBUG = 0, 1, 2, 3

USAGE = (
    "usage: python -m colab_runner --version\n"
    "       python -m colab_runner launch --spec SPEC --out-dir D [--dry | --local-cpu] [options] "
    "[--quiet | --verbose | --debug]\n"
    "       python -m colab_runner queue (validate | run) --queue SPEC [--dry | --local-cpu] [options]\n"
    "       python -m colab_runner status --out-dir D [--json] [--quiet | --verbose | --debug]\n"
    "       python -m colab_runner run --session S --out-dir D (--script P | --hold-s N) --wall-s N --max-rate R "
    "--cap C --cleanup-bound-s N [options] [--quiet | --verbose | --debug]\n"
    "       python -m colab_runner stop --out-dir D [--quiet | --verbose | --debug]   (stops the session a killed run "
    "left in D/session_open.json)\n"
    "exit codes: 0 ok, 1 refusal or usage error, 2 runtime error, 3 done but the session's stop not confirmed"
)
ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class ScrubbingParser(argparse.ArgumentParser):
    """argparse, with its usage and error messages scrubbed too (an error echoes the bad value it was given), and
    a usage error exiting 1 (the org's user-error code), not argparse's 2."""

    def _print_message(self, message, file=None):
        super()._print_message(scrub(message) if message else message, file)

    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(EXIT_USER, "%s: error: %s\n" % (self.prog, message))


def _add_levels(p: argparse.ArgumentParser) -> None:
    g = p.add_mutually_exclusive_group()
    g.add_argument("--quiet", dest="level", action="store_const", const=QUIET, help="only the end lines")
    g.add_argument("--verbose", dest="level", action="store_const", const=VERBOSE, help="also one line per poll")
    g.add_argument("--debug", dest="level", action="store_const", const=DEBUG,
                   help="also the traceback of an error (scrubbed)")
    p.set_defaults(level=NORMAL)


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
             "every command line (repeatable). Values reach the VM inside the cell file, never as arguments. "
             "CR_RUN_ID is reserved (RUN_ID_RESERVED).",
    )
    _add_levels(p)
    return p


def refuse_unbounded(max_rate, cap) -> Optional[Refused]:
    """An uncapped window must be impossible to start: no cap, a non-positive or non-finite cap, or a missing or
    non-positive max-rate is refused with a named code before anything runs."""
    if cap is None:
        return Refused("CAP_REQUIRED", "--cap is required: a window without a cap cannot be started")
    if not math.isfinite(cap) or cap <= 0:
        return Refused("CAP_INVALID", "--cap must be a finite number greater than 0 (got %r)" % cap)
    if max_rate is None:
        return Refused("MAX_RATE_REQUIRED", "--max-rate is required: it prices the window when no stable rate can be "
                       "attributed")
    if not math.isfinite(max_rate) or max_rate <= 0:
        return Refused("MAX_RATE_INVALID", "--max-rate must be a finite number greater than 0 (got %r)" % max_rate)
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
    """Every line this CLI writes to stderr (refusals, errors, hints, the runner's log) goes through the scrub."""
    print(scrub(line), file=sys.stderr)


def _out(line: str) -> None:
    print(scrub(line))


def _quiet(line: str) -> None:
    pass


def _log_for(level: int) -> Callable[[str], None]:
    return _err if level >= NORMAL else _quiet


def _trace_for(level: int) -> Callable[[str], None]:
    return _err if level >= VERBOSE else _quiet


def _refused(exc: Refused) -> int:
    _err("REFUSED: %s: %s" % (exc.code, exc.message))
    _err("hint: %s" % exc.hint)
    return EXIT_USER


def _error(tag: str, exc: BaseException, level: int) -> int:
    """`TAG: CODE: message` and `hint: ...` for an error during a run or a stop, scrubbed; the traceback, scrubbed
    too, only at --debug. An exception with a named code (`BinaryDownloadRefused`, `QuarantineEscape`) keeps it;
    any other is UNEXPECTED_ERROR, with its type in the message."""
    code = getattr(exc, "code", None)
    if isinstance(code, str) and code in CODES:
        rec = error_record(code, str(getattr(exc, "message", exc)))
    else:
        rec = error_record("UNEXPECTED_ERROR", "%s: %s" % (type(exc).__name__, exc))
    if level >= DEBUG:
        for chunk in traceback.format_exception(exc):
            for line in chunk.rstrip("\n").splitlines():
                _err(line)
    _err("%s: %s: %s" % (tag, rec["code"], rec["message"]))
    _err("hint: %s" % rec["hint"])
    return EXIT_RUNTIME


def run_exit_code(result, out_dir: Path) -> int:
    """0 for `done` with a confirmed stop; 3 for `done` whose stop was not confirmed (the marker is still there);
    2 for any other end, whatever its stop did."""
    if result.reason != "done":
        return EXIT_RUNTIME
    if result.stopped and not (out_dir / "session_open.json").exists():
        return EXIT_OK
    return EXIT_PARTIAL


def run_command(argv: list[str]) -> int:
    args = _run_parser().parse_args(argv)
    try:
        refusal = refuse_unbounded(args.max_rate, args.cap)
        if refusal:
            raise refusal
        if args.cleanup_bound_s is None:
            raise Refused("CLEANUP_BOUND_REQUIRED", "--cleanup-bound-s is required: the cap reserves the cleanup's cost")
        env = kit_env(args.env)
        guard = SpendGuard(
            max_rate=args.max_rate, cap=args.cap, margin=args.margin, cleanup_bound_s=args.cleanup_bound_s,
            pre_open_rate=args.pre_open_rate, pre_open_assignments=args.pre_open_assignments,
            log=_log_for(args.level),
        )
        runner = Runner(cli_from_env(), args.session, Path(args.out_dir), job=args.job, content=args.content,
                        poll_s=args.poll_s, log=_log_for(args.level), trace=_trace_for(args.level))
        result = runner.run(
            script=args.script, hold_s=args.hold_s, heartbeat_s=args.heartbeat_s, env=env, wall_s=args.wall_s,
            guard=guard, progress_stall_s=args.progress_stall_s, setup_stall_s=args.setup_stall_s,
            resident=args.resident, unreachable_polls=args.unreachable_polls, unreachable_s=args.unreachable_s,
        )
    except Refused as exc:
        return _refused(exc)
    except (Exception, KeyboardInterrupt) as exc:  # the run has stopped its session already (or left the marker)
        exit_code = _error("RUN_ERROR", exc, args.level)
        if (Path(args.out_dir) / "session_open.json").is_file():  # the run's stop was not confirmed
            _err("hint: %s" % lookup("STOP_NOT_CONFIRMED").hint)
        return exit_code
    code = (" code=%s" % result.refusal["code"]) if result.refusal else ""
    _out("RUN_END reason=%s rc=%s stopped=%s%s" % (result.reason, result.rc, result.stopped, code))
    if result.refusal:
        _err("hint: %s" % result.refusal["hint"])
    if result.cap_trip:  # a cap end: what to change, from runner_result.json's cap_trip
        _err("hint: %s" % result.cap_trip["hint"])
    if not result.stopped:
        _err("hint: %s" % lookup("STOP_NOT_CONFIRMED").hint)
    return run_exit_code(result, Path(args.out_dir))


def _launch_parser() -> argparse.ArgumentParser:
    p = ScrubbingParser(
        prog="colab_runner launch",
        description="Launch, watch, stop, and book a Colab window from a declarative spec.",
    )
    p.add_argument("--spec", required=True, help="window spec (.toml or .json)")
    p.add_argument("--out-dir", required=True, help="must not exist yet, or be empty")
    p.add_argument("--card", help="override compute.card (CPU, T4, L4, G4, A100)")
    p.add_argument("--shape", help="override compute.shape (standard or high-mem)")
    p.add_argument("--session", help="explicit Colab session name")
    p.add_argument("--cap", type=float, help="override compute.cap")
    p.add_argument("--max-rate", type=float, help="override compute.max_rate")
    p.add_argument("--wall-s", type=float, help="override compute.wall_s")
    p.add_argument("--cleanup-bound-s", type=float, help="override compute.cleanup_bound_s")
    p.add_argument("--margin", type=float, help="override compute.margin")
    p.add_argument("--resident", action="store_true", default=None, help="override compute.resident")
    p.add_argument("--env", action="append", default=[], help="NAME=VALUE or NAME from caller environment")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--dry", action="store_true", help="validate spec, inputs, and budget; make zero colab calls")
    mode.add_argument("--local-cpu", action="store_true",
                      help="run CPU stub end-to-end against an isolated local VM directory; zero colab calls")
    p.add_argument("--allow-sealed", action="store_true",
                   help="confirm a live launch carrying pre-registered sealed or outside-repo inputs")
    p.add_argument("--state-dir", help="directory storing per-card UTC circuit-breaker state and study ledger")
    p.add_argument("--study-ledger", help="path to study card-consistency ledger JSON")
    p.add_argument("--open-lock", help="path to cross-process open lock file")
    p.add_argument("--purpose", help="stated purpose for this window (overrides project.purpose)")
    p.add_argument("--line", help="line label for spend tracking (overrides project.line)")
    _add_levels(p)
    return p


def launch_command(argv: list[str]) -> int:
    args = _launch_parser().parse_args(argv)
    out_dir = Path(args.out_dir)
    try:
        if not args.dry and not args.local_cpu:
            spec = WindowSpec.from_file(args.spec)
            card_spec = resolve_card(args.card or spec.card)
            if card_spec.card_class != "cpu":
                if args.cap is None:
                    raise Refused("CAP_REQUIRED", "live %s launch requires explicit --cap C on the command line" % card_spec.name)
                if args.max_rate is None:
                    raise Refused(
                        "MAX_RATE_REQUIRED",
                        "live %s launch requires explicit --max-rate R on the command line" % card_spec.name,
                    )
        client = None if (args.dry or args.local_cpu) else cli_from_env()
        res = launch_window(
            args.spec,
            out_dir,
            card=args.card,
            shape=args.shape,
            session=args.session,
            cap=args.cap,
            max_rate=args.max_rate,
            wall_s=args.wall_s,
            cleanup_bound_s=args.cleanup_bound_s,
            margin=args.margin,
            resident=args.resident,
            extra_env=args.env,
            dry=args.dry,
            local_cpu=args.local_cpu,
            allow_sealed=args.allow_sealed,
            state_dir=args.state_dir,
            study_ledger=args.study_ledger,
            open_lock=args.open_lock,
            purpose=args.purpose,
            line=args.line,
            colab=client,
            log=_log_for(args.level),
            trace=_trace_for(args.level),
        )
    except Refused as exc:
        return _refused(exc)
    except (RunInterrupted, KeyboardInterrupt) as exc:
        outcome_p = out_dir / "outcome.json"
        if outcome_p.is_file():
            try:
                rec = json.loads(outcome_p.read_text(encoding="utf-8"))
                _out("LAUNCH_END status=%s session=%s spend_ccu=%s stopped=%s" % (
                    rec.get("status", "interrupted"), rec.get("session"), rec.get("spend_ccu"),
                    rec.get("session_stopped_confirmed"),
                ))
            except (OSError, ValueError):
                _error("LAUNCH_ERROR", exc, args.level)
        else:
            _error("LAUNCH_ERROR", exc, args.level)
        if (out_dir / "session_open.json").is_file():
            _err("hint: %s" % lookup("STOP_NOT_CONFIRMED").hint)
        return EXIT_RUNTIME
    except Exception as exc:
        exit_code = _error("LAUNCH_ERROR", exc, args.level)
        if (out_dir / "session_open.json").is_file():
            _err("hint: %s" % lookup("STOP_NOT_CONFIRMED").hint)
        return exit_code

    if args.dry:
        _out("DRY_RUN_OK session=%s card=%s shape=%s cap=%s" % (
            res["session"], res["card"], res["shape"], res["budget"]["cap"],
        ))
        if args.level >= NORMAL:
            _out(res["budget_summary"])
        return EXIT_OK

    refusal = res.get("refusal")
    cap_trip = res.get("cap_trip")
    code = (" code=%s" % refusal["code"]) if refusal else ""
    _out("LAUNCH_END status=%s session=%s spend_ccu=%s stopped=%s%s" % (
        res["status"], res["session"], res["spend_ccu"], res["session_stopped_confirmed"], code,
    ))
    if refusal:
        _err("hint: %s" % refusal["hint"])
    if cap_trip:
        _err("hint: %s" % cap_trip["hint"])
    if not res["session_stopped_confirmed"] and (out_dir / "session_open.json").is_file():
        _err("hint: %s" % lookup("STOP_NOT_CONFIRMED").hint)
    return int(res.get("exit_code", EXIT_RUNTIME))


def status_command(argv: list[str]) -> int:
    p = ScrubbingParser(prog="colab_runner status", description="Inspect a window out dir without touching the VM.")
    p.add_argument("--out-dir", required=True)
    p.add_argument("--json", action="store_true", help="emit structured JSON status")
    _add_levels(p)
    args = p.parse_args(argv)
    try:
        info = status_window(args.out_dir)
    except Refused as exc:
        return _refused(exc)
    if args.json:
        _out(json.dumps(info, indent=2))
    else:
        _out("STATUS state=%s session=%s card=%s status=%s spend_ccu=%s stop_pending=%s" % (
            info["state"], info["session"], info["card"], info["status"], info["spend_ccu"], info["stop_pending"],
        ))
    return EXIT_OK


def stop_command(argv: list[str]) -> int:
    """The compensator for a run killed outright: stop the session its `session_open.json` names, with the same
    confirmed-marker rule (the marker goes only after a confirmed stop). Nothing to stop is a success."""
    p = ScrubbingParser(prog="colab_runner stop", description="Stop the session a run left open in D.")
    p.add_argument("--out-dir", required=True)
    _add_levels(p)
    args = p.parse_args(argv)
    out_dir = Path(args.out_dir)
    marker = out_dir / "session_open.json"
    if not marker.is_file():
        _out("STOP_NOTHING: no session_open.json in %s" % out_dir)
        return EXIT_OK
    try:
        try:
            session = json.loads(marker.read_text(encoding="utf-8"))["session"]
            if not isinstance(session, str) or not session:
                raise ValueError("no session name")
        except (ValueError, KeyError, TypeError) as exc:
            raise Refused("SESSION_MARKER_INVALID", "%s does not name a session (%s)" % (marker, exc)) from None
        try:
            marker_mtime = marker.stat().st_mtime
        except OSError:
            marker_mtime = None
        client = cli_from_env()
        ok = Runner(client, session, out_dir, log=_log_for(args.level)).stop_session()
    except Refused as exc:
        return _refused(exc)
    except (Exception, KeyboardInterrupt) as exc:
        return _error("STOP_ERROR", exc, args.level)
    if ok:
        reconcile_stopped_outcome(out_dir, client, session, marker_mtime=marker_mtime)
    _out("STOP_END session=%s confirmed=%s" % (session, ok))
    if not ok:
        _err("hint: %s" % lookup("STOP_NOT_CONFIRMED").hint)
        return EXIT_RUNTIME
    return EXIT_OK


def queue_command(argv: list[str]) -> int:
    """Validate or run an unattended queue manifest (`queue validate` / `queue run`)."""
    p = ScrubbingParser(prog="colab_runner queue", description="Validate or run an unattended job queue.")
    p.add_argument("action", choices=("validate", "run"))
    p.add_argument("--queue", required=True, help="queue manifest (.toml or .json)")
    p.add_argument("--state-dir", help="state directory holding study_ledger.json, card_refusals.json, and open.lock")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--dry", action="store_true")
    mode.add_argument("--local-cpu", action="store_true")
    _add_levels(p)
    args = p.parse_args(argv)
    try:
        q = QueueSpec.from_file(args.queue)
        if args.action == "validate":
            _out("QUEUE_VALID jobs=%d out_root=%s" % (len(q.jobs), q.out_root))
            return EXIT_OK
        res = run_queue(
            args.queue,
            local_cpu=args.local_cpu,
            dry=args.dry,
            state_dir=args.state_dir,
            log=_log_for(args.level),
            trace=_trace_for(args.level),
        )
    except Refused as exc:
        return _refused(exc)
    except Exception as exc:
        return _error("QUEUE_ERROR", exc, args.level)
    c = res["counts"]
    _out(
        "QUEUE_END jobs=%d spend_ccu=%.6f done=%d failed_ours=%d failed_google=%d refused_capacity=%d stopped=%s"
        % (
            len(res["jobs"]), float(res["spend_ccu_total"]), c["done"], c["failed-ours"],
            c["failed-Google"], c["refused-capacity"], res["all_stopped_confirmed"],
        )
    )
    return int(res["exit_code"])


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args in ([], ["--help"], ["-h"]):
        print(USAGE)
        return EXIT_OK
    if args == ["--version"]:
        print(__version__)
        return EXIT_OK
    if args[0] == "launch":
        return launch_command(args[1:])
    if args[0] == "queue":
        return queue_command(args[1:])
    if args[0] == "status":
        return status_command(args[1:])
    if args[0] == "run":
        return run_command(args[1:])
    if args[0] == "stop":
        return stop_command(args[1:])
    _err("colab-runner: unknown arguments: %s" % " ".join(args))
    return EXIT_USER


if __name__ == "__main__":
    raise SystemExit(main())
