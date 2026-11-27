"""A thin, bounded wrapper over the `colab` CLI.

- Every call is bounded by its timeout: the CLI runs in a process group of its own, and a call that outlives its
  timeout is killed with that whole group (`os.killpg`), so a grandchild holding the pipes dies with it. It reports
  rc 124. A call interrupted by anything else (a signal, an exception) is killed the same way before the exception
  goes on.
- Every `out` and `err` is passed through the vendored library's token scrub (`launch_guards.scrub_colab_tokens`)
  before any caller sees it. Every downloaded file is checked by content: text is scrubbed in place with
  `launch_guards.scrub_colab_tokens`; binary files (containing NUL or failing strict UTF-8 decoding) are never
  rewritten — clean binaries stay byte-identical, while a token match (`binary_token_hit`) moves the file to
  quarantine and raises `BinaryDownloadRefused` (`BINARY_DOWNLOAD_TOKEN`). The quarantine is the caller's
  `quarantine_dir` (the Runner binds `<out-dir>.quarantine`); with none given, a new folder under the system temp
  dir (`bare_quarantine_dir`), never next to the downloaded file.
- A cell's environment travels inside the cell file, written owner-only (0600) where the OS has file modes, never
  as `colab exec --env` arguments, so no value reaches a command line.

The wrapper never parses CLI output beyond what its callers ask for, and never decides anything."""

from __future__ import annotations

import io
import os
import re
import shlex
import shutil
import signal
import stat
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Optional, Sequence

from colab_runner.errors import Refused, lookup
from colab_runner.window_lib import launch_guards as lg

TIMEOUT_RC = 124
KILL_WAIT_S = 5.0  # after a group kill: how long to wait for the pipes to close and the call to be reaped
EXEC_REPORT_S = 15.0  # of an exec call's bound, the time kept back for the CLI to report a kernel-side timeout


def _pattern_name(pattern: re.Pattern[str], idx: int) -> str:
    cleaned = pattern.pattern.replace(r"\b", "").replace(r"\s", " ")
    m = re.search(r"[A-Za-z][A-Za-z0-9_-]*", cleaned)
    return m.group(0) if m else "pattern_%d" % idx


# Every character a str pattern's `\s` matches. They all lie below U+3001 (the tests check the whole code space).
_STR_SPACES = [chr(c) for c in range(0x3001) if re.fullmatch(r"\s", chr(c))]


def _hex(data: bytes) -> bytes:
    return b"".join(b"\\x%02x" % b for b in data)


_SPACE_ONE = b"".join(_hex(ch.encode("utf-8")) for ch in _STR_SPACES if len(ch.encode("utf-8")) == 1)
_SPACE_MULTI = b"|".join(_hex(ch.encode("utf-8")) for ch in _STR_SPACES if len(ch.encode("utf-8")) > 1)
_SPACE_ANY = b"(?:[" + _SPACE_ONE + b"]|" + _SPACE_MULTI + b")"  # str `\s`, as UTF-8 bytes


def _unbounded_after(src: str, j: int) -> bool:
    """A greedy `+` or `*` at `j`: not a reluctant `+?` / `*?`, which would stop part-way through a character."""
    return j < len(src) and src[j] in "+*" and src[j + 1:j + 2] != "?"


def _byte_twin_source(src: str) -> bytes:
    r"""A str pattern's source as a bytes pattern that matches the same spans, on UTF-8 input, as the str pattern
    matches on the decoded text, for the patterns this function accepts:
    - `\s` (outside or inside a class) becomes the str class in UTF-8 (`_SPACE_ANY`: \x1c-\x1f, NEL, NBSP, U+1680,
      U+2000-U+200A, U+2028/9, U+202F, U+205F, U+3000 included);
    - a positive class of ASCII members matches one ASCII byte for one ASCII character (its `\s` as a whole
      multi-byte space), so any quantifier on it counts the same;
    - a negated class matches one BYTE where the str class matches one CHARACTER (a non-ASCII character is several
      bytes), so its spans are the same only when a run of it is taken whole: a negated class must be quantified
      with a greedy `+` or `*`. Anything else after it (none, `?`, `{m,n}`, a reluctant `+?` or `*?`) is refused, and
      so is an unescaped `.` outside a class, which has the same one-byte trouble;
    - a literal outside a class must be ASCII: a non-ASCII literal is several bytes, so a quantifier after it would
      count bytes, and it is refused;
    - `\b` is kept: over bytes it sees a non-ASCII character as a non-word byte, so next to a non-ASCII letter it
      matches where the str `\b` does not (a superset, never a miss).
    Any other class shorthand (`\S`, `\w`, `\W`, `\d`, `\D`) and any non-ASCII class member are refused, so a library
    change cannot slip through."""
    out: list[bytes] = []
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        if c == ".":
            raise ValueError("no byte twin for an unescaped '.' in %r (one byte, not one character)" % src)
        if c == "\\":
            esc = src[i:i + 2]
            if esc == "\\s":
                out.append(_SPACE_ANY)
            elif esc[1:] in ("S", "w", "W", "d", "D"):
                raise ValueError("no byte twin for %r in %r" % (esc, src))
            else:
                out.append(esc.encode("ascii"))
            i += 2
            continue
        if c == "[":
            j = i + 1
            neg = j < n and src[j] == "^"
            j += 1 if neg else 0
            items: list[str] = []
            if j < n and src[j] == "]":  # a leading ] is a literal
                items.append("\\]")
                j += 1
            has_space = False
            while j < n and src[j] != "]":
                if src[j] == "\\":
                    esc = src[j:j + 2]
                    if esc == "\\s":
                        has_space = True
                    elif esc[1:] in ("S", "w", "W", "d", "D"):
                        raise ValueError("no byte twin for %r in %r" % (esc, src))
                    else:
                        items.append(esc)
                    j += 2
                    continue
                if ord(src[j]) > 0x7F:
                    raise ValueError("no byte twin for a non-ASCII class member in %r" % src)
                items.append(src[j])
                j += 1
            body = "".join(items).encode("ascii")
            if neg and not _unbounded_after(src, j + 1):
                raise ValueError("no byte twin for a negated class without + or * in %r (one byte, not one "
                                 "character)" % src)
            if not has_space:
                out.append(b"[" + (b"^" if neg else b"") + body + b"]")
            elif neg:  # none of the members, and no str space (one byte, or a whole multi-byte one)
                out.append(b"(?:(?!" + _SPACE_MULTI + b")[^" + body + _SPACE_ONE + b"])")
            else:
                out.append(b"(?:[" + body + _SPACE_ONE + b"]|" + _SPACE_MULTI + b")")
            i = j + 1
            continue
        if ord(c) > 0x7F:
            raise ValueError("no byte twin for a non-ASCII literal outside a class in %r (several bytes)" % src)
        out.append(c.encode("ascii"))
        i += 1
    return b"".join(out)


# The byte twins of the vendored library's TOKEN_SCRUB_PATTERNS, derived from their sources (never copied).
BYTE_TOKEN_PATTERNS: list[tuple[str, re.Pattern[bytes]]] = [
    (_pattern_name(p, i), re.compile(_byte_twin_source(p.pattern), p.flags & ~re.UNICODE))
    for i, (p, _) in enumerate(lg.TOKEN_SCRUB_PATTERNS)
]

# The binary scan's one departure from the twins: `Bearer` + space + a word is ordinary prose ("Flame Bearer of the
# North"), and a render's metadata carries its prompt. In a binary, a Bearer hit counts only when the run after the
# spaces is token-shaped: at least BEARER_TOKEN_MIN characters of the pattern's own class [A-Za-z0-9._-], using at
# least two of lower case, upper case and digits. Every bearer credential the runner can meet is that shape: a
# Google OAuth access token (`ya29.` and well over 100 characters), a JWT (`eyJ`..., three base64url parts), a
# Hugging Face token (`hf_` and 34 mixed-case letters, also caught by the `hf_` pattern on its own). Text files
# keep the library's scrub unchanged.
BEARER_NAME = "Bearer"
BEARER_TOKEN_MIN = 20
_LOWER, _UPPER, _DIGIT = re.compile(rb"[a-z]"), re.compile(rb"[A-Z]"), re.compile(rb"[0-9]")


def token_shaped(run: bytes) -> bool:
    """A Bearer value that looks like a credential, not a word (see BEARER_TOKEN_MIN)."""
    if len(run) < BEARER_TOKEN_MIN:
        return False
    return sum(1 for kind in (_LOWER, _UPPER, _DIGIT) if kind.search(run)) >= 2


def _bearer_hit(bpat: re.Pattern[bytes], data: bytes) -> bool:
    pos = 0
    while True:
        m = bpat.search(data, pos)
        if m is None:
            return False
        if token_shaped(data[m.end(1):m.end()]):
            return True
        pos = m.start() + 1  # a later Bearer inside this match is tried too


def binary_token_hit(data: bytes) -> Optional[str]:
    """The name of the first token pattern a binary file's bytes match, or None: every byte twin, with the Bearer
    twin's value required to be token-shaped."""
    for name, bpat in BYTE_TOKEN_PATTERNS:
        if name == BEARER_NAME:
            if _bearer_hit(bpat, data):
                return name
        elif bpat.search(data):
            return name
    return None


class BinaryDownloadRefused(RuntimeError):
    """A downloaded binary file matched a token pattern: quarantined (or removed if quarantine failed) and refused."""

    def __init__(
        self,
        quarantined: list[dict[str, str]],
        *,
        removed: Sequence[dict[str, str]] = (),
        failures: Sequence[OSError] = (),
    ):
        self.code = "BINARY_DOWNLOAD_TOKEN"
        self.quarantined = list(quarantined)
        self.removed = list(removed)
        self.failures = list(failures)
        parts: list[str] = []
        if self.quarantined:
            parts.append("binary download quarantined: %s" % ", ".join(
                "%s (pattern=%s)" % (q["source"], q["pattern"]) for q in self.quarantined))
        if self.removed:
            parts.append("removed after quarantine failure: %s" % ", ".join(
                "%s (pattern=%s, %s)" % (r["source"], r["pattern"], r["error"]) for r in self.removed))
        self.message = "; ".join(parts) or "binary download token refused"
        self.hint, self.retryable = lookup(self.code).hint, lookup(self.code).retryable
        super().__init__("%s: %s" % (self.code, self.message))


def default_quarantine_dir(target: Path) -> Path:
    """Sibling `<target>.quarantine` directory, outside `target` itself (the Runner's, for its out dir). The path
    is resolved first, so a `target` ending in `..` names its real folder; a filesystem root has no sibling and is
    refused (OUT_DIR_INVALID)."""
    p = Path(target).resolve()
    if p.parent == p:
        raise Refused("OUT_DIR_INVALID", "%s is a filesystem root: its quarantine would be inside it" % p)
    return p.parent / (p.name + ".quarantine")


QUARANTINE_PREFIX = "colab-runner-quarantine-"


def bare_quarantine_dir() -> Path:
    """The quarantine of a download with no quarantine_dir (a bare `Colab.download` or `scrub_local`): a new folder
    `colab-runner-quarantine-*` under the system temp dir (`tempfile.mkdtemp`, owner-only), made at the first refused
    file. Never next to the file, so never inside the folder it was downloaded into."""
    return Path(tempfile.mkdtemp(prefix=QUARANTINE_PREFIX))


def is_binary_bytes(data: bytes) -> bool:
    """Decide text vs binary by content only (never by extension): NUL byte or invalid UTF-8 is binary."""
    if b"\x00" in data:
        return True
    try:
        data.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return True
    return False


_HOME_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9._/-])(?:"
    r"/(?:mnt/[a-zA-Z]/Users|home|Users)/[A-Za-z0-9._$-]+"
    r"|[A-Za-z]:(?:\\\\|\\|/)[Uu]sers(?:\\\\|\\|/)[A-Za-z0-9._$-]+"
    r")(?=(?:\\\\|\\|/)|$|[\s\"'`),;:|│])"
)
_HOME_TRAIL_RE = r"(?=(?:\\\\|\\|/)|$|[\s\"'`),;:|│])"


def scrub_home_paths(text: Optional[str]) -> str:
    """Replace local user home directory paths (e.g. from CLI tracebacks) with `~`."""
    out = text or ""
    if not out:
        return out
    tmp_resolved = str(Path(tempfile.gettempdir()).resolve())
    for var in ("HOME", "USERPROFILE"):
        raw = (os.environ.get(var) or "").strip()
        if not raw or len(Path(raw).parts) < 2 or str(Path(raw).resolve()) == tmp_resolved:
            continue
        for cand in {raw.rstrip("/\\"), raw.rstrip("/\\").replace("\\", "/")}:
            if cand:
                out = re.sub(r"(?<![A-Za-z0-9._/-])" + re.escape(cand) + _HOME_TRAIL_RE, "~", out)
    return _HOME_PATH_RE.sub("~", out)


def scrub(text: Optional[str]) -> str:
    return scrub_home_paths(lg.scrub_colab_tokens(text or ""))


@dataclass
class CliResult:
    rc: int
    out: str = ""
    err: str = ""

    @property
    def timed_out(self) -> bool:
        return self.rc == TIMEOUT_RC

    @property
    def ok(self) -> bool:
        return self.rc == 0


def _own_group() -> dict:
    """Popen arguments that give the CLI a process group of its own."""
    if os.name == "posix":
        return {"start_new_session": True}
    return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}  # pragma: no cover - Windows only; CI runs Linux


def kill_group(proc: subprocess.Popen) -> None:
    """SIGKILL the call's whole process group (POSIX), then the call itself."""
    if os.name == "posix":
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
    try:
        proc.kill()
    except OSError:  # pragma: no cover - the process is already gone
        pass


def _drain(proc: subprocess.Popen) -> str:
    """After a group kill: the rest of the call's stdout, if its pipes close within KILL_WAIT_S. A process outside
    the group can still hold a pipe; then its output is given up, the pipes are closed and the call returns."""
    try:
        out, _ = proc.communicate(timeout=KILL_WAIT_S)
        return out or ""
    except subprocess.TimeoutExpired:
        for pipe in (proc.stdout, proc.stderr):
            try:
                pipe.close()
            except OSError:  # pragma: no cover
                pass
        try:
            proc.wait(timeout=KILL_WAIT_S)
        except subprocess.TimeoutExpired:  # pragma: no cover - a SIGKILLed child is reaped at once
            pass
        return ""


def kernel_timeout(timeout: float) -> int:
    """The `colab exec --timeout` for a call bounded at `timeout` seconds: the bound, less the CLI's report time."""
    return max(1, int(timeout - min(EXEC_REPORT_S, timeout / 2.0)))


def env_preamble(env: Mapping[str, str] | None) -> str:
    """Python that sets `env` in the cell's os.environ before anything else in the cell runs."""
    if not env:
        return ""
    pairs = {str(k): str(v) for k, v in env.items()}
    return "import os as _cr_os\n_cr_os.environ.update(%r)\ndel _cr_os\n" % (pairs,)


def write_cell(folder: Path, source: str, env: Mapping[str, str] | None) -> Path:
    """The cell file: `env` first (env_preamble), then `source`. Created owner-only (0600) where the OS has file
    modes, inside a folder tempfile makes owner-only too; on Windows the mode only sets the read-only bit."""
    path = Path(folder) / "cell.py"
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(env_preamble(env) + source)
    return path


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:  # pragma: no cover
        return ""


class BackgroundCall:
    """One `colab exec` running in the background (the resident fallback), in a process group of its own, its output
    going to files (a pipe nobody reads would fill and stall it). `poll()` reaps it, or kills it once its deadline
    has passed; `kill()` kills its whole process group. When it ends, its output is read (scrubbed) and the temp
    folder holding the cell file is removed."""

    def __init__(self, proc, tmp, deadline: float, result: Optional[CliResult] = None):
        self.proc, self._tmp, self.deadline = proc, tmp, deadline
        self.result: Optional[CliResult] = result
        self.killed = False

    def poll(self) -> Optional[CliResult]:
        if self.result is None:
            if self.proc.poll() is not None:
                self._finish(self.proc.returncode)
            elif time.monotonic() >= self.deadline:
                self._kill(TIMEOUT_RC)
        return self.result

    def kill(self) -> CliResult:
        """Kill the call's process group, unless it has already ended (then it is only reaped)."""
        if self.result is None and self.proc.poll() is not None:
            self._finish(self.proc.returncode)
        if self.result is None:
            self._kill(None)
        return self.result

    def _kill(self, rc: Optional[int]) -> None:
        self.killed = True
        kill_group(self.proc)
        try:
            self.proc.wait(timeout=KILL_WAIT_S)
        except subprocess.TimeoutExpired:  # pragma: no cover - a SIGKILLed child is reaped at once
            pass
        self._finish(rc if rc is not None else (self.proc.returncode if self.proc.returncode is not None else -9))

    def _finish(self, rc: int) -> None:
        folder = Path(self._tmp.name)
        out, err = _read(folder / "out.txt"), _read(folder / "err.txt")
        self._tmp.cleanup()
        self.result = CliResult(int(rc), scrub(out), scrub(err))


class Colab:
    """`argv` is the command prefix, default ["colab"]; tests pass [sys.executable, "fake_colab.py"]."""

    def __init__(
        self,
        argv: Sequence[str] = ("colab",),
        env: Mapping[str, str] | None = None,
        *,
        quarantine_dir: Optional[Path] = None,
        log: Optional[Callable[[str], None]] = None,
        on_quarantine: Optional[Callable[[dict[str, str]], None]] = None,
    ):
        self.argv = list(argv)
        self.env = dict(os.environ if env is None else env)
        self.quarantine_dir = Path(quarantine_dir) if quarantine_dir is not None else None
        self.log = log
        self.on_quarantine = on_quarantine
        self._base_dir: Optional[Path] = None

    def run(self, args: Sequence[str], timeout: float) -> CliResult:
        """One call, bounded at `timeout` seconds (plus at most 2 x KILL_WAIT_S to kill and reap it)."""
        try:
            proc = subprocess.Popen(
                self.argv + list(args), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace", env=self.env, **_own_group(),
            )
        except OSError as exc:
            return CliResult(127, "", scrub(str(exc)))
        try:
            out, err = proc.communicate(timeout=max(0.0, float(timeout)))
        except subprocess.TimeoutExpired:
            kill_group(proc)
            return CliResult(TIMEOUT_RC, scrub(_drain(proc)), "timeout after %ss" % timeout)
        except BaseException:
            kill_group(proc)
            _drain(proc)
            raise
        return CliResult(proc.returncode, scrub(out), scrub(err))

    def exec_cell(
        self, session: str, source: str, env: Mapping[str, str] | None = None, timeout: float = 60.0
    ) -> CliResult:
        """Run `source` as a kernel cell (`colab exec -f`); the cell sees `env` as os.environ entries, set from the
        cell file itself (write_cell), never from the command line."""
        with tempfile.TemporaryDirectory(prefix="colab-runner-") as tmp:
            cell = write_cell(Path(tmp), source, env)
            return self.run(["exec", "-s", session, "-f", str(cell), "--timeout", str(kernel_timeout(timeout))], timeout)

    def start_cell(
        self, session: str, source: str, env: Mapping[str, str] | None = None, timeout: float = 60.0
    ) -> BackgroundCall:
        """`exec_cell` in the background: returns at once with a handle the caller polls and kills."""
        tmp = tempfile.TemporaryDirectory(prefix="colab-runner-")
        folder = Path(tmp.name)
        cell = write_cell(folder, source, env)
        args = ["exec", "-s", session, "-f", str(cell), "--timeout", str(kernel_timeout(timeout))]
        proc, error = None, None
        with (folder / "out.txt").open("w", encoding="utf-8") as out_fh, (folder / "err.txt").open(
            "w", encoding="utf-8"
        ) as err_fh:
            try:
                proc = subprocess.Popen(
                    self.argv + args, stdin=subprocess.DEVNULL, stdout=out_fh, stderr=err_fh, env=self.env,
                    **_own_group(),
                )
            except OSError as exc:
                error = exc
        if proc is None:  # the files are closed now, so the folder can go (Windows keeps open files)
            tmp.cleanup()
            return BackgroundCall(None, None, 0.0, CliResult(127, "", scrub(str(error))))
        return BackgroundCall(proc, tmp, time.monotonic() + max(0.0, float(timeout)))

    def download(self, session: str, remote: str, local: str, timeout: float = 120.0) -> CliResult:
        """`colab download`; whatever is at `local` afterwards runs through `scrub_local` before the call returns or
        raises, whatever the rc: a timed-out, failed or interrupted transfer can leave partial bytes."""
        try:
            return self.run(["download", "-s", session, remote, local], timeout)
        finally:
            scrub_local(
                local, quarantine_dir=self.quarantine_dir, base_dir=self._base_dir,
                log=self.log, on_quarantine=self.on_quarantine,
            )

    def download_unscrubbed(self, session: str, remote: str, local: str, timeout: float = 120.0) -> CliResult:
        """Download `remote` without running `scrub_local` on the transfer itself (staging tar archives only)."""
        return self.run(["download", "-s", session, remote, local], timeout)

    def stop(self, session: str, timeout: float = 120.0) -> CliResult:
        return self.run(["stop", "-s", session], timeout)

    def new_session(self, session: str, *, card: str, shape: Optional[str] = None,
                    cli_flag: str = "--gpu", timeout: float = 180.0) -> CliResult:
        """Open a Colab session (`colab new`): CPU uses `--high-mem`; TPU uses `--tpu`; GPU uses `--gpu`."""
        upper = card.strip().upper()
        if upper == "CPU" or cli_flag == "--cpu":
            return self.run(["new", "--high-mem", "-s", session], timeout)
        if cli_flag == "--tpu" or upper in ("V5E1", "V6E1"):
            return self.run(["new", "--tpu", card.strip().lower(), "-s", session], timeout)
        args = ["new", "--gpu", card]
        if shape == "high-mem":
            args.append("--high-mem")
        args.extend(["-s", session])
        return self.run(args, timeout)

    def upload(self, session: str, local: str, remote: str, timeout: float = 120.0) -> CliResult:
        """Upload one local file into the VM session (`colab upload -s S local remote`)."""
        return self.run(["upload", "-s", session, local, remote], timeout)

    def sessions(self, timeout: float = 30.0) -> CliResult:
        """List active Colab sessions (`colab sessions`)."""
        return self.run(["sessions"], timeout)

    def restart_kernel(self, session: str, timeout: float = 180.0) -> CliResult:
        return self.run(["restart-kernel", "-s", session], timeout)

    def usage(self, timeout: float = 60.0) -> CliResult:
        return self.run(["usage"], timeout)


_SESSION_ROW_RE = re.compile(r"^\[([^\]]+)\]\s*(.*)$")


def parse_sessions_output(text: str) -> list[dict[str, Optional[str]]]:
    """Parse `colab sessions` rows (`[name] endpoint | Hardware: X | Shape: Y | Variant: Z[ | Status: W]`)."""
    rows: list[dict[str, Optional[str]]] = []
    for line in (text or "").splitlines():
        m = _SESSION_ROW_RE.match(line.strip())
        if m is None:
            continue
        session_name, rest = m.group(1).strip(), m.group(2)
        parts = [seg.strip() for seg in rest.split("|")]
        endpoint: Optional[str] = parts[0] if parts and ":" not in parts[0] and parts[0] else None
        fields: dict[str, str] = {}
        start_idx = 1 if endpoint is not None or (parts and not parts[0]) else 0
        for seg in parts[start_idx:]:
            key, sep, val = seg.partition(":")
            if sep:
                fields[key.strip().lower()] = val.strip()
        hardware = fields.get("hardware") or fields.get("gpu") or fields.get("accelerator") or fields.get("card")
        rows.append({
            "session": session_name,
            "endpoint": endpoint,
            "hardware": hardware,
            "accelerator": hardware,
            "shape": fields.get("shape"),
            "variant": fields.get("variant"),
            "status": fields.get("status"),
            "raw": line.strip(),
        })
    return rows


def _unlink_quiet(path: Path) -> None:
    try:
        path.unlink()
    except OSError:  # pragma: no cover - it cannot be touched at all
        pass


def _unlink_link(path: Path) -> None:
    """Remove a symlink (or a Windows junction) to a directory: the link only, never its target."""
    try:
        path.unlink()
    except OSError:
        try:
            os.rmdir(path)  # Windows removes a directory link with rmdir; the target is untouched
        except OSError:  # pragma: no cover - it cannot be touched at all
            pass


_WINDOWS = os.name == "nt"
# the NTFS junction's reparse tag; the stat module names it on Windows only (its value is fixed by Windows)
_MOUNT_POINT_TAG = getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", 0xA0000003)


def _reparse_tag(path: Path) -> int:
    """The entry's own reparse tag (os.lstat: never followed), or 0: no tag, no such field off Windows, or no entry."""
    try:
        return getattr(os.lstat(path), "st_reparse_tag", 0)
    except OSError:
        return 0


def _is_junction(path: Path) -> bool:
    """An NTFS junction, live or broken. `os.path.isjunction` where Python has it (3.12+); before that, on Windows, the
    mount-point reparse tag. Only that tag: any other reparse kind (a deduplicated file, a cloud placeholder) is not a
    link, and goes on to the regular-file path (read, scrubbed, and removed only if it cannot be read)."""
    isjunction = getattr(os.path, "isjunction", None)
    if isjunction is not None:
        return isjunction(path)
    return _WINDOWS and _reparse_tag(path) == _MOUNT_POINT_TAG


def _is_link(path: Path) -> bool:
    """A symlink or an NTFS junction, live or broken: removed with `_unlink_link`, never followed. `Path.is_symlink()`
    is asked first (it catches every symlink, a Windows one included); it is False for a junction, so `_is_junction`
    is asked only when it is False."""
    return path.is_symlink() or _is_junction(path)


class QuarantineEscape(OSError):
    """A quarantine path that would land outside its quarantine directory: refused, the file is removed instead."""

    def __init__(self, src: Path, dest: Path, quarantine_dir: Path):
        self.code = "QUARANTINE_ESCAPE"
        self.message = "%s would be quarantined at %s, outside %s" % (src, dest, quarantine_dir)
        self.hint, self.retryable = lookup(self.code).hint, lookup(self.code).retryable
        super().__init__("%s: %s" % (self.code, self.message))


def _iter_files(root: Path, errors: list[OSError]) -> list[Path]:
    """The regular files under `root` (or `root` itself), to be read. Symlinks and NTFS junctions are unlinked (the
    link only), never followed; any other entry that is not a regular file or a directory (a FIFO, a socket, a device)
    is unlinked, never read (a FIFO would block the read). A directory that cannot be listed is added to `errors`,
    never skipped in silence."""
    if _is_link(root):
        _unlink_link(root)
        return []
    if not root.exists():
        return []
    if root.is_file():
        return [root]
    if not root.is_dir():  # a FIFO, a socket or a device
        _unlink_quiet(root)
        return []
    files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root, onerror=errors.append, followlinks=False):
        parent = Path(dirpath)
        keep = []
        for d in sorted(dirnames):
            if _is_link(parent / d):  # os.walk lists a link or junction to a directory here: unlinked, never followed
                _unlink_link(parent / d)
            else:
                keep.append(d)
        dirnames[:] = keep
        for name in sorted(filenames):
            p = parent / name
            if _is_link(p):  # a link to a file, or a broken link or junction
                _unlink_link(p)
            elif not p.is_file():
                _unlink_quiet(p)
            else:
                files.append(p)
    return files


def _quarantine_rel(src: Path, root: Path, qdir: Path, base: Optional[Path]) -> Path:
    """Where `src` goes inside the quarantine, relative to it. Every path is resolved first, so a root (or a base
    dir) written with `..` never puts `..` into the result: the base dir's relative path, else the root's own name
    and the path under it (no name when the quarantine is the root's own sibling `<root>.quarantine`)."""
    if base is not None and src.is_relative_to(base):
        return src.relative_to(base)
    if root.is_dir() and src.is_relative_to(root):
        inner = src.relative_to(root)
        if not root.name:  # a filesystem root has no name to file it under
            return Path("root") / inner
        same = qdir == root.parent / (root.name + ".quarantine")
        return inner if same else Path(root.name) / inner
    return Path(src.name)


def _quarantine_dest(src: Path, root: Path, quarantine_dir: Path, base_dir: Optional[Path]) -> Path:
    """The file's place in `quarantine_dir`, never outside it: checked before any move (QuarantineEscape)."""
    qdir = Path(quarantine_dir).resolve()
    rel = _quarantine_rel(Path(src).resolve(), Path(root).resolve(), qdir,
                          Path(base_dir).resolve() if base_dir is not None else None)
    dest = qdir / rel
    n = 1
    while dest.exists():
        dest = (qdir / rel).with_name("%s.%d" % (rel.name, n))
        n += 1
    if not dest.resolve().is_relative_to(qdir) or dest.resolve() == qdir:
        raise QuarantineEscape(Path(src), dest, qdir)
    return dest


def scrub_local(
    local,
    *,
    quarantine_dir: Optional[Path] = None,
    base_dir: Optional[Path] = None,
    log: Optional[Callable[[str], None]] = None,
    on_quarantine: Optional[Callable[[dict[str, str]], None]] = None,
) -> None:
    """Binary-safe scrub of `local` (file or directory).

    Text vs binary is decided by content (NUL byte or failed strict UTF-8 decode). Text files are scrubbed in place
    with `launch_guards.scrub_colab_tokens`; binary files are never rewritten (clean binaries stay byte-identical, while
    a token match, `binary_token_hit`, moves the file to quarantine and raises `BinaryDownloadRefused`). The
    quarantine is `quarantine_dir`, or with none a new `bare_quarantine_dir()` under the system temp dir. Unreadable
    files are removed. Only regular files are read; other entries are unlinked (see `_iter_files`).

    One failure never stops the scrub of the other files: a hit whose move fails, or whose quarantine path would land
    outside the quarantine (QuarantineEscape, checked before the move), is removed, and a directory that cannot be
    listed is noted, and the scrub goes on with the rest. QuarantineEscape raises first; otherwise any binary token hit
    raises BinaryDownloadRefused (with `failures` and `removed` attached), and any other OSError raises failures[0]."""
    root = Path(local)
    failures: list[OSError] = []
    files = _iter_files(root, failures)
    qdir = Path(quarantine_dir) if quarantine_dir is not None else None
    quarantined: list[dict[str, str]] = []
    removed_hits: list[dict[str, str]] = []
    for p in files:
        try:
            data = p.read_bytes()
        except OSError:
            _unlink_quiet(p)
            continue
        if not is_binary_bytes(data):
            raw = io.StringIO(data.decode("utf-8"), newline=None).read()
            scrubbed = lg.scrub_colab_tokens(raw)
            if scrubbed != raw:
                try:
                    p.write_text(scrubbed, encoding="utf-8", newline="\n")
                except OSError:
                    _unlink_quiet(p)
            continue
        hit = binary_token_hit(data)
        if hit is None:
            continue
        if qdir is None:
            qdir = bare_quarantine_dir()
        try:
            dest = _quarantine_dest(p, root, qdir, base_dir)  # QuarantineEscape (an OSError) before any move
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(p), str(dest))
        except OSError as exc:
            _unlink_quiet(p)  # never left in place with its token
            failures.append(exc)
            if not isinstance(exc, QuarantineEscape):
                removed_hits.append({"source": str(p), "pattern": hit, "error": "%s: %s" % (type(exc).__name__, exc)})
            if log is not None:
                log(scrub("BINARY_DOWNLOAD_TOKEN: %s could not be moved to quarantine and was removed (%s: %s)"
                          % (p, type(exc).__name__, exc)))
            continue
        entry = {"path": str(dest), "source": str(p), "pattern": hit}
        quarantined.append(entry)
        if log is not None:
            log(scrub("BINARY_DOWNLOAD_TOKEN: quarantined %s -> %s (pattern=%s)" % (p, dest, hit)))
        if on_quarantine is not None:
            on_quarantine(entry)
    escape = next((e for e in failures if isinstance(e, QuarantineEscape)), None)
    if escape is not None:
        raise escape
    if quarantined or removed_hits:
        raise BinaryDownloadRefused(quarantined, removed=removed_hits, failures=failures)
    if failures:
        raise failures[0]


def cli_from_env() -> Colab:
    """The CLI named by COLAB_RUNNER_CLI, else `colab`. It is split with `shlex.split`, POSIX quoting: on Windows
    write the path with forward slashes or quote it (`"C:\\tools\\colab.exe"`), since an unquoted backslash is
    dropped (`C:\\tools\\colab.exe` would become `C:toolscolab.exe`)."""
    raw = os.environ.get("COLAB_RUNNER_CLI", "").strip()
    try:
        parts = shlex.split(raw) if raw else []
    except ValueError as exc:  # an unclosed quote: refused before anything runs (no session marker yet)
        raise Refused("CLI_INVALID", "COLAB_RUNNER_CLI cannot be split with shlex.split (%s)" % exc) from None
    return Colab(parts or ["colab"])


__all__ = [
    "BEARER_TOKEN_MIN", "BYTE_TOKEN_PATTERNS", "BackgroundCall", "BinaryDownloadRefused", "Colab", "CliResult",
    "KILL_WAIT_S", "QuarantineEscape", "TIMEOUT_RC", "bare_quarantine_dir", "binary_token_hit", "cli_from_env",
    "default_quarantine_dir", "env_preamble", "is_binary_bytes", "kernel_timeout", "kill_group",
    "parse_sessions_output", "scrub", "scrub_home_paths", "scrub_local", "token_shaped", "write_cell",
]
