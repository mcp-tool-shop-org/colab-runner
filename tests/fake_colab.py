"""A fake `colab` CLI for the tests. Subcommands: exec -s S -f FILE [--timeout N], download -s S REMOTE LOCAL,
stop -s S, restart-kernel -s S, usage. An exec runs the cell file in a subprocess on this machine (the cell sets its
own environment from its first lines; the "VM" is a local directory the cells are pointed at with CR_CONTENT).

Faults come from the JSON file named by FAKE_COLAB_FAULTS, counted per subcommand (1-based call numbers):
  {"lose_reply": {"exec": [1]}}    run the call, then fail with rc 1 and no output ("Connection was lost")
  {"drop_call": {"exec": [2, 3]}}  do not run the call at all, fail with rc 1
  {"hang": {"exec": [4]}}          sleep past any timeout
  {"fail": {"stop": [1]}}          stop fails with rc 1
The call counters live in FAKE_COLAB_STATE (a directory), with every argv in argv.log (one JSON list per line).
FAKE_COLAB_LEAK, when set, is printed on stderr in a colab-runtime-proxy-token URL on every call, as the real CLI's
transfer errors do."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

USAGE = "Current balance: 100.00 compute units\nUsage rate: 0.26 / hr\nActive assignments: 1\n"


def _faults():
    p = os.environ.get("FAKE_COLAB_FAULTS")
    return json.loads(Path(p).read_text()) if p and Path(p).is_file() else {}


def _count(cmd, argv):
    state = Path(os.environ["FAKE_COLAB_STATE"])
    state.mkdir(parents=True, exist_ok=True)
    f = state / (cmd + ".count")
    n = int(f.read_text()) + 1 if f.exists() else 1
    f.write_text(str(n))
    with (state / "calls.log").open("a") as fh:
        fh.write("%s %d\n" % (cmd, n))
    with (state / "argv.log").open("a") as fh:
        fh.write(json.dumps(argv) + "\n")
    return n


def main(argv):
    cmd = argv[0]
    n = _count(cmd, argv)
    faults = _faults()
    leak = os.environ.get("FAKE_COLAB_LEAK")
    if leak:
        print("warning: https://colab.example/x?colab-runtime-proxy-token=%s&authuser=0" % leak, file=sys.stderr)

    def hit(kind):
        return n in faults.get(kind, {}).get(cmd, [])

    if hit("hang"):
        time.sleep(600)
    if hit("drop_call"):
        print("RuntimeError: Connection was lost", file=sys.stderr)
        return 1
    if cmd == "exec":
        if "--env" in argv:
            print("fake colab: --env is not used; the cell sets its own environment", file=sys.stderr)
            return 2
        cell = argv[argv.index("-f") + 1]
        proc = subprocess.run([sys.executable, "-c", Path(cell).read_text()], capture_output=True, text=True)
        if hit("lose_reply"):
            print("RuntimeError: Connection was lost", file=sys.stderr)
            return 1
        sys.stdout.write(proc.stdout)
        sys.stderr.write(proc.stderr)
        return proc.returncode
    if cmd == "download":
        remote, local = argv[-2], argv[-1]
        src = Path(os.environ.get("FAKE_COLAB_VM", "/content")) / Path(remote).name
        if not src.is_file():
            print("no such file: %s" % remote, file=sys.stderr)
            return 1
        Path(local).write_bytes(src.read_bytes())
        return 0
    if cmd == "stop":
        if hit("fail"):
            print("stop failed", file=sys.stderr)
            return 1
        return 0
    if cmd == "restart-kernel":
        return 0
    if cmd == "usage":
        sys.stdout.write(USAGE)
        return 0
    print("unknown subcommand %s" % cmd, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
