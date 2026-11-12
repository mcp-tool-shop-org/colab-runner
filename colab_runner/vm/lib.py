# Shared VM-side helpers. The runner prepends this file to every kernel cell (start, poll, resident), so each cell is
# one self-contained source for `colab exec -f` (an IPython cell: no __file__, no argv, no `if __name__`).
# Standard library only. Environment in (all optional except where noted):
#   CR_CONTENT          the VM working dir, default /content
#   CR_JOB              the job name, default "job"; files are <CR_CONTENT>/<job>.pid, .status.json, .run.out
#   CR_OUT_DIR          where the kit writes phase.txt, remote_step.txt, setup.log, progress files (default <job>_out)
#   CR_MODE             "script" (run CR_SCRIPT with bash) or "hold" (a heartbeat-only job for CR_HOLD_S seconds)
#   CR_SCRIPT           the bash script to run in script mode
#   CR_HEARTBEAT_S      hold mode: seconds between heartbeat lines, default 30
#   CR_PROGRESS_GLOB    files whose first integer is summed into the kit's progress counter
#                       (polyglot: SEGMENTS_DONE counters); default <out>/progress/*
#   CR_UNITS_GLOB       the count of files matching this glob is the kit's finished-units counter
#                       (polyglot: one summary per finished README); default none
#   CR_POLL_LOG_OFFSET  poll cell: byte offset of the next chunk of the job log
import glob
import json
import os
import re
import subprocess
import time

CR_CONTENT = os.environ.get("CR_CONTENT", "/content")
CR_JOB = os.environ.get("CR_JOB", "job")
CR_PID_FILE = os.path.join(CR_CONTENT, CR_JOB + ".pid")
CR_STATUS_FILE = os.path.join(CR_CONTENT, CR_JOB + ".status.json")
CR_RUN_LOG = os.path.join(CR_CONTENT, CR_JOB + ".run.out")
CR_POLL_FILE = os.path.join(CR_CONTENT, CR_JOB + ".poll.json")
CR_OUT = os.environ.get("CR_OUT_DIR") or os.path.join(CR_CONTENT, CR_JOB + "_out")
CR_HB_FILE = os.path.join(CR_OUT, "hold_heartbeat.txt")
CR_MAX_LOG = 65536
CR_TOKEN_RE = re.compile(r"\bhf_[A-Za-z0-9]{16,}\b")

# The wrapper runs the job, then writes the status file (rc, end time) atomically, after the job has ended.
CR_WRAPPER = (
    'log="$1"; st="$2"; shift 2; "$@" >> "$log" 2>&1; rc=$?; '
    'printf \'{"rc": %s, "ended_at": "%s"}\\n\' "$rc" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$st.tmp" && mv "$st.tmp" "$st"'
)

# A heartbeat-only job: one line per interval into the heartbeat file, nothing else. Used to measure whether a VM
# survives while the kernel is idle between polls.
CR_HOLD_SRC = (
    "import os, sys, time\n"
    "out, secs, every = sys.argv[1], float(sys.argv[2]), float(sys.argv[3])\n"
    "os.makedirs(out, exist_ok=True)\n"
    "end = time.time() + secs\n"
    "while True:\n"
    "    with open(os.path.join(out, 'hold_heartbeat.txt'), 'a') as fh:\n"
    "        fh.write('%.3f\\n' % time.time())\n"
    "    left = end - time.time()\n"
    "    if left <= 0:\n"
    "        break\n"
    "    time.sleep(min(every, left))\n"
)


def cr_read(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read().strip()
    except OSError:
        return None


def cr_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        with open("/proc/%d/stat" % pid) as fh:
            return fh.read().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return True


def cr_status():
    raw = cr_read(CR_STATUS_FILE)
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


def cr_pid():
    raw = cr_read(CR_PID_FILE)
    return int(raw) if raw and raw.isdigit() else None


def cr_first_int(path):
    raw = cr_read(path)
    m = re.search(r"-?\d+", raw or "")
    return int(m.group(0)) if m else 0


def cr_progress():
    pattern = os.environ.get("CR_PROGRESS_GLOB") or os.path.join(CR_OUT, "progress", "*")
    return sum(cr_first_int(p) for p in sorted(glob.glob(pattern)) if os.path.isfile(p))


def cr_units():
    pattern = os.environ.get("CR_UNITS_GLOB")
    return len([p for p in glob.glob(pattern) if os.path.isfile(p)]) if pattern else 0


def cr_start_job():
    """Start the job as its own session; idempotent. Returns the line to print."""
    existing = cr_pid()
    if existing is not None and (cr_alive(existing) or os.path.exists(CR_STATUS_FILE)):
        return "CR_ALREADY_RUNNING pid=%d" % existing
    for p in (CR_STATUS_FILE, CR_RUN_LOG, CR_POLL_FILE, CR_HB_FILE):
        if os.path.exists(p):
            os.remove(p)
    mode = os.environ.get("CR_MODE", "script")
    if mode == "hold":
        argv = [
            "python3", "-c", CR_HOLD_SRC, CR_OUT,
            os.environ.get("CR_HOLD_S", "60"), os.environ.get("CR_HEARTBEAT_S", "30"),
        ]
    else:
        argv = ["bash", os.environ["CR_SCRIPT"]]
    proc = subprocess.Popen(
        ["nohup", "bash", "-c", CR_WRAPPER, "crwrap", CR_RUN_LOG, CR_STATUS_FILE] + argv,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        env=dict(os.environ), start_new_session=True, close_fds=True,
    )
    with open(CR_PID_FILE + ".tmp", "w") as fh:
        fh.write("%d\n" % proc.pid)
    os.replace(CR_PID_FILE + ".tmp", CR_PID_FILE)
    return "CR_STARTED pid=%d" % proc.pid


def cr_collect(offset):
    """One poll record plus the next chunk of the job log. Reads only; never touches the job."""
    pid = cr_pid()
    status = cr_status()
    alive = bool(pid is not None and cr_alive(pid))
    if status is None and not alive:
        status = cr_status()  # the job may have finished between the two reads
    try:
        setup_bytes = os.stat(os.path.join(CR_OUT, "setup.log")).st_size
    except OSError:
        setup_bytes = 0
    try:
        hb_mtime = os.stat(CR_HB_FILE).st_mtime
        with open(CR_HB_FILE) as fh:
            hb_beats = sum(1 for ln in fh if ln.strip())
    except OSError:
        hb_mtime, hb_beats = None, 0
    lines, new_offset, eof = [], offset, True
    try:
        size = os.stat(CR_RUN_LOG).st_size
        if offset > size:
            offset = 0
        with open(CR_RUN_LOG, "rb") as fh:
            fh.seek(offset)
            blob = fh.read(CR_MAX_LOG)
        cut = blob.rfind(b"\n") + 1 if len(blob) == CR_MAX_LOG else len(blob)
        blob = blob[:cut]
        new_offset = offset + len(blob)
        eof = new_offset >= size
        lines = [CR_TOKEN_RE.sub("hf_[REDACTED]", ln) for ln in blob.decode("utf-8", errors="replace").splitlines()]
    except OSError:
        pass
    rec = {
        "job": CR_JOB,
        "pid": pid,
        "alive": alive,
        "rc": None if status is None else status.get("rc"),
        "ended_at": None if status is None else status.get("ended_at"),
        "phase": cr_read(os.path.join(CR_OUT, "phase.txt")),
        "step": cr_read(os.path.join(CR_OUT, "remote_step.txt")),
        "setup_bytes": setup_bytes,
        "progress": cr_progress(),
        "units_done": cr_units(),
        "hold_beats": hb_beats,
        "hold_heartbeat_epoch": hb_mtime,
        "now_epoch": time.time(),
        "boot_id": cr_read("/proc/sys/kernel/random/boot_id"),
        "log_offset": new_offset,
        "log_eof": eof,
    }
    return rec, lines
