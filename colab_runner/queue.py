"""Continuous work engine: unattended multi-job queue, four-way outcome buckets, and outcome-scanning board."""

from __future__ import annotations

import json
import re
import time
import tomllib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from colab_runner.cards import validate_card_and_shape
from colab_runner.colab_cli import Colab, cli_from_env, scrub
from colab_runner.errors import Refused
from colab_runner.manifest import WindowSpec
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
        code in ("POOL_CEILING_REACHED", "CARD_CIRCUIT_OPEN")
        or (code == "SESSION_OPEN_FAILED" and any(
            k in msg for k in ("capacity", "unavailable", "503", "412", "exhausted", "quota", "cannot assign", "refused")
        ))
    ):
        return "refused-capacity"
    if status in ("unreachable", "shape_mismatch", "card_mismatch") or (
        code in ("SESSION_OPEN_FAILED", "UPLOAD_FAILED", "SESSIONS_READ_FAILED")
        and any(k in msg for k in ("500", "502", "503", "504", "service", "internal error", "connection", "reset", "timed out"))
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
    state_dir: Path
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
        qtbl = data.get("queue", {})
        if not isinstance(qtbl, Mapping):
            raise Refused("SPEC_INVALID", "queue section [queue] must be a table/object")
        base_dir = qp.resolve().parent
        raw_root = str(qtbl.get("out_root") or data.get("out_root") or "queue_runs").strip()
        out_root = (base_dir / raw_root) if not Path(raw_root).is_absolute() else Path(raw_root)
        raw_state = str(qtbl.get("state_dir") or data.get("state_dir") or str(out_root / "_state")).strip()
        state_dir = (base_dir / raw_state) if not Path(raw_state).is_absolute() else Path(raw_state)
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
            state_dir=state_dir.resolve(),
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
    from colab_runner.launcher import launch_window, reconcile_stopped_outcome

    q = QueueSpec.from_file(queue_path)
    eff_state_dir = Path(state_dir).resolve() if state_dir is not None else q.state_dir
    eff_state_dir.mkdir(parents=True, exist_ok=True)
    q.out_root.mkdir(parents=True, exist_ok=True)
    open_lock_path = eff_state_dir / "open.lock"
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

        marker = job.out_dir / "session_open.json"
        if not dry and marker.is_file():
            try:
                sess = json.loads(marker.read_text(encoding="utf-8")).get("session")
                comp_client = client if client is not None else cli_from_env()
                if sess and Runner(comp_client, sess, job.out_dir, log=log).stop_session():
                    reconciled = reconcile_stopped_outcome(job.out_dir, comp_client, sess)
                    if reconciled is not None:
                        rec["spend_ccu"] = float(reconciled.get("spend_ccu") or rec["spend_ccu"])
                        rec["session_stopped_confirmed"] = True
            except Exception as exc:
                err_msg = err_msg or scrub(str(exc))

        if not dry and marker.is_file():
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


def build_board(
    *,
    out_root: Optional[Path | str] = None,
    out_dirs: Sequence[Path | str] = (),
    queue_state: Optional[Path | str] = None,
) -> dict[str, Any]:
    """Assemble an at-a-glance view of lines, ran/running windows, queued next items, and totals from `outcome.json`."""
    from colab_runner.launcher import _load_opt_json, status_window

    q_path = Path(queue_state) if queue_state else (Path(out_root) / "queue_state.json" if out_root else None)
    q_data = _load_opt_json(q_path) if q_path else None

    cand_dirs: list[Path] = [Path(d) for d in out_dirs]
    if out_root and Path(out_root).is_dir():
        root_p = Path(out_root)
        if any((root_p / f).is_file() for f in ("outcome.json", "window_meta.json", "dry_run_plan.json")):
            cand_dirs.append(root_p)
        for out_file in sorted(root_p.rglob("outcome.json")):
            if any(part.endswith(".quarantine") or part == "_local_vm_content" for part in out_file.parts):
                continue
            cand_dirs.append(out_file.parent)
        for child in sorted(root_p.iterdir()):
            if child.is_dir() and any((child / f).is_file() for f in ("window_meta.json", "dry_run_plan.json")):
                cand_dirs.append(child)
    for j in (q_data or {}).get("jobs", []):
        if j.get("out_dir"):
            cand_dirs.append(Path(str(j["out_dir"])))

    seen_dirs: set[Path] = set()
    windows: list[dict[str, Any]] = []
    for d in cand_dirs:
        rd = d.resolve()
        if rd in seen_dirs or not rd.is_dir():
            continue
        seen_dirs.add(rd)
        st = status_window(rd)
        out_obj = st.get("outcome") or {}
        meta = _load_opt_json(rd / "window_meta.json") or {}
        proj = out_obj.get("project") or meta.get("project") or {}
        windows.append({
            "out_dir": str(rd),
            "window_id": out_obj.get("window_id") or meta.get("window_id"),
            "line": out_obj.get("line") or meta.get("line") or proj.get("line") or proj.get("study") or "",
            "job": proj.get("job") or "",
            "purpose": out_obj.get("purpose") or meta.get("purpose") or proj.get("purpose") or proj.get("description") or "",
            "card": st.get("card"),
            "state": st.get("state"),
            "status": st.get("status") or st.get("state"),
            "outcome_bucket": out_obj.get("outcome_bucket") or (classify_window_outcome(out_obj) if out_obj else None),
            "spend_ccu": float(st.get("spend_ccu") or 0.0),
            "session_stopped_confirmed": st.get("session_stopped_confirmed"),
            "collected_outputs": list(out_obj.get("collected_outputs") or []),
        })

    grouped: dict[str, list[dict[str, Any]]] = {}
    seen_wids: set[str] = set()
    for w in windows:
        wid = str(w.get("window_id") or "")
        if wid and wid in seen_wids:
            continue
        if wid:
            seen_wids.add(wid)
        if w.get("outcome_bucket") is None:
            continue
        line_name = str(w.get("line") or "").strip() or "default"
        grouped.setdefault(line_name, []).append(w)

    lines: dict[str, dict[str, Any]] = {}
    for line_name in sorted(grouped):
        lwins = grouped[line_name]
        lcounts = {b: 0 for b in OUTCOME_BUCKETS}
        for w in lwins:
            b = str(w.get("outcome_bucket") or "failed-ours")
            lcounts[b if b in lcounts else "failed-ours"] += 1
        lines[line_name] = {
            "line": line_name,
            "spent_ccu": round(sum(float(w.get("spend_ccu") or 0.0) for w in lwins), 6),
            "window_count": len(lwins),
            "counts": lcounts,
        }

    next_jobs = [
        j for j in (q_data or {}).get("jobs", [])
        if j.get("state") in ("queued", "running")
    ]
    counts = {b: 0 for b in OUTCOME_BUCKETS}
    for w in windows:
        b = w.get("outcome_bucket")
        if b in counts:
            counts[b] += 1
    totals = {
        "windows": len(windows),
        "spend_ccu": round(sum(float(w.get("spend_ccu") or 0.0) for w in windows), 6),
        "counts": counts,
        "all_stopped_confirmed": all(w.get("session_stopped_confirmed") is True for w in windows) if windows else True,
    }
    return {"lines": lines, "windows": windows, "next": next_jobs, "totals": totals}


def format_board(board: Mapping[str, Any]) -> str:
    """Format `build_board()` as a concise, at-a-glance status report."""
    lines_out = ["LINES"]
    lines_map = board.get("lines") or {}
    if not lines_map:
        lines_out.append("  (none)")
    for name, info in lines_map.items():
        c = info["counts"]
        lines_out.append(
            "  %s  spent=%.6f  windows=%d  done=%d failed-ours=%d failed-Google=%d refused-capacity=%d"
            % (name, float(info["spent_ccu"]), info["window_count"],
               c["done"], c["failed-ours"], c["failed-Google"], c["refused-capacity"])
        )

    lines_out.append("WINDOWS")
    wins = board.get("windows") or []
    if not wins:
        lines_out.append("  (none)")
    for w in wins:
        produced = ",".join(w.get("collected_outputs") or []) or "-"
        lines_out.append(
            "  %s  line=%s  card=%s  status=%s(%s)  spend=%.6f  stopped=%s  produced=%s  purpose=%s"
            % (w.get("job") or w.get("out_dir"), w.get("line"), w.get("card"), w.get("status"),
               w.get("outcome_bucket") or "-", float(w.get("spend_ccu") or 0.0),
               w.get("session_stopped_confirmed"), produced, w.get("purpose") or "-")
        )

    lines_out.append("NEXT IN QUEUE")
    nxt = board.get("next") or []
    if not nxt:
        lines_out.append("  (none)")
    for j in nxt:
        lines_out.append(
            "  %s  state=%s  line=%s  card=%s  cap=%s  purpose=%s"
            % (j.get("job_id"), j.get("state"), j.get("line"), j.get("card"), j.get("cap_ccu"), j.get("purpose"))
        )

    tot = board["totals"]
    c = tot["counts"]
    lines_out.append(
        "TOTALS  windows=%d  spend_ccu=%.6f  done=%d  failed-ours=%d  failed-Google=%d  refused-capacity=%d  all_stopped=%s"
        % (tot["windows"], float(tot["spend_ccu"]), c["done"], c["failed-ours"], c["failed-Google"],
           c["refused-capacity"], tot["all_stopped_confirmed"])
    )
    return "\n".join(lines_out)


__all__ = [
    "OUTCOME_BUCKETS",
    "QueueJobSpec",
    "QueueSpec",
    "build_board",
    "classify_window_outcome",
    "format_board",
    "run_queue",
]
