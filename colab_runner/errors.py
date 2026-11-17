"""Every named code colab-runner refuses or fails with, and the one shape they all take.

A code is UPPER_SNAKE. Each has a one-line `hint` (what the caller should change) and `retryable` (would the same
call, unchanged, be worth retrying: False for every refusal of the caller's input, which fails the same way again).
The CLI prints `REFUSED: CODE: message` (or `RUN_ERROR: CODE: message`) and then `hint: ...`; `runner_result.json`
records a refusal as `{code, message, hint, retryable}`, and a `cap` end's trip the same way, as `cap_trip`.

Every code reaches the source through `Refused`, `require_number`, `error_record` or a `code` assignment, or is a
cap trip's (the `CODE:` prefixes of `SpendGuard.check` in spend.py, and the codes of the library's `evaluate_cap_guard`
that it passes on), so a test reads them all from the source (tests/test_errors.py) and checks each one has an entry
here."""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Code:
    hint: str
    retryable: bool


_FIX = False  # a refusal of the caller's input: retrying it unchanged fails the same way

CODES: dict[str, Code] = {
    # the window's bounds (CLI and SpendGuard)
    "CAP_REQUIRED": Code("pass --cap C, the window's spend cap in compute units", _FIX),
    "CAP_INVALID": Code("pass --cap as a finite number greater than 0", _FIX),
    "MAX_RATE_REQUIRED": Code("pass --max-rate R, the card's expected rate per hour; it prices the window", _FIX),
    "MAX_RATE_INVALID": Code("pass --max-rate as a finite number greater than 0", _FIX),
    "MARGIN_INVALID": Code("pass --margin as a finite number of 0 or more", _FIX),
    "CLEANUP_BOUND_REQUIRED": Code(
        "pass --cleanup-bound-s N, at least the runner's own cleanup bound (466 s, or 986 s with --resident, at the "
        "default timeouts)", _FIX),
    "CLEANUP_BOUND_INVALID": Code("pass --cleanup-bound-s as a finite number of 0 or more", _FIX),
    "CLEANUP_BOUND_TOO_SMALL": Code("raise --cleanup-bound-s to at least the bound the message names", _FIX),
    "CAP_TOO_SMALL_FOR_CLEANUP": Code(
        "raise --cap, or lower --margin or --cleanup-bound-s: the cap must exceed the margin plus the cleanup reserve",
        _FIX),
    "PRE_OPEN_RATE_INVALID": Code("pass --pre-open-rate as a finite number of 0 or more, read before the session "
                                  "opened", _FIX),
    "PRE_OPEN_ASSIGNMENTS_INVALID": Code("pass --pre-open-assignments as a whole number of 0 or more", _FIX),
    "PRE_OPEN_UNPAIRED": Code("pass --pre-open-rate and --pre-open-assignments together, from one reading, or "
                              "neither", _FIX),
    # the run's settings (CLI and Runner.run)
    "WALL_INVALID": Code("pass --wall-s as a finite number of seconds greater than 0", _FIX),
    "POLL_INVALID": Code("pass --poll-s as a finite number of seconds greater than 0", _FIX),
    "HEARTBEAT_INVALID": Code("pass --heartbeat-s as a finite number of seconds (greater than 0 with --hold-s)", _FIX),
    "HOLD_INVALID": Code("pass --hold-s as a finite number of seconds greater than 0", _FIX),
    "PROGRESS_STALL_INVALID": Code("pass --progress-stall-s as a finite number of seconds greater than 0", _FIX),
    "SETUP_STALL_INVALID": Code("pass --setup-stall-s as a finite number of seconds greater than 0", _FIX),
    "UNREACHABLE_INVALID": Code("pass --unreachable-polls as a whole number of 1 or more, and --unreachable-s as a "
                                "finite number of seconds greater than 0", _FIX),
    "DEAD_CONFIRM_INVALID": Code("pass dead_confirm as a whole number of 1 or more", _FIX),
    "SETTING_INVALID": Code("pass the Runner setting the message names as a finite number in its range", _FIX),
    "NOTHING_TO_RUN": Code("pass script= (--script) or hold_s= (--hold-s)", _FIX),
    "RESIDENT_MAX_INVALID": Code("pass CR_RESIDENT_MAX_S as a finite number of seconds greater than 0, or leave it "
                                 "out (the window's length is the default)", _FIX),
    "OUT_DIR_NOT_EMPTY": Code("pass an --out-dir that does not exist yet, or is empty: a new one for each run", _FIX),
    "OUT_DIR_INVALID": Code("pass an --out-dir that can be created and listed, below a filesystem root (its quarantine "
                            "is a sibling folder)", _FIX),
    "CLI_INVALID": Code("fix COLAB_RUNNER_CLI: it is split with POSIX shell quoting, so close every quote (or unset "
                        "it to use `colab`)", _FIX),
    "ENV_INVALID": Code("pass --env as NAME=VALUE or NAME, with NAME a variable name", _FIX),
    "ENV_UNSET": Code("set the variable in this environment before the run, or pass --env NAME=VALUE", _FIX),
    "RUN_ID_RESERVED": Code("drop CR_RUN_ID from --env: the runner gives every run its own id", _FIX),
    # stop
    "SESSION_MARKER_INVALID": Code("stop the session by name with `colab stop -s S`: this session_open.json does not "
                                   "name one", _FIX),
    "STOP_NOT_CONFIRMED": Code("run `python -m colab_runner stop --out-dir D` again; session_open.json stays until a "
                               "stop is confirmed", True),
    # ends of a run that started
    "JOB_ALREADY_EXISTS": Code("pass a distinct --job for each step that shares a --content directory", _FIX),
    "BINARY_DOWNLOAD_TOKEN": Code(
        "keep tokens out of the job's binary outputs; delete the quarantine named in runner_result.json and never "
        "ship it", _FIX),
    "QUARANTINE_ESCAPE": Code(
        "report it: the file was removed rather than moved outside its quarantine; pass a plain --out-dir (or a "
        "download path) without `..`", _FIX),
    # cap trips: a `cap` end, not a refusal (runner_result.json `cap_trip`; the hint is printed after RUN_END)
    "CAP_GUARD_TRIPPED": Code(
        "the window's priced spend reached its ceiling (the cap less --margin and the cleanup reserve): raise --cap, or "
        "give the job less to do in one window", _FIX),
    "CAP_CEILING_REACHED": Code(
        "the clock reached the cap's ceiling at the priced rate (the cap less --margin and the cleanup reserve): raise "
        "--cap, or give the job less to do in one window", _FIX),
    "RATE_ABOVE_MAX": Code(
        "the window's stable rate went above --max-rate: pass the card's real rate as --max-rate (and a --cap to "
        "match), and --pre-open-rate with --pre-open-assignments so a neighbour's rate is not counted as this one's",
        _FIX),
    "ASSIGNMENTS_UNREADABLE": Code(
        "`colab usage` gave no readable Active assignments count (or a stable rate below the floor) 3 times in a "
        "row: check `colab usage` by hand, then run the window again", True),
    "COLAB_USAGE_PARSE_FAILED": Code(
        "`colab usage` could not be read or parsed, and the guard never prices a window it cannot read: check "
        "`colab usage` by hand (sign-in, network), then run the window again", True),
    "UNEXPECTED_ERROR": Code(
        "read runner_result.json; if session_open.json is still in the out dir, run `python -m colab_runner stop "
        "--out-dir D`; rerun with --debug for the traceback", _FIX),
}

_UNKNOWN = Code("see the README, \"Using it from a lane\"", False)


def lookup(code: str) -> Code:
    return CODES.get(code, _UNKNOWN)


def error_record(code: str, message: str) -> dict:
    """`{code, message, hint, retryable}`: how a named code is recorded (runner_result.json) and printed."""
    c = lookup(code)
    return {"code": code, "message": message, "hint": c.hint, "retryable": c.retryable}


_TRIP_CODE_RE = re.compile(r"([A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+): (.*)", re.S)


def cap_trip_record(trip: str) -> dict:
    """A cap trip string (`CODE: message`, as SpendGuard.check writes it) as `{code, message, hint, retryable}`. A
    trip with no leading code (a stand-in guard's) keeps the whole string as its message, with a null code."""
    m = _TRIP_CODE_RE.fullmatch(trip)
    if m is None:
        return {"code": None, "message": trip, "hint": _UNKNOWN.hint, "retryable": _UNKNOWN.retryable}
    return error_record(m.group(1), m.group(2))


class Refused(ValueError):
    """A run refused by name (`code`), with what to change (`hint`). Refusals of a run's inputs are raised before
    any `colab` call."""

    def __init__(self, code: str, message: str, *, hint: str | None = None, retryable: bool | None = None):
        super().__init__("%s: %s" % (code, message))
        c = lookup(code)
        self.code, self.message = code, message
        self.hint = c.hint if hint is None else hint
        self.retryable = c.retryable if retryable is None else retryable

    @property
    def why(self) -> str:  # the 0.1.0 name of `message`
        return self.message

    def record(self) -> dict:
        return {"code": self.code, "message": self.message, "hint": self.hint, "retryable": self.retryable}


__all__ = ["CODES", "Code", "Refused", "cap_trip_record", "error_record", "lookup"]
