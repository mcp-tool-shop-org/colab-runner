"""0.1.1, the post-0.1.0 follow-ups from the reviews of PRs #4 and #5: an OOM line upgrades `failed` in the resident
cleanup-log fallback, NTFS junctions are unlinked and never walked, a QuarantineEscape marks the run log collected,
and a `cap` end carries a structured `cap_trip` with its hint."""

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from colab_runner import colab_cli
from colab_runner.colab_cli import BinaryDownloadRefused, CliResult, QuarantineEscape, scrub_local
from colab_runner.errors import CODES
from colab_runner.runner import Runner
from tests.fakes import FakeClock, FakeColab, guard, make, rec, usage_text
from tests.test_tokens import fake_tokens

WINDOWS = os.name == "nt"
OOM_LINE = "Out of memory: Killed process 9 (python3)\n"
PNG_HEAD = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"


def poisoned(t: dict) -> bytes:
    return PNG_HEAD + b"meta=" + t["hf"].encode("ascii") + b"\x00end"


def snapshot(folder: Path) -> dict:
    return {p.relative_to(folder).as_posix(): p.read_bytes() for p in sorted(folder.rglob("*")) if p.is_file()}


def count_downloads(colab: FakeColab, suffix: str) -> list[str]:
    """The remotes of every `colab download` of a name ending in `suffix`, as the fake receives them."""
    got: list[str] = []
    orig = colab.download

    def download(session, remote, local, timeout=120.0):
        if remote.endswith(suffix):
            got.append(remote)
        return orig(session, remote, local, timeout)

    colab.download = download
    return got


def cli(argv, colab, guard_factory=None):
    """main(argv) on `colab`, its Runner on the fake's clock; (code, stdout, stderr)."""
    from colab_runner.__main__ import main

    clock = colab.clock = colab.clock or FakeClock()
    out, err = io.StringIO(), io.StringIO()
    with contextlib.ExitStack() as stack:
        stack.enter_context(mock.patch("colab_runner.__main__.cli_from_env", return_value=colab))
        stack.enter_context(mock.patch("colab_runner.__main__.Runner", side_effect=lambda *a, **kw: Runner(
            *a, clock=clock.now, sleep=clock.sleep, **kw)))
        if guard_factory is not None:
            stack.enter_context(mock.patch("colab_runner.__main__.SpendGuard", side_effect=guard_factory))
        stack.enter_context(contextlib.redirect_stdout(out))
        stack.enter_context(contextlib.redirect_stderr(err))
        code = main(argv)
    return code, out.getvalue(), err.getvalue()


class OomUpgradesFailedTest(unittest.TestCase):
    """Item 1: in the resident fallback whose run log is first collected in _end, an OOM kill line makes the end
    `oom` whatever the rc (the handbook's end-reason table), `failed` included."""

    @staticmethod
    def log_only_in_cleanup(kind, fake):
        # download 1 is the poll file, 2 the in-loop log (none on the VM yet: it fails), 3 the cleanup's log
        if kind == "download" and fake.events.count("download") == 3:
            fake.run_log = fake.later_log

    @staticmethod
    def no_time_in_the_loop(kind, fake):
        # the poll file that carries the rc lands with less than MIN_CALL_S left: the log waits for _end
        if kind == "download" and fake.events.count("download") == 1:
            fake.clock.advance(99.5)
            fake.run_log = fake.later_log

    def test_rc_1_with_an_oom_line_collected_only_in_cleanup_is_oom(self):
        for name, hook, wall in (("in-loop download failed", self.log_only_in_cleanup, 36000),
                                 ("less than MIN_CALL_S left", self.no_time_in_the_loop, 100)):
            with self.subTest(path=name), tempfile.TemporaryDirectory() as tmp:
                colab = FakeColab(vm_poll_file=rec(rc=1), on_call=hook)
                colab.later_log = "step 3\n" + OOM_LINE
                runner, _ = make(colab, tmp)
                downloads = count_downloads(colab, ".run.out")
                res = runner.run(script="/c/x.sh", wall_s=wall, resident=True)
                self.assertEqual((res.reason, res.rc), ("oom", 1))
                self.assertEqual(res.detail, "the job was OOM-killed: an OOM kill line in the job log (OOM_KILL_RE)")
                self.assertEqual(len(downloads), 2 if hook is self.log_only_in_cleanup else 1)
                self.assertIn("Out of memory", (runner.out_dir / "job.run.out").read_text(encoding="utf-8"))
                written = json.loads((runner.out_dir / "runner_result.json").read_text(encoding="utf-8"))
                self.assertEqual(written["reason"], "oom")

    def test_rc_1_with_no_oom_line_stays_failed(self):
        for name, hook, wall in (("in-loop download failed", self.log_only_in_cleanup, 36000),
                                 ("less than MIN_CALL_S left", self.no_time_in_the_loop, 100)):
            with self.subTest(path=name), tempfile.TemporaryDirectory() as tmp:
                colab = FakeColab(vm_poll_file=rec(rc=1), on_call=hook)
                colab.later_log = "step 3\nTraceback: ValueError\n"
                runner, _ = make(colab, tmp)
                res = runner.run(script="/c/x.sh", wall_s=wall, resident=True)
                self.assertEqual((res.reason, res.rc, res.detail), ("failed", 1, "the job exited with rc 1"))
                self.assertTrue((runner.out_dir / "job.run.out").is_file())  # collected, and judged


@unittest.skipUnless(WINDOWS, "an NTFS junction exists only on Windows; a Linux VM download cannot create one")
class JunctionTest(unittest.TestCase):
    """Item 2: an NTFS junction inside a download, as its root, or broken, is unlinked (the junction only), never
    walked; its target and everything in it stay untouched."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._far = tempfile.TemporaryDirectory()  # outside the bundle's tree altogether
        self.addCleanup(self._far.cleanup)
        self.root, self.t = Path(self._tmp.name), fake_tokens()
        self.target = Path(self._far.name) / "target"
        self.target.mkdir()
        (self.target / "notes.txt").write_text("Bearer %s\n" % self.t["bearer"], encoding="utf-8")
        (self.target / "render.png").write_bytes(poisoned(self.t))  # a followed junction would quarantine it
        self.before = snapshot(self.target)

    def junction(self, link: Path, target: Path) -> None:
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
        self.assertTrue(os.path.isjunction(link))
        self.assertFalse(link.is_symlink())  # why _iter_files asks os.path.isjunction too

    def assert_target_untouched(self):
        self.assertTrue(self.target.is_dir())
        self.assertEqual(snapshot(self.target), self.before)  # never scrubbed, never moved

    def test_a_junction_inside_a_download_is_removed_and_its_target_untouched(self):
        bundle = self.root / "bundle"
        (bundle / "sub").mkdir(parents=True)
        (bundle / "keep.txt").write_text("Bearer %s\n" % self.t["bearer"], encoding="utf-8")
        self.junction(bundle / "sub" / "to_target", self.target)
        scrub_local(bundle, quarantine_dir=self.root / "q")  # no hit inside the bundle itself: nothing is refused
        self.assertFalse(os.path.lexists(bundle / "sub" / "to_target"))
        self.assertTrue((bundle / "sub").is_dir())
        self.assertEqual((bundle / "keep.txt").read_text(encoding="utf-8"), "Bearer [REDACTED]\n")
        self.assertFalse((self.root / "q").exists())
        self.assert_target_untouched()

    def test_a_junction_as_the_root_is_removed_and_its_target_untouched(self):
        link = self.root / "download"
        self.junction(link, self.target)
        scrub_local(link, quarantine_dir=self.root / "q")
        self.assertFalse(os.path.lexists(link))
        self.assert_target_untouched()

    def test_a_broken_junction_is_removed(self):
        gone = Path(self._far.name) / "gone"
        gone.mkdir()
        bundle = self.root / "bundle"
        bundle.mkdir()
        self.junction(bundle / "dangling", gone)
        gone.rmdir()  # the junction now points at nothing
        self.assertFalse((bundle / "dangling").exists())
        scrub_local(bundle)
        self.assertFalse(os.path.lexists(bundle / "dangling"))
        root_link = self.root / "dangling_root"
        gone.mkdir()
        self.junction(root_link, gone)
        gone.rmdir()
        scrub_local(root_link)
        self.assertFalse(os.path.lexists(root_link))
        self.assert_target_untouched()

    def test_a_junction_reached_through_runner_download_is_removed(self):
        out = self.root / "out"
        out.mkdir()
        runner = Runner(FakeColab(), "s", out, clock=FakeClock().now)
        self.junction(out / "pulled", self.target)  # as a download that left a junction at the target path
        runner.download("/content/nothing", out / "pulled")
        self.assertFalse(os.path.lexists(out / "pulled"))
        self.assertEqual(runner._res.quarantined, [])
        self.assert_target_untouched()


DEDUP_TAG, CLOUD_TAG = 0x80000013, 0x9000001A  # IO_REPARSE_TAG_DEDUP, IO_REPARSE_TAG_CLOUD: real files, not links
REAL_LSTAT = os.lstat


class _TaggedStat:
    """A real lstat result with the reparse tag Windows would report for that entry."""

    def __init__(self, real, tag):
        self._real, self.st_reparse_tag = real, tag

    def __getattr__(self, name):
        return getattr(self._real, name)


def _caller() -> str:
    """The function that called os.lstat, past this stand-in and unittest.mock's own frames."""
    f = sys._getframe(2)
    while f is not None and f.f_globals.get("__name__") == "unittest.mock":
        f = f.f_back
    return f.f_code.co_name if f is not None else ""


class JunctionFallbackTest(unittest.TestCase):
    """PR #6 fix 1, item 2: on a Python with no `os.path.isjunction` (3.11), a junction on Windows is known by its
    mount-point reparse tag (os.lstat, never followed). Runs on any OS: `os.path.isjunction` is taken away, the
    platform is Windows, and os.lstat reports the tag set for each entry here."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root, self.t = Path(self._tmp.name), fake_tokens()
        self.tags: dict[str, object] = {}  # path -> tag; None: an lstat result with no tag field (off Windows)
        real_unlink_link = colab_cli._unlink_link
        self.unlinked: list[Path] = []

        def unlink_link(path):
            self.unlinked.append(Path(path))
            return real_unlink_link(path)

        for patch in (mock.patch.object(colab_cli.os.path, "isjunction", None, create=True),
                      mock.patch.object(colab_cli, "_WINDOWS", True),
                      mock.patch.object(colab_cli.os, "lstat", side_effect=self.lstat),
                      mock.patch.object(colab_cli, "_unlink_link", side_effect=unlink_link)):
            patch.start()
            self.addCleanup(patch.stop)

    def lstat(self, path, *a, **kw):
        key = os.path.normcase(os.fspath(path))
        # an OSError tag is raised into _reparse_tag only; other callers (pathlib's is_symlink, whose OSError handling
        # differs between Pythons) get the real lstat
        if isinstance(self.tags.get(key), OSError) and _caller() == "_reparse_tag":
            raise self.tags[key]
        real = REAL_LSTAT(path, *a, **kw)
        if isinstance(self.tags.get(key), OSError):
            return _TaggedStat(real, 0)
        if key in self.tags and self.tags[key] is None:
            return _NoTag(real)
        return _TaggedStat(real, self.tags.get(key, 0))

    def tag(self, path: Path, tag) -> Path:
        self.tags[os.path.normcase(os.fspath(path))] = tag
        return path

    def token_file(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("Bearer %s\n" % self.t["bearer"], encoding="utf-8")
        return path

    def test_a_mount_point_tag_is_treated_as_a_link_and_unlinked(self):
        bundle = self.root / "bundle"
        empty = self.tag(bundle / "empty_junction", colab_cli._MOUNT_POINT_TAG)
        empty.mkdir(parents=True)
        full = self.tag(bundle / "junction_to_secrets", colab_cli._MOUNT_POINT_TAG)
        inside = self.token_file(full / "notes.txt")  # what a followed junction would scrub
        before = inside.read_bytes()
        self.assertTrue(colab_cli._is_link(empty))
        scrub_local(bundle)
        self.assertEqual(sorted(self.unlinked), sorted([empty, full]))  # each removed as a link, with _unlink_link
        self.assertFalse(os.path.lexists(empty))
        self.assertEqual(inside.read_bytes(), before)  # never walked into: its file is not read or scrubbed
        root = self.tag(self.root / "junction_root", colab_cli._MOUNT_POINT_TAG)
        root.mkdir()
        scrub_local(root)
        self.assertEqual(self.unlinked[-1], root)
        self.assertFalse(os.path.lexists(root))

    def test_another_reparse_tag_takes_the_regular_file_path(self):
        bundle = self.root / "bundle"
        dedup = self.tag(self.token_file(bundle / "dedup.txt"), DEDUP_TAG)
        cloud_dir = self.tag(bundle / "cloud_folder", CLOUD_TAG)
        cloud_file = self.tag(self.token_file(cloud_dir / "placeholder.txt"), CLOUD_TAG)
        for p in (dedup, cloud_dir, cloud_file):
            self.assertFalse(colab_cli._is_link(p))
        self.assertEqual(sorted(colab_cli._iter_files(bundle, [])), sorted([dedup, cloud_file]))  # read, folder walked
        scrub_local(bundle)
        self.assertEqual(self.unlinked, [])  # never unlinked as a link
        for p in (dedup, cloud_file):
            self.assertEqual(p.read_text(encoding="utf-8"), "Bearer [REDACTED]\n")  # read and scrubbed in place

    def test_an_ordinary_directory_is_not_a_link(self):
        plain = self.root / "plain"
        self.token_file(plain / "notes.txt")
        no_field = self.tag(self.root / "no_tag_field", None)
        self.token_file(no_field / "notes.txt")
        self.assertFalse(colab_cli._is_link(plain))  # tag 0
        self.assertFalse(colab_cli._is_link(no_field))  # no st_reparse_tag at all (an OS that has none)
        self.tag(plain, colab_cli._MOUNT_POINT_TAG)
        with mock.patch.object(colab_cli, "_WINDOWS", False):  # off Windows the tag is never read
            self.assertFalse(colab_cli._is_link(plain))
        self.tags.clear()
        scrub_local(self.root)
        self.assertEqual(self.unlinked, [])
        self.assertEqual((plain / "notes.txt").read_text(encoding="utf-8"), "Bearer [REDACTED]\n")

    def test_a_mount_point_tag_in_filenames_is_unlinked_as_a_link(self):
        # os.walk lists an entry that is not a directory to it under `filenames`: there too a mount-point tag is a link
        bundle = self.root / "bundle"
        entry = self.tag(self.token_file(bundle / "sub" / "tagged_entry"), colab_cli._MOUNT_POINT_TAG)
        plain = self.token_file(bundle / "sub" / "plain.txt")
        walked = [(Path(d), sorted(f)) for d, _, f in os.walk(bundle)]
        self.assertIn((bundle / "sub", ["plain.txt", "tagged_entry"]), walked)  # both in filenames, not dirnames
        files = colab_cli._iter_files(bundle, [])
        self.assertEqual(files, [plain])
        self.assertEqual(self.unlinked, [entry])  # removed with _unlink_link, as a link
        self.assertFalse(os.path.lexists(entry))

    def test_an_oserror_from_lstat_reads_as_not_a_junction(self):
        # _reparse_tag catches the OSError and returns 0, so _is_junction is False: the entry is not a link, and
        # _iter_files judges it as any other (is_file()); a regular file is read and scrubbed, never unlinked as a link
        entry = self.token_file(self.root / "bundle" / "unreadable_tag.txt")
        self.tag(entry, PermissionError(13, "lstat refused", str(entry)))
        with mock.patch.object(colab_cli.os, "lstat", side_effect=PermissionError(13, "lstat refused")) as lst:
            self.assertEqual(colab_cli._reparse_tag(entry), 0)  # the bare path: lstat raises, the tag reads 0
            lst.assert_called_once_with(entry)
        self.assertEqual(colab_cli._reparse_tag(entry), 0)  # through the stand-in, which raises into _reparse_tag
        self.assertFalse(colab_cli._is_junction(entry))
        self.assertFalse(colab_cli._is_link(entry))
        self.assertEqual(colab_cli._iter_files(self.root / "bundle", []), [entry])
        scrub_local(self.root / "bundle")
        self.assertEqual(self.unlinked, [])
        self.assertEqual(entry.read_text(encoding="utf-8"), "Bearer [REDACTED]\n")

    def test_a_symlink_still_takes_the_is_symlink_route_with_the_fallback_active(self):
        target = self.root / "target"
        inside = self.token_file(target / "notes.txt")
        before = inside.read_bytes()
        bundle = self.root / "bundle"
        bundle.mkdir()
        link = bundle / "to_target"
        try:
            link.symlink_to(target, target_is_directory=True)
        except OSError:
            self.skipTest("symlinks need privilege on this Windows host (CI runs Linux)")
        with mock.patch.object(colab_cli, "_reparse_tag", wraps=colab_cli._reparse_tag) as tag_read:
            self.assertTrue(colab_cli._is_link(link))
            tag_read.assert_not_called()  # is_symlink() answered first; the mount-point fallback only adds junctions
            scrub_local(bundle)
            self.assertNotIn(link, [c.args[0] for c in tag_read.call_args_list])
        self.assertEqual(self.unlinked, [link])
        self.assertFalse(os.path.lexists(link))
        self.assertEqual(inside.read_bytes(), before)


class _NoTag:
    """A real lstat result with no st_reparse_tag field, as on an OS that has none."""

    def __init__(self, real):
        self._real = real

    def __getattr__(self, name):
        if name == "st_reparse_tag":
            raise AttributeError(name)
        return getattr(self._real, name)


class QuarantineEscapeMarksTheLogTest(unittest.TestCase):
    """Item 3: a QuarantineEscape during the in-loop run-log collection marks the log collected, so _end never
    downloads `<job>.run.out` a second time."""

    def test_an_escape_in_the_loop_causes_no_second_download(self):
        t = fake_tokens()
        with tempfile.TemporaryDirectory() as tmp:
            colab = FakeColab(vm_poll_file=rec(rc=0), run_log=poisoned(t))
            runner, _ = make(colab, tmp)
            downloads = count_downloads(colab, ".run.out")
            with mock.patch.object(colab_cli, "_quarantine_rel", return_value=Path("..") / "escaped.png"):
                with self.assertRaises(QuarantineEscape):
                    runner.run(script="/c/x.sh", wall_s=36000, resident=True)
            self.assertEqual(downloads, ["/content/job.run.out"])  # the loop's, and no cleanup download
            res = runner._res
            self.assertEqual((res.reason, res.stopped, res.refusal["code"]), ("error", True, "QUARANTINE_ESCAPE"))
            self.assertFalse((runner.out_dir / "job.run.out").exists())
            self.assertFalse((Path(tmp) / "escaped.png").exists())

    def test_a_binary_refusal_in_the_loop_still_causes_no_second_download(self):
        t = fake_tokens()
        with tempfile.TemporaryDirectory() as tmp:
            colab = FakeColab(vm_poll_file=rec(rc=0), run_log=poisoned(t))
            runner, _ = make(colab, tmp)
            downloads = count_downloads(colab, ".run.out")
            with self.assertRaises(BinaryDownloadRefused):
                runner.run(script="/c/x.sh", wall_s=36000, resident=True)
            self.assertEqual(len(downloads), 1)


class QuietGuard:
    """Never trips by itself; its ceiling is 300 s away (the runner's own clock ends the run: CAP_CEILING_REACHED)."""

    def __init__(self, trip=None):
        self.trip = trip

    def check(self, elapsed_s):
        return self.trip if self.trip is not None and elapsed_s >= 100 else None

    def seconds_left(self, elapsed_s):
        return 300.0 - elapsed_s


def overrun_first_poll(seconds):
    def hook(kind, fake):
        if kind == "poll" and fake.poll_count == 1:
            fake.clock.advance(seconds)  # a poll call that overran the ceiling: the guard trips on the next check

    return hook


def trip_scenarios():
    """code -> (FakeColab kwargs, guard CLI args, SpendGuard stand-in or None). One real end per trip code."""
    pre = ["--pre-open-rate", "0", "--pre-open-assignments", "0"]
    no_count = "Current balance: 90.00 compute units\nUsage rate: 10.00 / hr\n"  # no Active assignments line
    return {
        # 10/h with 1000 s reserved: ceiling 5 - 2.78 = 2.22 units after 800 s; the first poll overruns it
        "CAP_GUARD_TRIPPED": ({"on_call": overrun_first_poll(1000)},
                              ["--max-rate", "10", "--cap", "5", "--cleanup-bound-s", "1000"], None),
        "RATE_ABOVE_MAX": ({"usage": usage_text(rate=11.2, n=1)},
                           ["--max-rate", "10.5", "--cap", "40", "--cleanup-bound-s", "1000"] + pre, None),
        "ASSIGNMENTS_UNREADABLE": ({"usage": lambda n: usage_text(rate=10.0, n=1) if n <= 2 else no_count},
                                   ["--max-rate", "10.5", "--cap", "40", "--cleanup-bound-s", "1000"] + pre, None),
        "COLAB_USAGE_PARSE_FAILED": ({"usage": lambda n: CliResult(1, "", "RuntimeError: Connection was lost")},
                                     ["--max-rate", "1", "--cap", "20", "--cleanup-bound-s", "1000"], None),
        "CAP_CEILING_REACHED": ({}, ["--max-rate", "1", "--cap", "20", "--cleanup-bound-s", "1000"],
                                lambda **kw: QuietGuard()),
    }


class CapTripTest(unittest.TestCase):
    """Item 4: a `cap` end records `cap_trip: {code, message, hint, retryable}` in runner_result.json, with the code
    read from the trip's leading `CODE:` and the hint from errors.CODES, and prints the hint after RUN_END. `detail`
    and the exit code are as they were."""

    def argv(self, out, guard_args):
        return ["run", "--session", "s", "--out-dir", str(out), "--script", "/c/x.sh", "--wall-s", "36000"] + guard_args

    def test_each_trip_code_records_its_cap_trip_and_prints_its_hint(self):
        scenarios = trip_scenarios()
        self.assertEqual(set(scenarios), {"CAP_GUARD_TRIPPED", "RATE_ABOVE_MAX", "ASSIGNMENTS_UNREADABLE",
                                          "COLAB_USAGE_PARSE_FAILED", "CAP_CEILING_REACHED"})
        for code, (fake_kw, guard_args, factory) in scenarios.items():
            with self.subTest(code=code), tempfile.TemporaryDirectory() as tmp:
                colab = FakeColab(polls=lambda n, env: rec(progress=n), **fake_kw)
                out = Path(tmp) / "out"
                exit_code, stdout, stderr = cli(self.argv(out, guard_args), colab, factory)
                self.assertEqual(exit_code, 2)  # a cap end, as before
                self.assertEqual(stdout.strip().splitlines()[-1], "RUN_END reason=cap rc=None stopped=True")
                written = json.loads((out / "runner_result.json").read_text(encoding="utf-8"))
                self.assertEqual(written["reason"], "cap")
                trip = written["cap_trip"]
                self.assertEqual(set(trip), {"code", "message", "hint", "retryable"})
                self.assertEqual(trip["code"], code)
                self.assertEqual((trip["hint"], trip["retryable"]), (CODES[code].hint, CODES[code].retryable))
                if code == "CAP_CEILING_REACHED":
                    self.assertEqual((written["detail"], trip["message"]), ("the cap was reached", "the cap was reached"))
                else:  # the detail is the trip string, unchanged: `CODE: message`
                    self.assertEqual(written["detail"], "%s: %s" % (code, trip["message"]))
                self.assertEqual(written["refusal"], None)
                lines = stderr.splitlines()
                self.assertEqual(lines[-1], "hint: %s" % CODES[code].hint)  # after RUN_END, as for a refusal
                self.assertEqual(sum(ln.startswith("hint: ") for ln in lines), 1)

    def test_a_cap_reached_end_keeps_the_0_1_0_detail_byte_for_byte(self):
        # 0.1.0 (45ecf68e, runner.py:724): `self._end_as(bound, "the %s was reached" % bound)`, bound "cap"
        detail_010 = "the %s was reached" % "cap"
        self.assertEqual(detail_010.encode("utf-8"), b"the cap was reached")
        with tempfile.TemporaryDirectory() as tmp:
            colab = FakeColab(polls=lambda n, env: rec(progress=n))
            runner, _ = make(colab, tmp)
            res = runner.run(script="/c/x.sh", wall_s=36000, guard=QuietGuard())
            self.assertEqual((res.reason, res.detail.encode("utf-8")), ("cap", b"the cap was reached"))
            written = json.loads((runner.out_dir / "runner_result.json").read_bytes())
            self.assertEqual(written["detail"].encode("utf-8"), detail_010.encode("utf-8"))
            self.assertEqual(written["cap_trip"]["code"], "CAP_CEILING_REACHED")
            self.assertEqual(written["cap_trip"]["hint"], CODES["CAP_CEILING_REACHED"].hint)

    def test_a_trip_with_no_leading_code_keeps_its_string_and_has_a_null_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            colab = FakeColab(polls=lambda n, env: rec(progress=n))
            runner, _ = make(colab, tmp)
            res = runner.run(script="/c/x.sh", wall_s=36000, guard=QuietGuard(trip="CAP"))
            self.assertEqual((res.reason, res.detail), ("cap", "CAP"))
            self.assertEqual((res.cap_trip["code"], res.cap_trip["message"]), (None, "CAP"))
            self.assertTrue(res.cap_trip["hint"])

    def test_a_non_cap_end_has_no_cap_trip(self):
        with self.subTest(end="done"), tempfile.TemporaryDirectory() as tmp:
            colab = FakeColab(polls=lambda n, env: rec(rc=0))
            out = Path(tmp) / "out"
            exit_code, stdout, stderr = cli(self.argv(out, ["--max-rate", "1", "--cap", "20", "--cleanup-bound-s",
                                                            "1000"]), colab)
            self.assertEqual(exit_code, 0)
            self.assertIsNone(json.loads((out / "runner_result.json").read_text(encoding="utf-8"))["cap_trip"])
            self.assertNotIn("hint: ", stderr)

        with self.subTest(end="wall, from a trip string"), tempfile.TemporaryDirectory() as tmp:
            # a failed usage read whose retry the wall cuts short: the trip names the nearer limit, the wall
            clock = FakeClock()

            def advance_on_start(kind, fake):
                if kind == "start":
                    clock.advance(6.0)

            colab = FakeColab(clock=clock, on_call=advance_on_start,
                              usage=lambda n: CliResult(1, "", "Connection was lost"))
            runner, _ = make(colab, tmp)
            res = runner.run(script="/c/x.sh", wall_s=10.0, guard=guard(max_rate=1.0, cap=50.0, cleanup_bound_s=600.0))
            self.assertEqual(res.reason, "wall")
            self.assertTrue(res.detail.startswith("COLAB_USAGE_PARSE_FAILED: "), res.detail)
            self.assertIsNone(res.cap_trip)

        with self.subTest(end="error that replaced a cap"), tempfile.TemporaryDirectory() as tmp:
            t = fake_tokens()
            colab = FakeColab(vm_poll_file=lambda n: rec(progress=n), run_log=poisoned(t))
            runner, _ = make(colab, tmp)
            with self.assertRaises(BinaryDownloadRefused):
                runner.run(script="/c/x.sh", wall_s=36000, resident=True, guard=guard(max_rate=3.6, cap=2.0))
            self.assertEqual((runner._res.reason, runner._res.prior_reason), ("error", "cap"))
            self.assertIsNone(runner._res.cap_trip)
            written = json.loads((runner.out_dir / "runner_result.json").read_text(encoding="utf-8"))
            self.assertIsNone(written["cap_trip"])


if __name__ == "__main__":
    unittest.main()
