"""Continuous work engine: unattended multi-job queue and four-way outcome buckets."""

from __future__ import annotations

import json
import os
import re
import time
import tomllib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from colab_runner.cards import validate_card_and_shape
from colab_runner.colab_cli import Colab, cli_from_env, scrub
from colab_runner.errors import Refused
from colab_runner.manifest import WindowSpec, _reject_unknown_keys
from colab_runner.runner import Runner, write_json_atomic

OUTCOME_BUCKETS = ("done", "failed-ours", "failed-Google", "refused-capacity")
QUEUE_JOB_ID_RE = re.compile(r"[A-Za-z0-9_-]+")


def _utc_now() -> str:
    return datetime.now(timezone.utc).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def classify_window_outcome(outcome: Mapping[str, Any]) -> str:
    """Classify any finished window outcome into `done`, `failed-ours`, `failed-Google`, or `refused-capacity`."""
    status = str(outcome.get("status") or "")
    if status == "done":
        return "done"
    refusal = outcome.get("refusal") or {}
    code = str(refusal.get("code") or "")
    msg = str(refusal.get("message") or "").lower()
    opened = bool(outcome.get("opened"))
    if not opened and (
        code == "POOL_CEILING_REACHED"
        or (code == "SESSION_OPEN_FAILED" and any(
            k in msg for k in ("capacity", "unavailable", "503", "412", "exhausted", "quota", "cannot assign", "refused")
        ))
    ):
        return "refused-capacity"
    if status in ("unreachable", "shape_mismatch", "card_mismatch") or (
        code in ("SESSION_OPEN_FAILED", "UPLOAD_FAILED", "SESSIONS_READ_FAILED")
        and any(k in msg for k in ("500", "502", "503", "504", "service", "internal error", "connection", "reset", "timed out", "timeout"))
    ):
        return "failed-Google"
    return "failed-ours"


@dataclass(frozen=True)
class QueueJobSpec:
    """One queued Colab window with validated purpose, line of work, cap, wall, and output directory."""

    job_id: str
    spec_path: Path
    spec: WindowSpec
    purpose: str
    line: str
    card: str
    shape: Optional[str]
    max_rate: float
    cap: float
    wall_s: float
    out_dir: Path
    env: tuple[str, ...] = ()
    allow_sealed: bool = False


@dataclass(frozen=True)
class QueueSpec:
    """Validated file-backed multi-job queue ready for unattended execution."""

    queue_path: Path
    out_root: Path
    state_dir: Optional[Path]
    default_line: str
    jobs: tuple[QueueJobSpec, ...]

    @classmethod
    def from_file(cls, path: Path | str) -> "QueueSpec":
        """Load and validate a `.toml` or `.json` queue manifest, enforcing purpose and single-card-per-study rules."""
        qp = Path(path)
        if not qp.is_file():
            raise Refused("SPEC_NOT_FOUND", "queue spec does not exist: %s" % qp)
        raw_text = qp.read_text(encoding="utf-8")
        try:
            data = json.loads(raw_text) if qp.suffix.lower() == ".json" else tomllib.loads(raw_text)
        except (ValueError, tomllib.TOMLDecodeError) as exc:
            raise Refused("SPEC_INVALID", "could not parse queue %s (%s)" % (qp, exc)) from None
        if not isinstance(data, dict) or data.get("schema_version", 1) != 1:
            raise Refused("SPEC_INVALID", "queue manifest must have schema_version = 1")
        if "publish" in data:
            raise Refused("SPEC_INVALID", "[publish] was removed; delete it; publish with `hf upload`")
        _reject_unknown_keys(
            data,
            frozenset({"schema_version", "queue", "out_root", "state_dir", "line", "jobs"}),
            "queue top-level",
        )
        qtbl = data.get("queue", {})
        if not isinstance(qtbl, Mapping):
            raise Refused("SPEC_INVALID", "queue section [queue] must be a table/object")
        _reject_unknown_keys(qtbl, frozenset({"out_root", "state_dir", "line"}), "[queue]")
        base_dir = qp.resolve().parent
        raw_root = str(qtbl.get("out_root") or data.get("out_root") or "queue_runs").strip()
        out_root = (base_dir / raw_root) if not Path(raw_root).is_absolute() else Path(raw_root)
        raw_state_val = qtbl.get("state_dir") if "state_dir" in qtbl else data.get("state_dir")
        if raw_state_val is not None:
            if not isinstance(raw_state_val, str) or not raw_state_val.strip():
                raise Refused("SPEC_INVALID", "queue state_dir must be a non-empty string")
            s_raw = raw_state_val.strip()
            state_dir: Optional[Path] = (
                (base_dir / s_raw) if not Path(s_raw).is_absolute() else Path(s_raw)
            ).resolve()
        else:
            state_dir = None
        default_line = str(qtbl.get("line") or data.get("line") or "").strip()

        raw_jobs = data.get("jobs")
        if not isinstance(raw_jobs, list) or not raw_jobs:
            raise Refused("SPEC_INVALID", "queue manifest must declare at least one [[jobs]] entry")

        jobs: list[QueueJobSpec] = []
        seen_ids: set[str] = set()
        seen_dirs: set[Path] = set()
        for idx, entry in enumerate(raw_jobs):
            if not isinstance(entry, Mapping):
                raise Refused("SPEC_INVALID", "jobs[%d] must be a table/object" % idx)
            _reject_unknown_keys(
                entry,
                frozenset({
                    "id", "spec", "purpose", "line", "card", "shape",
                    "max_rate", "cap", "wall_s", "out_dir", "env", "allow_sealed",
                }),
                "jobs[%d]" % idx,
            )
            job_id = str(entry.get("id") or ("job-%d" % (idx + 1))).strip()
            if not QUEUE_JOB_ID_RE.fullmatch(job_id) or job_id in seen_ids:
                raise Refused("SPEC_INVALID", "jobs[%d].id must be a unique [A-Za-z0-9_-]+ name" % idx)
            seen_ids.add(job_id)

            raw_spec = entry.get("spec")
            if not isinstance(raw_spec, str) or not raw_spec.strip():
                raise Refused("SPEC_INVALID", "jobs[%d].spec is required" % idx)
            sp = (base_dir / raw_spec.strip()) if not Path(raw_spec.strip()).is_absolute() else Path(raw_spec.strip())
            spec = WindowSpec.from_file(sp)

            purpose = str(entry.get("purpose", "")).strip() or spec.purpose
            if not purpose:
                raise Refused("SPEC_INVALID", "queued job %r requires a non-empty stated purpose" % job_id)
            line = str(entry.get("line", "")).strip() or default_line or spec.line
            if not line:
                raise Refused("SPEC_INVALID", "queued job %r requires a non-empty line of work" % job_id)

            raw_card = str(entry.get("card") or spec.card)
            raw_shape = entry["shape"] if "shape" in entry else spec.shape
            shape_in = None if raw_shape is None else str(raw_shape).strip().lower() or None
            card_spec, clean_shape = validate_card_and_shape(raw_card, shape_in)

            eff_cap = float(entry.get("cap") if entry.get("cap") is not None else spec.cap)
            eff_rate = float(entry.get("max_rate") if entry.get("max_rate") is not None else (spec.max_rate or 0.0))
            eff_wall = float(entry.get("wall_s") if entry.get("wall_s") is not None else spec.wall_s)
            if eff_cap <= 0 or eff_rate <= 0 or eff_wall <= 0:
                raise Refused("SPEC_INVALID", "queued job %r cap, max_rate, and wall_s must be > 0" % job_id)

            raw_out = entry.get("out_dir")
            out_dir = (
                ((base_dir / str(raw_out).strip()) if not Path(str(raw_out).strip()).is_absolute() else Path(str(raw_out).strip()))
                if raw_out
                else (out_root / job_id)
            ).resolve()
            if out_dir in seen_dirs:
                raise Refused("OUT_DIR_INVALID", "queued job %r duplicates out_dir %s" % (job_id, out_dir))
            seen_dirs.add(out_dir)

            raw_env = entry.get("env") or ()
            if not isinstance(raw_env, (list, tuple)) or not all(isinstance(x, str) for x in raw_env):
                raise Refused("SPEC_INVALID", "jobs[%d].env must be a list of strings" % idx)

            jobs.append(QueueJobSpec(
                job_id=job_id,
                spec_path=sp.resolve(),
                spec=spec,
                purpose=purpose,
                line=line,
                card=card_spec.name,
                shape=clean_shape,
                max_rate=eff_rate,
                cap=eff_cap,
                wall_s=eff_wall,
                out_dir=out_dir,
                env=tuple(raw_env),
                allow_sealed=bool(entry.get("allow_sealed", False)),
            ))

        return cls(
            queue_path=qp.resolve(),
            out_root=out_root.resolve(),
            state_dir=state_dir,
            default_line=default_line,
            jobs=tuple(jobs),
        )


def run_queue(
    queue_path: Path | str,
    *,
    local_cpu: bool = False,
    dry: bool = False,
    state_dir: Optional[Path | str] = None,
    colab: Optional[Colab] = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    log: Callable[[str], None] = lambda s: None,
    trace: Callable[[str], None] = lambda s: None,
) -> dict[str, Any]:
    """Run all jobs in `queue_path` unattended: isolate failures, enforce pool limits via launch, and record spend."""
    from colab_runner.launcher import (
        _default_open_lock_path, _default_user_state_dir, _load_opt_json, launch_window,
        reconcile_stopped_outcome, verify_pending_stop,
    )

    q = QueueSpec.from_file(queue_path)
    explicit_state = Path(state_dir).resolve() if state_dir is not None else q.state_dir
    if explicit_state is not None:
        eff_state_dir = explicit_state
        open_lock_path = eff_state_dir / "open.lock"
    else:
        env_state = os.environ.get("COLAB_RUNNER_STATE_DIR", "").strip()
        eff_state_dir = Path(env_state).resolve() if env_state else _default_user_state_dir()
        open_lock_path = _default_open_lock_path(None)
    eff_state_dir.mkdir(parents=True, exist_ok=True)
    q.out_root.mkdir(parents=True, exist_ok=True)
    state_file = q.out_root / "queue_state.json"

    records: list[dict[str, Any]] = [
        {
            "job_id": j.job_id,
            "spec_path": str(j.spec_path),
            "out_dir": str(j.out_dir),
            "line": j.line,
            "purpose": j.purpose,
            "card": "CPU" if local_cpu else j.card,
            "shape": None if local_cpu else j.shape,
            "cap_ccu": j.cap,
            "max_rate": j.max_rate,
            "wall_s": j.wall_s,
            "state": "queued",
            "status": None,
            "outcome_bucket": None,
            "spend_ccu": 0.0,
            "session_stopped_confirmed": None,
            "error": None,
        }
        for j in q.jobs
    ]
    queue_state: dict[str, Any] = {
        "schema_version": 1,
        "queue_path": str(q.queue_path),
        "out_root": str(q.out_root),
        "state_dir": str(eff_state_dir),
        "mode": "dry_run" if dry else ("local_cpu" if local_cpu else "live"),
        "started_at": _utc_now(),
        "finished_at": None,
        "halted_reason": None,
        "jobs": records,
    }
    write_json_atomic(state_file, queue_state)

    live_mode = not dry and not local_cpu
    client = colab if colab is not None else (cli_from_env() if live_mode else None)

    for idx, job in enumerate(q.jobs):
        rec = records[idx]
        rec["state"] = "running"
        write_json_atomic(state_file, queue_state)

        outcome: Optional[dict[str, Any]] = None
        err_msg: Optional[str] = None
        try:
            outcome = launch_window(
                job.spec_path,
                job.out_dir,
                card=job.card,
                shape=job.shape,
                cap=job.cap,
                max_rate=job.max_rate,
                wall_s=job.wall_s,
                extra_env=job.env,
                dry=dry,
                local_cpu=local_cpu,
                allow_sealed=job.allow_sealed,
                state_dir=eff_state_dir,
                open_lock=open_lock_path,
                purpose=job.purpose,
                line=job.line,
                colab=client,
                clock=clock,
                sleep=sleep,
                log=log,
                trace=trace,
            )
        except Refused as exc:
            if exc.code == "PLATFORM_UNSUPPORTED":
                raise
            err_msg = "%s: %s" % (exc.code, scrub(exc.message))
            rec["status"] = "refused"
            rec["outcome_bucket"] = classify_window_outcome({
                "status": "refused",
                "opened": False,
                "refusal": {"code": exc.code, "message": exc.message},
            })
            rec["session_stopped_confirmed"] = True
        except Exception as exc:
            err_msg = "%s: %s" % (type(exc).__name__, scrub(str(exc)))
            outcome_file = job.out_dir / "outcome.json"
            if outcome_file.is_file():
                try:
                    outcome = json.loads(outcome_file.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    outcome = None

        if outcome is not None:
            if dry:
                rec["state"] = "dry_ok"
                rec["status"] = "dry_run"
                rec["outcome_bucket"] = "done"
                rec["session_stopped_confirmed"] = True
            else:
                rec["state"] = "finished"
                rec["status"] = outcome.get("status")
                rec["outcome_bucket"] = outcome.get("outcome_bucket") or classify_window_outcome(outcome)
                rec["spend_ccu"] = float(outcome.get("spend_ccu") or 0.0)
                rec["session_stopped_confirmed"] = bool(outcome.get("session_stopped_confirmed"))
                if not outcome.get("session_stopped_confirmed") and outcome.get("refusal"):
                    err_msg = err_msg or outcome["refusal"].get("message")
        else:
            rec["state"] = "finished"
            if rec["status"] is None:
                rec["status"] = "error"
            if rec["outcome_bucket"] is None:
                rec["outcome_bucket"] = "failed-ours"

        marker, pend_path = job.out_dir / "session_open.json", job.out_dir / "stop_pending.json"
        if not dry and (marker.is_file() or pend_path.is_file()):
            try:
                pending, saved_open = _load_opt_json(pend_path), _load_opt_json(marker)
                sess = json.loads((marker if marker.is_file() else pend_path).read_text(encoding="utf-8")).get("session")
                comp_client = client if client is not None else cli_from_env()
                vfn = (lambda d=job.out_dir, c=comp_client, s=sess, p=pending, o=saved_open: verify_pending_stop(d, c, s, p, o)) if sess and (pending or {}).get("reason") == "tpu_open_timeout_unknown" else None
                if sess and Runner(comp_client, sess, job.out_dir, log=log).stop_session(verify=vfn):
                    reconciled = reconcile_stopped_outcome(job.out_dir, comp_client, sess)
                    if reconciled is not None:
                        rec["spend_ccu"], rec["session_stopped_confirmed"] = float(reconciled.get("spend_ccu") or rec["spend_ccu"]), True
            except Exception as exc:
                err_msg = err_msg or scrub(str(exc))

        if not dry and (marker.is_file() or pend_path.is_file()):
            rec["session_stopped_confirmed"] = False
            rec["error"] = err_msg or "session stop unconfirmed; halting queue"
            queue_state["halted_reason"] = "STOP_NOT_CONFIRMED on %s" % job.job_id
            write_json_atomic(state_file, queue_state)
            break

        rec["error"] = err_msg
        write_json_atomic(state_file, queue_state)

    counts = {b: 0 for b in OUTCOME_BUCKETS}
    for r in records:
        if r.get("outcome_bucket") in counts:
            counts[r["outcome_bucket"]] += 1
    all_stopped = all(
        r.get("session_stopped_confirmed") is not False and not (Path(r["out_dir"]) / "session_open.json").is_file()
        for r in records
    )
    ok = (
        queue_state["halted_reason"] is None
        and all(
            (r["status"] == "dry_run" if dry else r["status"] == "done")
            and r.get("session_stopped_confirmed") is True
            for r in records
        )
    )
    queue_state["finished_at"] = _utc_now()
    queue_state["counts"] = counts
    queue_state["spend_ccu_total"] = round(sum(float(r.get("spend_ccu") or 0.0) for r in records), 6)
    queue_state["all_stopped_confirmed"] = all_stopped
    queue_state["exit_code"] = 0 if ok else 2
    write_json_atomic(state_file, queue_state)
    return queue_state


__all__ = [
    "OUTCOME_BUCKETS",
    "QueueJobSpec",
    "QueueSpec",
    "classify_window_outcome",
    "run_queue",
]
