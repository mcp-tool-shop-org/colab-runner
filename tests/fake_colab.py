"""A fake `colab` CLI for the tests. Subcommands: exec -s S -f FILE [--timeout N] [--env K=V]..., download -s S REMOTE
LOCAL, stop -s S. An exec runs the cell file in a subprocess on this machine, with the --env entries in its
environment (the "VM" is a local directory the cells are pointed at with CR_CONTENT).

Faults come from the JSON file named by FAKE_COLAB_FAULTS, counted per subcommand (1-based call numbers):
  {"lose_reply": {"exec": [1]}}    run the call, then fail with rc 1 and no output ("Connection was lost")
  {"drop_call": {"exec": [2, 3]}}  do not run the call at all, fail with rc 1
  {"hang": {"exec": [4]}}          sleep past any timeout
  {"fail": {"stop": [1]}}          stop fails with rc 1
The call counters live in FAKE_COLAB_STATE (a directory)."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path


def _faults():
    p = os.environ.get("FAKE_COLAB_FAULTS")
    return json.loads(Path(p).read_text()) if p and Path(p).is_file() else {}


def _count(cmd):
    state = Path(os.environ["FAKE_COLAB_STATE"])
    state.mkdir(parents=True, exist_ok=True)
    f = state / (cmd + ".count")
    n = int(f.read_text()) + 1 if f.exists() else 1
    f.write_text(str(n))
    (state / "calls.log").open("a").write("%s %d\n" % (cmd, n))
    return n


def main(argv):
    cmd = argv[0]
    n = _count(cmd)
    faults = _faults()

    def hit(kind):
        return n in faults.get(kind, {}).get(cmd, [])

    if hit("hang"):
        time.sleep(600)
    if hit("drop_call"):
        print("RuntimeError: Connection was lost", file=sys.stderr)
        return 1
    if cmd == "exec":
        cell = argv[argv.index("-f") + 1]
        env = dict(os.environ)
        i = 0
        while i < len(argv):
            if argv[i] == "--env":
                k, _, v = argv[i + 1].partition("=")
                env[k] = v
                i += 1
            i += 1
        proc = subprocess.run([sys.executable, "-c", Path(cell).read_text()], env=env, capture_output=True, text=True)
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
    print("unknown subcommand %s" % cmd, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
