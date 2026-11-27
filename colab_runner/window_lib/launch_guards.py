#!/usr/bin/env python3
"""Trimmed window guards used by colab-runner (wall, cap, rate, sha normalization, OOM, token scrub)."""

from __future__ import annotations

import re
from typing import Any

OOM_KILL_RE = re.compile(r"\boom_kill\s+([1-9]\d*)\b|\bOut of memory: Killed process\b", re.I)

TOKEN_SCRUB_PATTERNS = [
    (re.compile(r"(colab-runtime-proxy-token=)[^\s&\"']+"), r"\1[REDACTED]"),
    (re.compile(r"(access_token=)[^\s&\"']+"), r"\1[REDACTED]"),
    (re.compile(r"(Bearer\s+)[A-Za-z0-9._\-]+"), r"\1[REDACTED]"),
    (re.compile(r"\bhf_[A-Za-z0-9]{16,}\b"), "hf_[REDACTED]"),
]


def normalize_hex_sha(val: str) -> str:
    s = str(val).strip().lower()
    if s.startswith("sha256:"):
        s = s[7:]
    return s


def scrub_colab_tokens(text: str) -> str:
    out = text
    for pattern, repl in TOKEN_SCRUB_PATTERNS:
        out = pattern.sub(repl, out)
    return out


def evaluate_wall_guard(
    *,
    mode: str = "live",
    elapsed_s: float,
    wall_s: float = 3000.0,
    grace_s: float = 60.0,
) -> dict[str, Any]:
    threshold_s = round(max(0.0, float(wall_s) - float(grace_s)), 6)
    elapsed_val = round(float(elapsed_s), 6)
    remaining_s = round(threshold_s - elapsed_val, 6)
    tripped = elapsed_val >= threshold_s
    return {
        "mode": mode,
        "tripped": tripped,
        "code": "WALL_GUARD_TRIPPED" if tripped else None,
        "remaining_s": remaining_s,
        "threshold_s": threshold_s,
    }


CURRENT_BALANCE_RE = re.compile(
    r"^\s*Current\s+balance:\s*([+-]?\d+(?:\.\d+)?)\s*(?:compute\s+units)?\s*$",
    re.I | re.M,
)
USAGE_RATE_RE = re.compile(
    r"^\s*Usage\s+rate:\s*([+-]?\d+(?:\.\d+)?)\s*/\s*hr?\s*$",
    re.I | re.M,
)
ACTIVE_ASSIGNMENTS_RE = re.compile(
    r"^\s*Active\s+assignments:\s*(\d+)\s*$",
    re.I | re.M,
)


class RateUnread(Exception):
    """A GPU window has no positive live post-open rate. `partial` carries what the usage text did
    give (balance, the readings), so a caller can record it; the caller decides whether this
    means "not yet" (polling right after open) or a refusal (RATE_UNREAD)."""

    code = "RATE_UNREAD"

    def __init__(self, partial: dict[str, Any]) -> None:
        super().__init__("RATE_UNREAD: no positive live post-open rate for a GPU window")
        self.partial = partial


CARD_CLASSES = ("gpu", "cpu")

# The window rate. `colab usage` reports the ACCOUNT's rate (docs/colab-cli-contract.md): a window's
# own rate is the account reading minus the neighbour baseline (at first the pre-open reading). It
# counts only when it is stable (two reads at least RATE_STABLE_GAP_S apart agreeing within
# RATE_STABLE_TOLERANCE) and at or above GPU_RATE_FLOOR. The floor is below the cheapest GPU
# measured, the T4 at 1.07/hr (16 reads, receipt windowC2-12-t4-s1a-20261027), and above the highest
# CPU rate, 0.26/hr. A GPU window is priced on max(--max-rate, the highest stable window rate seen),
# never on a window or account rate alone.
#
# "Active assignments" is recorded with every reading. When it changes, another window opened or
# closed. Nothing is judged until the count settles: two counted reads at the new count, at least
# RATE_STABLE_GAP_S apart, whose account rates agree within the tolerance (the count can arrive a
# read before the neighbour's rate). Then the neighbour is re-baselined: baseline = account - the
# highest stable rate of ours seen (not the last one: a low stable read taken while a neighbour's
# rate led its count would otherwise inflate the baseline and hide our later rise). Pending or settling reads are never judged against the max and never raise our
# priced rate, which stays max(--max-rate, highest stable seen). Only a read at an unchanged count
# may raise our rate or trip the max. Known limit: a neighbour opening on the same read our own rate
# rises folds that rise into the baseline; pricing at max(--max-rate, seen) bounds the cost.
#
# Before our window's first stable read, a missing count or 0 means "not yet registered": the
# receipts show the rate and the count arriving together (windowC2-12-t4-s1a-20261027 reads 0.00
# with 0 assignments until the session registers, then 1.07 with 1). Such a read is recorded,
# uncounted, and spends only the RATE_UNREAD poll budget. After the first stable read, a missing
# count or 0 is a bad read: recorded, the rate not judged, and ASSIGNMENTS_UNREADABLE after
# ASSIGNMENTS_BAD_LIMIT consecutive bad reads. After the first stable read the floor applies too: a
# stable pair below GPU_RATE_FLOOR (our window cannot bill under the cheapest GPU) is never
# accepted; it is recorded `below_floor` and counts as a bad read toward the same limit. Known
# limit: a rate that leads its count by two or more reads at open waits for the count before it
# can be stable.
GPU_RATE_FLOOR = 1.00
RATE_STABLE_TOLERANCE = 0.01
RATE_STABLE_GAP_S = 5.0
ASSIGNMENTS_BAD_LIMIT = 3


def parse_active_assignments(raw_usage: str) -> int | None:
    m = re.search(r"^\s*Active\s+assignments:\s*(\d+)\s*$", raw_usage or "", re.I | re.M)
    return int(m.group(1)) if m else None


def update_window_rate(state: dict[str, Any], *, account_rate: float | None, pre_open_rate: float | None,
                       at: str, epoch: float, assignments: int | None,
                       min_gap_s: float = RATE_STABLE_GAP_S, floor: float = GPU_RATE_FLOOR,
                       tolerance: float = RATE_STABLE_TOLERANCE,
                       pre_open_assignments: int | None = None) -> dict[str, Any]:
    """Fold one post-open `colab usage` reading into the window-rate state (kept in the out dir).
    Parallel windows (2026-11-12): with `pre_open_assignments` known, the first counted reading must
    show exactly one more assignment than before the open. The same count means this window has
    not registered yet (not counted, poll again); any other count means a neighbour opened or closed
    in between, so (account − pre-open) is not this window's rate: `unattributable`, never guessed.
    Any other assignment before or during the window sets `shared_account` (the balance is shared).
    Every reading is recorded. A bad count (missing, or 0) is recorded and judged nothing; three in
    a row set `assignments_unreadable`. A read less than `min_gap_s` after the last counted one is
    recorded as `counted: false`. A changed count starts a pending re-baseline that settles on two
    agreeing reads at the new count. Otherwise the window rate is `stable` when it is at or above
    the floor and agrees with the last counted window rate within the tolerance; `best_stable` is
    the highest stable one."""
    readings = state.setdefault("readings", [])
    state["floor"] = floor
    if "neighbour_baseline" not in state:
        state["neighbour_baseline"] = float(pre_open_rate) if pre_open_rate is not None else 0.0
    if pre_open_assignments is not None and "pre_open_assignments" not in state:
        state["pre_open_assignments"] = int(pre_open_assignments)
    pre_count = state.get("pre_open_assignments")
    if (pre_count is not None and int(pre_count) > 0) or (assignments is not None and int(assignments) > 1):
        state["shared_account"] = True
    base = float(state["neighbour_baseline"])
    rec: dict[str, Any] = {"at": at, "epoch": round(float(epoch), 3), "account_rate": account_rate,
                           "pre_open_rate": pre_open_rate, "assignments": assignments,
                           "pre_open_assignments": pre_count,
                           "neighbour_baseline": base, "window_rate": None, "stable": False}
    first_count = "last_assignments" not in state and state.get("best_stable") is None
    if (first_count and pre_count is not None and assignments is not None
            and int(assignments) == int(pre_count) and int(pre_count) > 0):
        # This window is not on the account's count yet (registration lag): judge nothing.
        rec.update({"counted": False, "not_registered": True})
        readings.append(rec)
        return state
    if (assignments is None or int(assignments) <= 0) and state.get("best_stable") is None:
        rec.update({"counted": False, "not_registered": True})
        readings.append(rec)
        return state
    if assignments is None or int(assignments) <= 0:
        state["assignments_bad_streak"] = int(state.get("assignments_bad_streak", 0)) + 1
        if state["assignments_bad_streak"] >= ASSIGNMENTS_BAD_LIMIT:
            state["assignments_unreadable"] = True
        rec.update({"counted": False, "assignments_bad": True})
        readings.append(rec)
        return state
    bad_streak_before = int(state.get("assignments_bad_streak", 0))
    state["assignments_bad_streak"] = 0
    last_epoch = state.get("last_counted_epoch")
    if last_epoch is not None and float(epoch) - float(last_epoch) < float(min_gap_s):
        rec["counted"] = False
        readings.append(rec)
        return state
    rec["counted"] = True
    state["last_counted_epoch"] = float(epoch)
    known = "last_assignments" in state
    if (not known and pre_count is not None and state.get("best_stable") is None
            and int(assignments) != int(pre_count) + 1):
        state["unattributable"] = True
        state["last_assignments"] = assignments
        rec.update({"unattributable": True, "expected_assignments": int(pre_count) + 1})
        readings.append(rec)
        return state
    prev_count = state.get("last_assignments")
    state["last_assignments"] = assignments
    pending = state.get("rebaseline_pending")
    if known and assignments != prev_count:
        # A new count: start (or restart) the pending re-baseline. Judge nothing.
        state["rebaseline_pending"] = {"count": assignments, "from": prev_count, "last_account": account_rate}
        rec.update({"rebaseline_pending": True, "assignments_before": prev_count})
        readings.append(rec)
        return state
    if pending is not None:
        last_acc = pending.get("last_account")
        if (account_rate is None or last_acc is None
                or abs(float(account_rate) - float(last_acc)) > tolerance):
            pending["last_account"] = account_rate
            rec["rebaseline_pending"] = True
            readings.append(rec)
            return state
        # Settled: two agreeing reads at the new count. The neighbour is what the account reads above
        # the highest stable rate of ours seen: the conservative direction (our later reads can only
        # come out higher, never hidden under an inflated baseline).
        ours = state.get("best_stable")
        if ours is not None:
            base = round(max(float(account_rate) - float(ours), 0.0), 4)
            state["neighbour_baseline"] = base
        state["last_window"] = ours
        state.pop("rebaseline_pending", None)
        rec.update({"rebaselined": True, "window_rate": ours, "neighbour_baseline": base,
                    "assignments_before": pending.get("from")})
        readings.append(rec)
        return state
    window = None if account_rate is None else round(max(float(account_rate) - base, 0.0), 4)
    prev = state.get("last_window")
    stable = (window is not None and window >= floor and prev is not None
              and abs(window - float(prev)) <= tolerance)
    below_floor = (window is not None and window < floor and prev is not None
                   and abs(window - float(prev)) <= tolerance and state.get("best_stable") is not None)
    rec.update({"window_rate": window, "stable": stable})
    if below_floor:
        # Mid-run, a stable own rate below the floor is a bad read, never an accepted rate.
        state["assignments_bad_streak"] = bad_streak_before + 1
        if state["assignments_bad_streak"] >= ASSIGNMENTS_BAD_LIMIT:
            state["assignments_unreadable"] = True
        rec["below_floor"] = True
    readings.append(rec)
    state["last_window"] = window
    if stable:
        state["last_stable"] = window
        if state.get("best_stable") is None or window > state["best_stable"]:
            state["best_stable"] = window
    return state


def gpu_projection_rate(state: dict[str, Any], max_rate: float) -> float:
    """The rate a GPU window's cap is projected with: max(--max-rate, highest stable window rate)."""
    best = state.get("best_stable")
    return max(float(max_rate), float(best)) if best is not None else float(max_rate)


def window_rate_decision(state: dict[str, Any], max_rate: float) -> str:
    """`assignments_unreadable`, `unread` (no stable window rate at or above the floor yet),
    `above_max` or `ok`."""
    if state.get("assignments_unreadable"):
        return "assignments_unreadable"
    if state.get("unattributable") and state.get("best_stable") is None:
        return "unattributable"
    best = state.get("best_stable")
    if best is None:
        return "unread"
    return "above_max" if float(best) > float(max_rate) else "ok"


def parse_usage_text(
    raw_usage: str,
    card_class: str,
    *,
    usage_rc: int = 0,
    configured_rate: float = 0.26,
    session_opened: bool = True,
    post_open_rate: float | None = None,
    pre_open_rate: float | None = None,
) -> dict[str, Any]:
    """Parse `colab usage`. `card_class` ("gpu" or "cpu", required) decides the rate policy: a GPU
    window is priced only by its live post-open rate, and `_select_rate` raises RateUnread when it
    has none, so no GPU caller can reach a configured-rate branch. A CPU stub keeps the labelled
    configured rate."""
    if card_class not in CARD_CLASSES:
        raise ValueError(f"card_class must be one of {CARD_CLASSES}, got {card_class!r}")
    cfg = float(configured_rate)

    def _select_rate(
        cur_measured: float | None,
    ) -> tuple[float | None, float | None, float, str]:
        eff_pre = cur_measured if not session_opened else pre_open_rate
        existing_pos_post = post_open_rate if (post_open_rate is not None and post_open_rate > 0.0) else None
        if existing_pos_post is not None:
            eff_post: float | None = existing_pos_post
        elif session_opened and cur_measured is not None:
            eff_post = cur_measured
        elif post_open_rate is not None:
            eff_post = post_open_rate
        else:
            eff_post = None

        if eff_post is not None and eff_post > 0.0:
            return eff_pre, eff_post, float(eff_post), "post_open_measured"
        if card_class == "gpu":
            raise RateUnread({"rate_measured_pre_open": eff_pre, "rate_measured_post_open": eff_post})
        if eff_post is not None and eff_post == 0.0:
            pos_pre = float(eff_pre) if (eff_pre is not None and eff_pre > 0.0) else 0.0
            return eff_pre, eff_post, max(cfg, pos_pre), "configured_pending_post_open_rate"
        if eff_pre is not None:
            return eff_pre, None, max(float(eff_pre), cfg), "max_of_pre_open_and_configured"
        return None, None, cfg, "configured"

    def _failure_rates() -> tuple[float | None, float | None, float | None, str]:
        # A usage read that failed is COLAB_USAGE_PARSE_FAILED (the cap guard trips on it); for a
        # GPU window without a live rate it carries no projection rate at all, never a configured one.
        try:
            return _select_rate(None)
        except RateUnread as exc:
            return (exc.partial["rate_measured_pre_open"], exc.partial["rate_measured_post_open"], None,
                    "live_rate_unread")

    if usage_rc != 0:
        eff_pre, eff_post, rate_used, rate_src = _failure_rates()
        return {
            "ok": False,
            "code": "COLAB_USAGE_PARSE_FAILED",
            "balance": None,
            "rate_measured": None,
            "rate_measured_pre_open": eff_pre,
            "rate_measured_post_open": eff_post,
            "rate_for_projection": rate_used,
            "rate_used_for_cap_and_projection": rate_used,
            "rate_source": rate_src,
            "active_assignments": None,
            "error": f"COLAB_USAGE_PARSE_FAILED: colab usage exited with status {usage_rc}",
        }
    text = raw_usage or ""
    m_bal = CURRENT_BALANCE_RE.search(text)
    if not m_bal:
        eff_pre, eff_post, rate_used, rate_src = _failure_rates()
        return {
            "ok": False,
            "code": "COLAB_USAGE_PARSE_FAILED",
            "balance": None,
            "rate_measured": None,
            "rate_measured_pre_open": eff_pre,
            "rate_measured_post_open": eff_post,
            "rate_for_projection": rate_used,
            "rate_used_for_cap_and_projection": rate_used,
            "rate_source": rate_src,
            "active_assignments": None,
            "error": "COLAB_USAGE_PARSE_FAILED: missing 'Current balance: <float> compute units'",
        }
    balance = float(m_bal.group(1))
    if balance < 0.0:
        eff_pre, eff_post, rate_used, rate_src = _failure_rates()
        return {
            "ok": False,
            "code": "COLAB_USAGE_PARSE_FAILED",
            "balance": None,
            "rate_measured": None,
            "rate_measured_pre_open": eff_pre,
            "rate_measured_post_open": eff_post,
            "rate_for_projection": rate_used,
            "rate_used_for_cap_and_projection": rate_used,
            "rate_source": rate_src,
            "active_assignments": None,
            "error": f"COLAB_USAGE_PARSE_FAILED: negative balance {balance} is impossible",
        }
    m_rate = USAGE_RATE_RE.search(text)
    if m_rate:
        rate_measured: float | None = float(m_rate.group(1))
        if rate_measured < 0.0:
            eff_pre, eff_post, rate_used, rate_src = _failure_rates()
            return {
                "ok": False,
                "code": "COLAB_USAGE_PARSE_FAILED",
                "balance": None,
                "rate_measured": None,
                "rate_measured_pre_open": eff_pre,
                "rate_measured_post_open": eff_post,
                "rate_for_projection": rate_used,
                "rate_used_for_cap_and_projection": rate_used,
                "rate_source": rate_src,
                "active_assignments": None,
                "error": f"COLAB_USAGE_PARSE_FAILED: negative usage rate {rate_measured} is impossible",
            }
    else:
        rate_measured = None
    m_assign = ACTIVE_ASSIGNMENTS_RE.search(text)
    active_assignments = int(m_assign.group(1)) if m_assign else None
    try:
        eff_pre, eff_post, rate_for_projection, rate_source = _select_rate(rate_measured)
    except RateUnread as exc:
        exc.partial.update({"balance": balance, "rate_measured": rate_measured,
                            "active_assignments": active_assignments})
        raise
    return {
        "ok": True,
        "code": None,
        "balance": balance,
        "rate_measured": rate_measured,
        "rate_measured_pre_open": eff_pre,
        "rate_measured_post_open": eff_post,
        "rate_for_projection": rate_for_projection,
        "rate_used_for_cap_and_projection": rate_for_projection,
        "rate_source": rate_source,
        "active_assignments": active_assignments,
        "error": None,
    }


CAP_TRIP_FORMULA = (
    "effective_trip = cap - margin - cleanup_reserve; cleanup_reserve = (cleanup_bound_s / 3600) * priced_rate"
)


def compute_cap_trip(
    cap: float,
    margin: float,
    cleanup_bound_s: float = 0.0,
    priced_rate: float = 0.0,
) -> dict[str, Any]:
    bound_s = max(0.0, float(cleanup_bound_s))
    rate = max(0.0, float(priced_rate))
    cleanup_reserve = round((bound_s / 3600.0) * rate, 6)
    effective_trip = round(float(cap) - float(margin) - cleanup_reserve, 6)
    ceiling = round(max(0.0, effective_trip), 6)
    return {
        "cap_ccu": round(float(cap), 6),
        "margin_ccu": round(float(margin), 6),
        "cleanup_bound_s": int(bound_s) if bound_s == int(bound_s) else round(bound_s, 6),
        "priced_rate": round(rate, 6),
        "cleanup_reserve_units": cleanup_reserve,
        "effective_trip_units": effective_trip,
        "ceiling_ccu": ceiling,
        "formula": CAP_TRIP_FORMULA,
        "cap_trip_formula": CAP_TRIP_FORMULA,
    }


def evaluate_cap_budget(
    *,
    cap: float,
    margin: float,
    cleanup_bound_s: float,
    priced_rate: float,
    max_rate: float | None = None,
    min_runtime_s: float | None = None,
) -> dict[str, Any]:
    trip = compute_cap_trip(cap=cap, margin=margin, cleanup_bound_s=cleanup_bound_s, priced_rate=priced_rate)
    eff_max_rate = round(float(max_rate), 6) if max_rate is not None else trip["priced_rate"]
    effective_trip = trip["effective_trip_units"]
    cleanup_reserve = trip["cleanup_reserve_units"]
    p_rate = trip["priced_rate"]
    bound_s = trip["cleanup_bound_s"]
    cap_v = trip["cap_ccu"]
    margin_v = trip["margin_ccu"]

    runtime_at_priced_rate_s = round((effective_trip / p_rate) * 3600.0, 2) if p_rate > 0.0 else 0.0
    runtime_at_max_rate_s = (
        round((effective_trip / eff_max_rate) * 3600.0, 2) if eff_max_rate > 0.0 else runtime_at_priced_rate_s
    )
    death_time_exact_s = max(0.0, (effective_trip / p_rate) * 3600.0) if p_rate > 0.0 else 0.0
    death_time_max_s = max(0.0, runtime_at_priced_rate_s)
    worst_case_cap_path = round(effective_trip + margin_v + cleanup_reserve, 6)
    worst_case_launcher_death_path = round(((death_time_exact_s + bound_s) / 3600.0) * p_rate, 6)
    worst_case_total_spend = round(max(worst_case_cap_path, worst_case_launcher_death_path), 6)
    assumptions = {
        "cap_path": "live launcher trips cap guard and runs cleanup_on_exit",
        "launcher_death_path": (
            "wall watcher survives launcher death to poll kill -0 every 1 s and run bounded colab stop"
        ),
    }

    tripped = False
    code: str | None = None
    why: str | None = None
    if effective_trip <= 0.0:
        tripped = True
        code = "CAP_TOO_SMALL_FOR_CLEANUP"
        why = (
            f"effective_trip = cap {cap_v} - margin {margin_v} - cleanup_reserve {cleanup_reserve} "
            f"(cleanup_bound_s {bound_s} / 3600 * priced_rate {p_rate}/h) = {effective_trip} <= 0"
        )
    elif min_runtime_s is not None and runtime_at_priced_rate_s < float(min_runtime_s):
        min_rt = (
            int(min_runtime_s)
            if float(min_runtime_s) == int(float(min_runtime_s))
            else round(float(min_runtime_s), 2)
        )
        tripped = True
        code = "CAP_TOO_SMALL_FOR_CLEANUP"
        why = (
            f"effective_trip = cap {cap_v} - margin {margin_v} - cleanup_reserve {cleanup_reserve} "
            f"(cleanup_bound_s {bound_s} / 3600 * priced_rate {p_rate}/h) = {effective_trip} units "
            f"leaves {runtime_at_priced_rate_s} s at {p_rate}/h < --min-runtime-s {min_rt} s"
        )

    rec: dict[str, Any] = {
        "check": "cap_budget",
        "ok": not tripped,
        "tripped": tripped,
        "code": code,
        "why": why,
        "cap": cap_v,
        "cap_ccu": cap_v,
        "margin": margin_v,
        "margin_ccu": margin_v,
        "cleanup_bound_s": bound_s,
        "priced_rate": p_rate,
        "max_rate": eff_max_rate,
        "reserve": cleanup_reserve,
        "cleanup_reserve": cleanup_reserve,
        "cleanup_reserve_units": cleanup_reserve,
        "effective_trip": effective_trip,
        "effective_trip_units": effective_trip,
        "runtime_to_trip_s": runtime_at_priced_rate_s,
        "runtime_at_priced_rate_s": runtime_at_priced_rate_s,
        "runtime_at_max_rate_s": runtime_at_max_rate_s,
        "death_time_max_s": death_time_max_s,
        "min_runtime_s": (
            None
            if min_runtime_s is None
            else (
                int(min_runtime_s)
                if float(min_runtime_s) == int(float(min_runtime_s))
                else round(float(min_runtime_s), 2)
            )
        ),
        "worst_case_cap_path": worst_case_cap_path,
        "worst_case_launcher_death_path": worst_case_launcher_death_path,
        "worst_case_total_spend": worst_case_total_spend,
        "worst_case_total_spend_units": worst_case_total_spend,
        "assumptions": assumptions,
        "formula": CAP_TRIP_FORMULA,
        "cap_trip_formula": CAP_TRIP_FORMULA,
    }
    if tripped and code and why:
        rec["refusal"] = {"code": code, "why": why}
    return rec


def evaluate_cap_guard(
    *,
    mode: str = "live",
    cap: float = 0.05,
    margin: float = 0.01,
    cleanup_bound_s: float = 0.0,
    start_balance: float | None = None,
    current_balance: float | None = None,
    rate_ccu_per_hour: float = 0.0,
    elapsed_s: float = 0.0,
    usage_parse_error: str | None = None,
    phase: str | None = None,
    checkpoint: str | None = None,
) -> dict[str, Any]:
    trip = compute_cap_trip(
        cap=cap, margin=margin, cleanup_bound_s=cleanup_bound_s, priced_rate=rate_ccu_per_hour
    )
    ceiling = trip["ceiling_ccu"]
    extra: dict[str, Any] = {
        "cap_ccu": trip["cap_ccu"],
        "margin_ccu": trip["margin_ccu"],
        "cleanup_bound_s": trip["cleanup_bound_s"],
        "priced_rate": trip["priced_rate"],
        "cleanup_reserve_units": trip["cleanup_reserve_units"],
        "effective_trip_units": trip["effective_trip_units"],
        "formula": CAP_TRIP_FORMULA,
        "cap_trip_formula": CAP_TRIP_FORMULA,
    }
    if phase is not None:
        extra["phase"] = phase
    if checkpoint is not None:
        extra["checkpoint"] = checkpoint
    if usage_parse_error:
        return {
            "mode": mode,
            "tripped": True,
            "code": "COLAB_USAGE_PARSE_FAILED",
            "spend_ccu": None,
            "ceiling_ccu": ceiling,
            **extra,
            "usage_parse_error": usage_parse_error,
        }
    if (start_balance is not None and float(start_balance) < 0.0) or (
        current_balance is not None and float(current_balance) < 0.0
    ):
        return {
            "mode": mode,
            "tripped": True,
            "code": "COLAB_USAGE_PARSE_FAILED",
            "spend_ccu": None,
            "ceiling_ccu": ceiling,
            **extra,
            "usage_parse_error": "COLAB_USAGE_PARSE_FAILED: negative balance is impossible",
        }
    if mode not in ("fake", "dry_run", "offline") and (start_balance is None or current_balance is None):
        return {
            "mode": mode,
            "tripped": True,
            "code": "COLAB_USAGE_PARSE_FAILED",
            "spend_ccu": None,
            "ceiling_ccu": ceiling,
            **extra,
            "usage_parse_error": "COLAB_USAGE_PARSE_FAILED: missing start_balance or current_balance in non-fake mode",
        }
    drop = (
        round(max(0.0, float(start_balance) - float(current_balance)), 6)
        if start_balance is not None and current_balance is not None
        else 0.0
    )
    rate_est = round(max(0.0, (float(rate_ccu_per_hour) * float(elapsed_s)) / 3600.0), 6)
    spend = round(max(drop, rate_est), 6)
    tripped = spend >= ceiling
    return {
        "mode": mode,
        "tripped": tripped,
        "code": "CAP_GUARD_TRIPPED" if tripped else None,
        "spend_ccu": spend,
        "ceiling_ccu": ceiling,
        **extra,
    }
