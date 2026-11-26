# Shared VM-side helpers. The runner prepends this file to every kernel cell (start, poll, resident, kill), so each
# cell is one self-contained source for `colab exec -f` (an IPython cell: no __file__, no argv, no `if __name__`).
# Ahead of this file the runner puts the cell's environment and the vendored library's token scrub
# (TOKEN_SCRUB_PATTERNS and scrub_colab_tokens, from launch_guards.py; runner._scrub_source), so the VM scrubs with
# the library's one implementation.
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
#   CR_KILL_GRACE_S     kill cell: seconds between SIGTERM and SIGKILL to the job's process group, default 5
#   CR_RUN_ID           every cell: the unique id of the runner invocation. The start writes it to <job>.run_id
#                       before the job starts; the job's status file and every poll record carry it; a start, a
#                       resident cell or a kill for another run id never touches the files or the job (CR_JOB_EXISTS,
#                       CR_NOT_OURS); a resident refusal goes to <job>.refused.json, never to <job>.poll.json
import glob
import json
import os
import re
import signal
import subprocess
import time

CR_CONTENT = os.environ.get("CR_CONTENT", "/content")
CR_JOB = os.environ.get("CR_JOB", "job")
CR_PID_FILE = os.path.join(CR_CONTENT, CR_JOB + ".pid")
CR_STATUS_FILE = os.path.join(CR_CONTENT, CR_JOB + ".status.json")
CR_RUN_LOG = os.path.join(CR_CONTENT, CR_JOB + ".run.out")
CR_POLL_FILE = os.path.join(CR_CONTENT, CR_JOB + ".poll.json")
CR_RUN_ID_FILE = os.path.join(CR_CONTENT, CR_JOB + ".run_id")
CR_REFUSED_FILE = os.path.join(CR_CONTENT, CR_JOB + ".refused.json")
# The job's own files: if any of them exists under another run id, the job is another run's.
CR_JOB_FILES = (CR_PID_FILE, CR_STATUS_FILE, CR_RUN_LOG, CR_POLL_FILE, CR_RUN_ID_FILE)
CR_OUT = os.environ.get("CR_OUT_DIR") or os.path.join(CR_CONTENT, CR_JOB + "_out")
CR_HB_FILE = os.path.join(CR_OUT, "hold_heartbeat.txt")
CR_MAX_LOG = 65536

# The wrapper runs the job, then writes the status file (rc, end time, run id) atomically, after the job has ended.
# Its third argument is the run id already JSON-encoded (json.dumps, ASCII only); printf's %s copies it verbatim.
CR_WRAPPER = (
    'log="$1"; st="$2"; rid="$3"; shift 3; "$@" >> "$log" 2>&1; rc=$?; '
    'printf \'{"rc": %s, "ended_at": "%s", "run_id": %s}\\n\' "$rc" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$rid" '
    '> "$st.tmp" && mv "$st.tmp" "$st"'
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
    """Is our job's process alive? A PermissionError means the pid is another user's process (a recycled pid), so
    not our job: dead, not alive."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return False
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


def cr_glob_files(pattern):
    if not pattern:
        return []
    if os.path.isabs(pattern):
        cands = glob.glob(pattern)
    else:
        cands = []
        for base in (CR_CONTENT, os.path.join(CR_CONTENT, CR_JOB + "_work"), CR_OUT):
            cands.extend(glob.glob(os.path.join(base, pattern)))
    seen, out = set(), []
    for p in sorted(cands):
        if os.path.isfile(p):
            rp = os.path.realpath(p)
            if rp not in seen:
                seen.add(rp)
                out.append(p)
    return out


def cr_progress():
    pattern = os.environ.get("CR_PROGRESS_GLOB") or os.path.join(CR_OUT, "progress", "*")
    return sum(cr_first_int(p) for p in cr_glob_files(pattern))


def cr_units():
    pattern = os.environ.get("CR_UNITS_GLOB")
    return len(cr_glob_files(pattern)) if pattern else 0


def cr_run_id():
    """This cell's run id (CR_RUN_ID), "" when none was given."""
    return os.environ.get("CR_RUN_ID", "").strip()


def cr_owner():
    """The run id the job's files belong to: the <job>.run_id file, "" when it is missing or empty."""
    return cr_read(CR_RUN_ID_FILE) or ""


def cr_start_job():
    """Start the job as its own session; idempotent for its own run id. Returns the line to print.

    If any of the job's files (CR_JOB_FILES) exists under another run id, the job is another run's, whether its
    process is alive or dead and with or without a status file: the start is refused (CR_JOB_EXISTS) and nothing is
    touched. Only the same run id gets CR_ALREADY_RUNNING (a live job, or a status file) or a restart (a dead job
    with no status file: its own files are removed first)."""
    want_id = cr_run_id()
    existing = cr_pid()
    if any(os.path.exists(p) for p in CR_JOB_FILES) and cr_owner() != want_id:
        return "CR_JOB_EXISTS job=%s pid=%s" % (CR_JOB, existing if existing is not None else 0)
    if os.path.exists(CR_STATUS_FILE) or (existing is not None and cr_alive(existing)):
        return "CR_ALREADY_RUNNING pid=%s" % (existing if existing is not None else 0)
    for p in (CR_STATUS_FILE, CR_RUN_LOG, CR_POLL_FILE, CR_HB_FILE, CR_RUN_ID_FILE, CR_PID_FILE):
        if os.path.exists(p):
            os.remove(p)
    # the run id goes down before the job exists, so a job is never without its owner
    with open(CR_RUN_ID_FILE + ".tmp", "w") as fh:
        fh.write(want_id + "\n")
    os.replace(CR_RUN_ID_FILE + ".tmp", CR_RUN_ID_FILE)
    mode = os.environ.get("CR_MODE", "script")
    if mode == "hold":
        argv = [
            "python3", "-c", CR_HOLD_SRC, CR_OUT,
            os.environ.get("CR_HOLD_S", "60"), os.environ.get("CR_HEARTBEAT_S", "30"),
        ]
    else:
        argv = ["bash", os.environ["CR_SCRIPT"]]
    proc_env = dict(os.environ)
    proc_env["CR_OUT_DIR"] = CR_OUT
    proc = subprocess.Popen(
        ["nohup", "bash", "-c", CR_WRAPPER, "crwrap", CR_RUN_LOG, CR_STATUS_FILE, json.dumps(want_id)] + argv,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        env=proc_env, start_new_session=True, close_fds=True,
    )
    with open(CR_PID_FILE + ".tmp", "w") as fh:
        fh.write("%d\n" % proc.pid)
    os.replace(CR_PID_FILE + ".tmp", CR_PID_FILE)
    return "CR_STARTED pid=%d" % proc.pid


def cr_group_alive(pgid):
    try:
        os.killpg(pgid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return True


def cr_kill_job(grace_s):
    """Terminate the job's whole process group: SIGTERM, then SIGKILL after `grace_s`. cr_start_job started the job
    as its own session, so the group id is the pid in the pid file. Touches nothing else, and never another run's
    job: with a CR_RUN_ID that is not the job's owner it answers CR_NOT_OURS."""
    want_id = cr_run_id()
    if want_id and cr_owner() != want_id:
        return "CR_NOT_OURS job=%s" % CR_JOB
    pid = cr_pid()
    if pid is None:
        return "CR_NO_JOB"
    if not cr_group_alive(pid):
        return "CR_NOT_RUNNING pid=%d" % pid
    try:
        os.killpg(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return "CR_NOT_RUNNING pid=%d" % pid
    end = time.time() + grace_s
    while time.time() < end and cr_group_alive(pid):
        time.sleep(0.1)
    if cr_group_alive(pid):
        try:
            os.killpg(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    return "CR_KILLED pid=%d" % pid


def cr_whole_chars(blob):
    """len(blob), less a UTF-8 character its end splits, so a forced cut never breaks one (and is never 0)."""
    n = len(blob)
    for j in range(n - 1, max(n - 4, 0) - 1, -1):
        b = blob[j]
        if b & 0xC0 != 0x80:  # the last lead (or ASCII) byte
            need = 1 if b < 0x80 else 2 if b >> 5 == 0b110 else 3 if b >> 4 == 0b1110 else 4 if b >> 3 == 0b11110 else 1
            return j if (j + need > n and j > 0) else n
    return n


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
        if len(blob) == CR_MAX_LOG:
            # A full chunk is cut after its last line end, \n or \r (progress bars redraw with \r). A chunk with
            # neither is returned whole, as one line, cut at a character boundary: the offset always advances.
            cut = max(blob.rfind(b"\n"), blob.rfind(b"\r")) + 1
            blob = blob[:cut] if cut > 0 else blob[:cr_whole_chars(blob)]
        new_offset = offset + len(blob)
        eof = new_offset >= size
        lines = [scrub_colab_tokens(ln) for ln in blob.decode("utf-8", errors="replace").splitlines()]
    except OSError:
        pass
    rec = {
        "job": CR_JOB,
        "run_id": cr_owner(),  # the run whose files these are, never the asker's
        "status_run_id": None if status is None else status.get("run_id"),
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
