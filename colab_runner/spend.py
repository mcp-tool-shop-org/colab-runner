"""Per-window spend guard: the vendored library's rate and cap functions, priced locally by rate x elapsed.

A running window stops only for cap (own rate x elapsed >= ceiling), wall, or a dead job. Flaky account-level
`colab usage` reads, unreadable active-assignment counts, and mid-run rate drift are logged rather than halting a
running job; only open-time rate checks in `launcher` refuse an oversized rate.

The account balance is a RECORD only (Publisher ruling, 2026-11): `colab usage` and the balance are account-wide, so
the guard passes the same non-negative reading as start and current balance and never prices from balance drops."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

from colab_runner.runner import Refused, require_number
from colab_runner.window_lib import launch_guards as lg


def neighbour_warning(assignments_now: int) -> Optional[str]:
    """A line to log when other assignments are open. Never an error: it explains why `colab usage` and the balance
    are not this window's numbers (PLATFORM.md: usage is account-wide)."""
    others = max(0, assignments_now - 1)
    if others == 0:
        return None
    return (
        "NEIGHBOURS_OPEN: %d other assignment(s) on this account; usage and the balance are account-wide and are "
        "not used to price this window" % others
    )


@dataclass(kw_only=True)
class SpendGuard:
    max_rate: float
    cap: float
    cleanup_bound_s: float
    margin: float = 0.0
    pre_open_rate: Optional[float] = None  # the account's usage rate read just before the session was opened
    pre_open_assignments: Optional[int] = None  # and its active assignments, then
    state: dict = field(default_factory=dict)  # the library's window-rate state (update_window_rate)
    balance: Optional[float] = None  # the latest reading's balance: a record only
    usage_error: Optional[str] = None  # the latest reading's parse failure, if it failed
    balances: list = field(default_factory=list)
    log: Optional[Callable[[str], None]] = field(default=None, repr=False, compare=False)

    def _emit(self, message: str) -> None:
        if self.log is not None:
            self.log(message)

    def __post_init__(self):
        require_number("MAX_RATE_INVALID", "max_rate", self.max_rate)
        require_number("CAP_INVALID", "cap", self.cap)
        require_number("MARGIN_INVALID", "margin", self.margin, inclusive=True)
        require_number("CLEANUP_BOUND_INVALID", "cleanup_bound_s", self.cleanup_bound_s, inclusive=True)
        if self.pre_open_rate is not None:
            require_number("PRE_OPEN_RATE_INVALID", "pre_open_rate", self.pre_open_rate, inclusive=True)
        if self.pre_open_assignments is not None:
            require_number("PRE_OPEN_ASSIGNMENTS_INVALID", "pre_open_assignments", self.pre_open_assignments,
                           inclusive=True, integer=True)
        if (self.pre_open_rate is None) != (self.pre_open_assignments is None):
            # both come from one pre-open `colab usage` reading: a rate without its count lets a neighbour that
            # closed meanwhile hide this window's rate; a count without its rate prices against a zero baseline
            raise Refused("PRE_OPEN_UNPAIRED", "pre_open_rate and pre_open_assignments come from one pre-open reading: "
                          "give both, or neither")
        budget = self.budget()
        if budget["tripped"]:  # margin + cleanup reserve >= cap: the library's refusal, before any colab call
            raise Refused(budget["code"], budget["why"])

    # -- the library's numbers --------------------------------------------------------------------------------
    def budget(self) -> dict:
        return lg.evaluate_cap_budget(cap=self.cap, margin=self.margin, cleanup_bound_s=self.cleanup_bound_s,
                                      priced_rate=self.priced_rate(), max_rate=self.max_rate)

    def priced_rate(self) -> float:
        return lg.gpu_projection_rate(self.state, self.max_rate)

    def trip(self) -> dict:
        return lg.compute_cap_trip(self.cap, self.margin, self.cleanup_bound_s, self.priced_rate())

    @property
    def ceiling(self) -> float:
        return self.trip()["ceiling_ccu"]

    def decision(self) -> str:
        return lg.window_rate_decision(self.state, self.max_rate)

    def cap_record(self, elapsed_s: float) -> dict:
        bal = self.balance if (self.balance is not None and self.balance >= 0.0) else None
        return lg.evaluate_cap_guard(
            mode="offline", cap=self.cap, margin=self.margin, cleanup_bound_s=self.cleanup_bound_s,
            start_balance=bal, current_balance=bal, rate_ccu_per_hour=self.priced_rate(),
            elapsed_s=elapsed_s, usage_parse_error=None,
        )

    # -- what the runner calls ----------------------------------------------------------------------------------
    def fold_usage(self, raw: str, *, rc: int, epoch: float, at: str) -> None:
        """One `colab usage` reading parsed and folded into rate state; errors/drift are logged, never a kill."""
        try:
            parsed = lg.parse_usage_text(raw, "gpu", usage_rc=rc, session_opened=True)
        except lg.RateUnread as exc:  # the account reads no positive rate yet: still a reading
            parsed = dict(exc.partial, ok=True, error=None)
        if not parsed.get("ok"):
            self.usage_error = parsed.get("error") or "colab usage parse failed"
            self._emit("usage read ignored (continuing priced locally at %s/h): %s"
                       % (self.priced_rate(), self.usage_error))
            return
        self.usage_error = None
        self.balance = parsed.get("balance")
        self.balances.append({"at": at, "balance": self.balance, "used_for_pricing": False})
        lg.update_window_rate(
            self.state, account_rate=parsed.get("rate_measured"), pre_open_rate=self.pre_open_rate, at=at,
            epoch=epoch, assignments=parsed.get("active_assignments"), pre_open_assignments=self.pre_open_assignments,
        )
        decision = self.decision()
        if decision == "above_max":
            self._emit("mid-run rate note (not a stop): window stable rate %s/h exceeds max_rate %s/h; "
                       "pricing at %s/h" % (self.state.get("best_stable"), self.max_rate, self.priced_rate()))
        elif decision == "assignments_unreadable":
            self._emit("usage note (not a stop): active assignments unreadable or rate below floor; "
                       "pricing at %s/h" % self.priced_rate())

    def check(self, elapsed_s: float) -> Optional[str]:
        """A trip reason, or None: only the local spend cap (priced rate x elapsed >= ceiling)."""
        rec = self.cap_record(elapsed_s)
        if rec["tripped"]:
            return "CAP_GUARD_TRIPPED: spend=%s ceiling=%s rate=%s/h" % (
                rec["spend_ccu"], rec["ceiling_ccu"], rec["priced_rate"])
        return None

    def seconds_left(self, elapsed_s: float) -> float:
        """Seconds until the priced spend (evaluate_cap_guard: priced rate x elapsed) reaches the ceiling."""
        return self.trip()["ceiling_ccu"] / self.priced_rate() * 3600.0 - elapsed_s
