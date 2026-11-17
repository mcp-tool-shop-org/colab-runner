"""Item 4: the log is drained and the loop never spins. The VM's own chunker (vm/lib.py cr_collect) serves the log,
exec'd in this process against a temp folder (VmLog), so these run on any OS."""

import tempfile
import unittest

from tests.fakes import FakeColab, VmLog, make, rec

CHUNK = 65536


def progress_bar(n: int) -> bytes:
    """A tqdm-style log: n redraws separated by \\r, and no newline at all."""
    return b"".join(b"\r step %6d of %6d" % (i, n) for i in range(n))


class ChunkerTest(unittest.TestCase):
    """The VM side: a full chunk is cut after its last \\n or \\r; a chunk with neither is one line; the offset
    always advances."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.vm = VmLog(self._tmp.name)

    def test_a_full_chunk_with_no_newline_is_returned_as_a_line_and_the_offset_advances(self):
        self.vm.write(b"x" * 70000)
        rec1, lines1 = self.vm.poll(0)
        self.assertEqual((rec1["log_offset"], rec1["log_eof"], lines1), (CHUNK, False, ["x" * CHUNK]))
        rec2, lines2 = self.vm.poll(rec1["log_offset"])
        self.assertEqual((rec2["log_offset"], rec2["log_eof"], lines2), (70000, True, ["x" * (70000 - CHUNK)]))

    def test_a_carriage_return_progress_bar_is_cut_at_a_carriage_return(self):
        data = progress_bar(5000)
        self.vm.write(data)
        rec1, lines1 = self.vm.poll(0)
        self.assertGreater(rec1["log_offset"], 0)
        self.assertEqual(data[rec1["log_offset"] - 1:rec1["log_offset"]], b"\r")
        self.assertTrue(all(ln == "" or ln.startswith(" step") for ln in lines1))

    def test_a_forced_cut_never_splits_a_utf8_character(self):
        self.vm.write("€".encode() * 30000)  # 3 bytes each, 90,000 bytes, no newline
        rec1, lines1 = self.vm.poll(0)
        self.assertEqual(rec1["log_offset"], CHUNK - 1)  # 21,845 whole characters
        self.assertEqual(lines1, ["€" * 21845])
        rec2, lines2 = self.vm.poll(rec1["log_offset"])
        self.assertEqual((lines2, rec2["log_eof"]), (["€" * (30000 - 21845)], True))

    def test_a_full_chunk_with_newlines_is_still_cut_at_the_last_one(self):
        self.vm.write(("y" * 99 + "\n").encode() * 1000)
        rec1, lines1 = self.vm.poll(0)
        self.assertEqual(rec1["log_offset"], 65500)
        self.assertTrue(all(len(ln) == 99 for ln in lines1))


class DrainTest(unittest.TestCase):
    """The rig side: each log advances and ends, a finished job's log is drained in full before `done`, and the
    loop never polls again without the offset advancing or a sleep."""

    def run_log(self, data: bytes, rc_from: int = 1, wall_s: float = 3600):
        with tempfile.TemporaryDirectory() as tmp:
            vm = VmLog(tmp)
            vm.write(data)
            colab = FakeColab(polls=lambda n, env: vm.poll(int(env["CR_POLL_LOG_OFFSET"]),
                                                           rc=0 if n >= rc_from else None))
            runner, clock = make(colab, tmp)
            res = runner.run(script="/content/x.sh", wall_s=wall_s)
            log = (runner.out_dir / "job.run.out").read_text(encoding="utf-8") if (runner.out_dir / "job.run.out").exists() else ""
            offsets = [int(e["CR_POLL_LOG_OFFSET"]) for e in colab.envs if "CR_POLL_LOG_OFFSET" in e]
            return res, log, offsets, clock

    def test_a_64k_log_with_no_newline_advances_and_ends(self):
        res, log, offsets, clock = self.run_log(b"z" * 70000, rc_from=3)
        self.assertEqual(res.reason, "done")
        self.assertEqual(offsets[:3], [0, CHUNK, 70000])
        self.assertEqual(log.replace("\n", ""), "z" * 70000)

    def test_a_carriage_return_progress_bar_log_advances_and_ends(self):
        data = progress_bar(8000)  # 184,000 bytes, only \r
        res, log, offsets, clock = self.run_log(data, rc_from=4)
        self.assertEqual(res.reason, "done")
        self.assertEqual(offsets, sorted(set(offsets)))  # strictly advancing while there was log to read
        self.assertEqual(len([ln for ln in log.splitlines() if ln]), 8000)

    def test_a_multi_chunk_log_is_drained_in_full_after_rc(self):
        data = "".join("line %05d\n" % i for i in range(30000)).encode()  # 330,000 bytes, six chunks
        res, log, offsets, clock = self.run_log(data, rc_from=1)  # the job had already ended at the first poll
        self.assertEqual((res.reason, res.rc), ("done", 0))
        self.assertEqual(log.splitlines(), ["line %05d" % i for i in range(30000)])
        self.assertEqual(len(offsets), 6)
        self.assertEqual(clock.sleeps, [])  # drained back to back, no sleep between chunks

    def test_a_drain_is_still_inside_the_wall(self):
        data = "".join("line %05d\n" % i for i in range(30000)).encode()
        with tempfile.TemporaryDirectory() as tmp:
            vm = VmLog(tmp)
            vm.write(data)

            def polls(n, env):
                # a slow VM: each chunk takes 400 s to come back, or the whole of a shorter (clamped) timeout
                colab.clock.advance(min(400, colab.calls[-1]["timeout"]))
                return vm.poll(int(env["CR_POLL_LOG_OFFSET"]), rc=0)

            colab = FakeColab(polls=polls)
            runner, _ = make(colab, tmp, poll_timeout_s=500)
            res = runner.run(script="/content/x.sh", wall_s=1000)
            self.assertEqual((res.reason, res.rc), ("wall", 0))  # the wall ends the drain; the rc is still kept
            self.assertEqual(colab.poll_count, 3)  # 0-400, 400-800, then the 200 s left
            self.assertAlmostEqual(res.elapsed_s, 1000, delta=1e-6)

    def test_the_loop_never_polls_again_without_the_offset_advancing_or_a_sleep(self):
        # a broken VM that says there is more log but never moves the offset
        colab = FakeColab(polls=lambda n, env: rec(log_eof=False, log_offset=0, progress=n), max_polls=200)
        with tempfile.TemporaryDirectory() as tmp:
            runner, clock = make(colab, tmp)
            res = runner.run(script="/content/x.sh", wall_s=3600)
        self.assertEqual(res.reason, "wall")
        polls = [c["at"] for c in colab.calls if c["kind"] == "poll"]
        self.assertGreater(len(polls), 1)
        for a, b in zip(polls, polls[1:], strict=False):
            self.assertGreater(b, a, "two polls with no sleep between them and no offset advance")

    def test_a_log_reset_on_the_vm_is_read_again_after_a_sleep(self):
        def polls(n, env):
            if n == 1:
                return rec(log_offset=500, log_eof=True)
            return rec(log_offset=100, log_eof=False, rc=0 if n >= 3 else None)  # the log shrank: read from 0

        colab = FakeColab(polls=polls)
        with tempfile.TemporaryDirectory() as tmp:
            runner, clock = make(colab, tmp)
            res = runner.run(script="/content/x.sh", wall_s=3600)
        self.assertEqual(res.reason, "done")
        self.assertEqual(len(clock.sleeps), 2)  # an offset that went back is not an advance: it waits a poll


if __name__ == "__main__":
    unittest.main()
