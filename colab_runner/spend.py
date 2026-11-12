"""Per-window spend guard, priced from the window's own rate times its elapsed time.

The rule (Publisher ruling, 2026-11): every window is priced the same way, CPU included. The price is the window's
attributed rate times elapsed, or `max_rate` times elapsed when no stable rate can be attributed. The account balance
drop is a RECORD only. `colab usage` and the balance are account-wide, so on a shared account a drop that belongs to
other sessions (a G4 billing at about 10 per hour beside a CPU window at 0.26) would trip a small window's cap
within minutes if it were used for pricing. Nothing in this module reads a balance to decide anything."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


def attribute_rate(
    *, pre_open_assignments: int, assignments_now: int, account_rate_now: float, baseline_rate: float, max_rate: float
) -> tuple[float, str, int]:
    """The rate to price this window at, how it was found, and how many other assignments are open.

    Attributable only when exactly one assignment (this window) was added since the pre-open reading; then the rate is
    what the account rate rose by. Anything else (a neighbour opened or closed meanwhile) is not attributable and the
    window is priced at `max_rate`, the rate its own booking was capped with."""
    neighbours = max(0, assignments_now - 1)
    if assignments_now == pre_open_assignments + 1 and account_rate_now > baseline_rate:
        return round(account_rate_now - baseline_rate, 6), "attributed", neighbours
    return float(max_rate), "max_rate_x_elapsed", neighbours


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


@dataclass
class SpendGuard:
    rate_per_h: float
    cap: float
    margin: float = 0.0
    cleanup_reserve: float = 0.0
    balance_records: list = field(default_factory=list)

    def __post_init__(self):
        # a guard with rate 0 prices every window at 0 forever, and a non-positive cap trips at once or never
        if not (self.rate_per_h > 0 and self.cap > 0):
            raise ValueError("SpendGuard needs a rate and a cap greater than 0 (rate=%r, cap=%r)" % (self.rate_per_h, self.cap))

    @property
    def ceiling(self) -> float:
        return self.cap - self.margin - self.cleanup_reserve

    def spend(self, elapsed_s: float) -> float:
        return self.rate_per_h * max(0.0, elapsed_s) / 3600.0

    def check(self, elapsed_s: float) -> Optional[str]:
        """A trip reason, or None. Priced from this window's rate and elapsed only."""
        spent = self.spend(elapsed_s)
        if spent >= self.ceiling:
            return "CAP_GUARD_TRIPPED spend=%.4f ceiling=%.4f rate=%.4f/h" % (spent, self.ceiling, self.rate_per_h)
        return None

    def record_balance(self, start: float, end: float) -> dict:
        """Keep the account balance reading for the record. It decides nothing."""
        rec = {"start": start, "end": end, "drop": round(start - end, 6), "used_for_pricing": False}
        self.balance_records.append(rec)
        return rec
