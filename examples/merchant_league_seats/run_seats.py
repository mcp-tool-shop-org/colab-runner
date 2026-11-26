"""Worked example driver for merchant-league local OpenAI-compatible seats.

Supports three modes:
  - validate-setup: check staged inputs against league-relay's local seat invariants;
  - cpu-stub: run a keyless loopback OpenAI-compatible mock server (`127.0.0.1:0`) implementing
    `POST /tokenize` and `POST /v1/chat/completions`, dispatch all 4 synthetic guild seats under
    `--cap-gpu-secs`, update `$CR_OUT_DIR/progress/seats_completed`, and write outputs;
  - gpu-live: dispatch the same 4 guild turns against the VM's local OpenAI-compatible endpoint.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

LOCAL_TIMEOUT_MARGIN_SECS = 0.05


def _load_seats(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    seats = data["seats"] if isinstance(data, dict) else data
    if not isinstance(seats, list) or not seats:
        raise ValueError("seats.json must contain a non-empty seat list")
    return seats


def validate_seats(work_dir: Path) -> list[dict]:
    """Verify synthetic local-seat invariants mirroring merchant-league `seats.rs`."""
    for name in ("seats.json", "wrapper.md", "response_schema.json"):
        if not (work_dir / name).is_file():
            raise FileNotFoundError("missing staged input: %s" % name)
    seats = _load_seats(work_dir / "seats.json")
    for seat in seats:
        if seat.get("backend") != "local":
            raise ValueError("example expects backend='local', got %r" % seat.get("backend"))
        parsed = urllib.parse.urlparse(str(seat.get("base_url", "")))
        if parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost", "::1"):
            raise ValueError("local seat %s must target loopback HTTP, got %r" % (seat.get("guild"), seat.get("base_url")))
        t_sec = float(seat["timeout_secs"])
        cap_sec = float(seat["max_gpu_secs_per_call"])
        if t_sec + LOCAL_TIMEOUT_MARGIN_SECS > cap_sec + 1e-9:
            raise ValueError(
                "seat %s max_gpu_secs_per_call (%s) < timeout_secs (%s) + %.2f"
                % (seat.get("guild"), cap_sec, t_sec, LOCAL_TIMEOUT_MARGIN_SECS)
            )
    return seats


class _MockLocalOpenAiHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args) -> None:
        pass

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length).decode("utf-8")
        return json.loads(body) if body else {}

    def _send_json(self, status: int, payload: dict) -> None:
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self) -> None:  # noqa: N802
        if self.headers.get("Authorization"):
            self._send_json(400, {"error": "local backend refuses Authorization headers"})
            return
        req = self._read_json()
        if self.path.endswith("/tokenize"):
            text = str(req.get("prompt", ""))
            tokens = max(1, len(text.split()))
            self._send_json(200, {"count": tokens, "tokens": list(range(tokens))})
            return
        if self.path.endswith("/chat/completions"):
            meta = req.get("metadata") or {}
            guild = str(meta.get("guild") or "Unknown")
            reply_obj = {
                "guild": guild,
                "bid_units": 12,
                "ask_price": 4.5,
                "rationale": f"Synthetic CPU stub turn response for {guild}.",
            }
            if meta.get("note"):
                reply_obj["note"] = str(meta["note"])
            self._send_json(
                200,
                {
                    "id": f"chatcmpl-stub-{guild.lower()}",
                    "model": req.get("model", "stub-model"),
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": json.dumps(reply_obj)},
                            "finish_reason": "stop",
                        }
                    ],
                },
            )
            return
        self._send_json(404, {"error": f"unknown endpoint: {self.path}"})


def _post_json(url: str, payload: dict, timeout_s: float) -> dict:
    raw = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=raw, method="POST", headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _write_progress(out_dir: Path, count: int) -> None:
    prog_dir = out_dir / "progress"
    prog_dir.mkdir(parents=True, exist_ok=True)
    tmp = prog_dir / "seats_completed.tmp"
    tmp.write_text(f"{count}\n", encoding="utf-8")
    os.replace(tmp, prog_dir / "seats_completed")


def run_turns(work_dir: Path, out_dir: Path, *, stub_server: bool, cap_gpu_secs: float) -> dict:
    seats = validate_seats(work_dir)
    wrapper = (work_dir / "wrapper.md").read_text(encoding="utf-8")
    server: ThreadingHTTPServer | None = None
    thread: threading.Thread | None = None
    override_base: str | None = None
    if stub_server:
        server = ThreadingHTTPServer(("127.0.0.1", 0), _MockLocalOpenAiHandler)
        port = server.server_address[1]
        override_base = f"http://127.0.0.1:{port}/v1"
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

    from_seat_dir = work_dir / "from-seat"
    from_seat_dir.mkdir(parents=True, exist_ok=True)
    total_gpu_secs = 0.0
    seat_records = []
    try:
        for idx, seat in enumerate(seats, start=1):
            guild = seat["guild"]
            base_url = (override_base or str(seat["base_url"])).rstrip("/")
            timeout_s = float(seat["timeout_secs"])
            cap_call = float(seat["max_gpu_secs_per_call"])
            if total_gpu_secs + cap_call > cap_gpu_secs + 1e-9:
                raise RuntimeError("GPU seconds budget exhausted before guild %s" % guild)

            prompt = f"{wrapper}\n\nGuild: {guild}\nTurn: 1\n"
            t0 = time.monotonic()
            tok = _post_json(
                f"{base_url}/tokenize",
                {"model": seat["model"], "prompt": prompt},
                timeout_s=timeout_s,
            )
            comp = _post_json(
                f"{base_url}/chat/completions",
                {
                    "model": seat["model"],
                    "temperature": seat["temperature"],
                    "max_tokens": seat["max_output_tokens"],
                    "messages": [{"role": "user", "content": prompt}],
                    "metadata": {"guild": guild, "note": str(seat.get("note") or "")},
                },
                timeout_s=timeout_s,
            )
            elapsed = min(cap_call, round(time.monotonic() - t0, 6))
            total_gpu_secs += elapsed
            content_raw = comp["choices"][0]["message"]["content"]
            parsed_reply = json.loads(content_raw)
            (from_seat_dir / f"{guild}.json").write_text(json.dumps(parsed_reply, indent=2) + "\n", encoding="utf-8")
            seat_records.append({
                "guild": guild,
                "model": seat["model"],
                "prompt_tokens": tok.get("count"),
                "charged_gpu_secs": elapsed,
                "max_gpu_secs_per_call": cap_call,
            })
            _write_progress(out_dir, idx)
            print(f"[turn] guild={guild} tokens={tok.get('count')} elapsed_s={elapsed}", flush=True)
    finally:
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None:
            thread.join(timeout=5.0)

    summary = {
        "schema_version": 1,
        "mode": "cpu-stub" if stub_server else "gpu-live",
        "cap_gpu_secs": cap_gpu_secs,
        "total_gpu_secs": round(total_gpu_secs, 6),
        "seats": seat_records,
    }
    (work_dir / "turn_summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return summary


def _probe_nvidia_smi() -> str | None:
    env_smi = (os.environ.get("CR_NVIDIA_SMI") or "").strip()
    smi = env_smi if (env_smi and os.path.isabs(env_smi) and os.path.exists(env_smi)) else shutil.which(env_smi or "nvidia-smi")
    if not smi:
        return None
    proc = subprocess.run(
        [smi, "--query-gpu=name,memory.total,compute_cap", "--format=csv,noheader,nounits"],
        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=15,
    )
    if proc.returncode == 0 and proc.stdout.strip():
        return proc.stdout.strip().splitlines()[0].strip()
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("validate-setup", "cpu-stub", "gpu-live"), required=True)
    parser.add_argument("--cap-gpu-secs", type=float, default=120.2)
    parser.add_argument("--smoke-stub", action="store_true",
                        help="run an in-process loopback OpenAI-compatible stub inside gpu-live when no model server runs")
    args = parser.parse_args()

    work_dir = Path(os.environ.get("CR_WORK_DIR") or ".").resolve()
    out_dir = Path(os.environ.get("CR_OUT_DIR") or (work_dir / "out")).resolve()
    if args.mode == "validate-setup":
        seats = validate_seats(work_dir)
        print(f"[setup] validated {len(seats)} local seats", flush=True)
        return 0
    smoke_stub = args.smoke_stub or any(
        os.environ.get(k) == "1" for k in ("ML_SMOKE_STUB", "SMOKE_STUB", "CR_SMOKE_STUB")
    )
    use_stub = args.mode == "cpu-stub" or (args.mode == "gpu-live" and smoke_stub)
    summary = run_turns(work_dir, out_dir, stub_server=use_stub, cap_gpu_secs=args.cap_gpu_secs)
    if args.mode == "gpu-live" and smoke_stub:
        summary["smoke_stub"] = True
        summary["gpu_smoke_device"] = _probe_nvidia_smi()
        (work_dir / "turn_summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
