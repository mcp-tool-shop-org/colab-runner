"""Per-window spend guard: the vendored library's rate and cap functions, called, never re-implemented.

The library is `colab_runner/window_lib/launch_guards.py` (vendored byte for byte, see PROVENANCE.md):
- each `colab usage` reading is parsed by `parse_usage_text` and folded into the window-rate state by
  `update_window_rate` (stability over RATE_STABLE_GAP_S, GPU_RATE_FLOOR, the pre-open assignment count);
- the window is priced at `gpu_projection_rate`: max(--max-rate, the highest stable window rate). A window the
  library cannot price (no stable rate at or above the floor: every CPU window, an unattributable one) is priced at
  --max-rate x elapsed, the library's own floor;
- a stable rate above --max-rate is refused as the library refuses it mid-run (`window_rate_decision` above_max:
  RATE_ABOVE_MAX; assignments_unreadable: ASSIGNMENTS_UNREADABLE, as colab_window.sh's colab_midrun_rate_check);
- the cap is checked by `evaluate_cap_guard`, with the cleanup reserve of `compute_cap_trip` (the library's rule:
  cleanup_reserve = cleanup_bound_s / 3600 x priced rate), and a ceiling at or below zero is refused before the
  window starts by `evaluate_cap_budget` (CAP_TOO_SMALL_FOR_CLEANUP). The library has no default cleanup bound
  (`evaluate_cap_budget` takes it as a required argument; the shell computes it per lane), so it is required here.

The account balance is a RECORD only (Publisher ruling, 2026-11): `colab usage` and the balance are account-wide,
so a drop that belongs to other sessions (a G4 billing at about 10 per hour beside a CPU window at 0.26) would trip a
small window's cap within minutes. The guard passes the same reading as start and current balance, as the library's
GPU path does (colab_window.sh: `cur_bal = start_bal`), so the drop never prices the window."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

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
        return lg.evaluate_cap_guard(
            mode="live", cap=self.cap, margin=self.margin, cleanup_bound_s=self.cleanup_bound_s,
            start_balance=self.balance, current_balance=self.balance, rate_ccu_per_hour=self.priced_rate(),
            elapsed_s=elapsed_s, usage_parse_error=self.usage_error,
        )

    # -- what the runner calls ----------------------------------------------------------------------------------
    def fold_usage(self, raw: str, *, rc: int, epoch: float, at: str) -> None:
        """One `colab usage` reading: parsed by the library, folded into the rate state by the library. A reading
        that failed is kept as the error the next check trips on (COLAB_USAGE_PARSE_FAILED, the library's rule)."""
        try:
            parsed = lg.parse_usage_text(raw, "gpu", usage_rc=rc, session_opened=True)
        except lg.RateUnread as exc:  # the account reads no positive rate yet: still a reading
            parsed = dict(exc.partial, ok=True, error=None)
        if not parsed.get("ok"):
            self.usage_error = parsed.get("error") or "COLAB_USAGE_PARSE_FAILED"
            return
        self.usage_error = None
        self.balance = parsed.get("balance")
        self.balances.append({"at": at, "balance": self.balance, "used_for_pricing": False})
        lg.update_window_rate(
            self.state, account_rate=parsed.get("rate_measured"), pre_open_rate=self.pre_open_rate, at=at,
            epoch=epoch, assignments=parsed.get("active_assignments"), pre_open_assignments=self.pre_open_assignments,
        )

    def check(self, elapsed_s: float) -> Optional[str]:
        """A trip reason, or None: the library's mid-run rate refusals, then its cap guard."""
        decision = self.decision()
        if decision == "above_max":
            return "RATE_ABOVE_MAX: the window's stable rate %s/h is above --max-rate %s/h" % (
                self.state.get("best_stable"), self.max_rate)
        if decision == "assignments_unreadable":
            return ("ASSIGNMENTS_UNREADABLE: 3 consecutive bad reads: no readable Active assignments count, or a "
                    "stable window rate below the floor")
        rec = self.cap_record(elapsed_s)
        if rec["tripped"]:
            return "%s: spend=%s ceiling=%s rate=%s/h%s" % (
                rec["code"], rec["spend_ccu"], rec["ceiling_ccu"], rec["priced_rate"],
                " (%s)" % rec["usage_parse_error"] if rec.get("usage_parse_error") else "")
        return None

    def seconds_left(self, elapsed_s: float) -> float:
        """Seconds until the priced spend (evaluate_cap_guard: priced rate x elapsed) reaches the ceiling."""
        return self.trip()["ceiling_ccu"] / self.priced_rate() * 3600.0 - elapsed_s
