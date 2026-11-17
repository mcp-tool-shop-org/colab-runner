"""Ship gate (B1, B3): every named code has a hint and a `retryable` value, read from the source (not a hand list);
refusals and errors print `CODE: message` then `hint: ...`; the log levels; no token at any level."""

import ast
import contextlib
import io
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from colab_runner.__main__ import main
from colab_runner.errors import CODES, Refused, cap_trip_record, error_record
from colab_runner.window_lib import launch_guards as lg
from tests.fakes import FakeClock, rec
from tests.test_tokens import TokenLeakingColab, fake_tokens, leak_lines, values

PKG = Path(__file__).resolve().parent.parent / "colab_runner"
# Where a code enters the source: the argument index of the code in each constructor, and any `code = "..."`.
CODE_ARG = {"Refused": 0, "require_number": 0, "error_record": 0, "lookup": 0, "_end_refused": 1}


def _callee(node: ast.Call) -> str:
    f = node.func
    return f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else ""


def source_codes() -> tuple[dict[str, list[str]], list[str]]:
    """({code: [file:line, ...]}, [file:function of each Refused(...) whose code is not a literal]), over every .py
    file in colab_runner/ but the vendored window_lib/."""
    codes: dict[str, list[str]] = {}
    dynamic: list[str] = []
    for path in sorted(PKG.rglob("*.py")):
        rel = path.relative_to(PKG.parent).as_posix()
        if rel.startswith("colab_runner/window_lib/"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), rel)
        funcs = [(f.lineno, f.end_lineno, f.name) for f in ast.walk(tree) if isinstance(f, ast.FunctionDef)]

        def where(line, funcs=funcs):
            inner = [f for f in funcs if f[0] <= line <= f[1]]
            return min(inner, key=lambda f: f[1] - f[0])[2] if inner else "<module>"

        for node in ast.walk(tree):
            found = None
            if isinstance(node, ast.Call) and _callee(node) in CODE_ARG:
                i = CODE_ARG[_callee(node)]
                arg = node.args[i] if len(node.args) > i else None
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    found = arg.value
                elif _callee(node) == "Refused":
                    dynamic.append("%s:%s" % (rel, where(node.lineno)))
            elif isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) \
                    and isinstance(node.value.value, str):
                if any((isinstance(t, ast.Name) and t.id == "code") or (isinstance(t, ast.Attribute) and t.attr == "code")
                       for t in node.targets):
                    found = node.value.value
            if found is not None:
                codes.setdefault(found, []).append("%s:%d" % (rel, node.lineno))
    return codes, dynamic


def library_refusal_codes() -> set[str]:
    """The codes the vendored library's evaluate_cap_budget can refuse with (spend.py raises them as Refused)."""
    import inspect

    return set(re.findall(r'"([A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+)"', inspect.getsource(lg.evaluate_cap_budget)))


def _function(path: Path, name: str) -> ast.FunctionDef:
    tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
    return next(f for f in ast.walk(tree) if isinstance(f, ast.FunctionDef) and f.name == name)


def trip_codes() -> set[str]:
    """The codes a cap trip can carry (runner_result.json `cap_trip`), read from the source: the literal `CODE:`
    prefixes of SpendGuard.check in spend.py, and, because check formats its cap record's code (`rec["code"]`, from
    SpendGuard.cap_record, which is the library's evaluate_cap_guard), every code evaluate_cap_guard sets."""
    import inspect

    spend = PKG / "spend.py"
    check = _function(spend, "check")
    codes = set()
    for node in ast.walk(check):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            m = re.match(r"([A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+): ", node.value)
            if m:
                codes.add(m.group(1))
    check_src = ast.unparse(check)
    record_src = ast.unparse(_function(spend, "cap_record"))
    if "self.cap_record(" in check_src and "rec['code']" in check_src and "lg.evaluate_cap_guard(" in record_src:
        codes |= set(re.findall(r'"([A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+)"', inspect.getsource(lg.evaluate_cap_guard)))
    return codes


class CodeTableTest(unittest.TestCase):
    def test_every_code_in_the_source_has_a_hint_and_a_retryable_value(self):
        codes, dynamic = source_codes()
        self.assertGreater(len(codes), 25)  # the collector sees the source
        for code, where in sorted(codes.items()):
            with self.subTest(code=code, where=where):
                self.assertRegex(code, r"^[A-Z][A-Z0-9]*(_[A-Z0-9]+)+$")
                self.assertIn(code, CODES)
                self.assertIsInstance(CODES[code].hint, str)
                self.assertTrue(CODES[code].hint.strip())
                self.assertNotIn("\n", CODES[code].hint)  # one line: the CLI prints it as `hint: ...`
                self.assertIsInstance(CODES[code].retryable, bool)

    def test_the_one_dynamic_refusal_is_the_librarys_budget_and_its_codes_are_in_the_table(self):
        _, dynamic = source_codes()
        # require_number passes on its callers' literal codes (collected above); SpendGuard raises the library's
        self.assertEqual(sorted(dynamic), ["colab_runner/runner.py:require_number",
                                           "colab_runner/spend.py:__post_init__"])
        lib = library_refusal_codes()
        self.assertEqual(lib, {"CAP_TOO_SMALL_FOR_CLEANUP"})
        budget = lg.evaluate_cap_budget(cap=1.0, margin=1.0, cleanup_bound_s=0.0, priced_rate=1.0, max_rate=1.0)
        self.assertIn(budget["code"], lib)
        for code in lib:
            self.assertIn(code, CODES)

    def test_every_cap_trip_code_in_the_source_has_a_hint_and_a_retryable_value(self):
        trips = trip_codes()
        # the collector sees spend.py's own prefixes and the library's cap-guard codes that check() passes on
        self.assertEqual(trips, {"RATE_ABOVE_MAX", "ASSIGNMENTS_UNREADABLE", "CAP_GUARD_TRIPPED",
                                 "COLAB_USAGE_PARSE_FAILED"})
        codes, _ = source_codes()
        self.assertIn("CAP_CEILING_REACHED", codes)  # the runner's own cap end (the ceiling reached by the clock)
        for code in sorted(trips | {"CAP_CEILING_REACHED"}):
            with self.subTest(code=code):
                self.assertIn(code, CODES)
                self.assertTrue(CODES[code].hint.strip())
                self.assertNotIn("\n", CODES[code].hint)  # one line: the CLI prints it as `hint: ...`
                self.assertIsInstance(CODES[code].retryable, bool)
                rec_ = cap_trip_record("%s: what happened" % code)
                self.assertEqual(rec_, error_record(code, "what happened"))

    def test_the_table_has_no_code_the_source_never_uses(self):
        codes, _ = source_codes()
        self.assertEqual(sorted(set(CODES) - set(codes) - library_refusal_codes() - trip_codes()), [])

    def test_a_refusal_carries_code_message_hint_and_retryable(self):
        exc = Refused("OUT_DIR_NOT_EMPTY", "x exists")
        self.assertEqual(exc.record(), {"code": "OUT_DIR_NOT_EMPTY", "message": "x exists",
                                        "hint": CODES["OUT_DIR_NOT_EMPTY"].hint, "retryable": False})
        self.assertEqual((str(exc), exc.why), ("OUT_DIR_NOT_EMPTY: x exists", "x exists"))
        self.assertEqual(error_record("STOP_NOT_CONFIRMED", "m")["retryable"], True)
        self.assertTrue(Refused("NO_SUCH_CODE_ANYWHERE", "m").hint)  # an unknown code still prints a hint


def cli(argv, colab):
    """main(argv) on `colab`, its Runner on a fake clock; returns (code, stdout, stderr)."""
    from colab_runner.runner import Runner

    clock = colab.clock = colab.clock or FakeClock()
    out, err = io.StringIO(), io.StringIO()
    with mock.patch("colab_runner.__main__.cli_from_env", return_value=colab), \
            mock.patch("colab_runner.__main__.Runner",
                       side_effect=lambda *a, **kw: Runner(*a, clock=clock.now, sleep=clock.sleep, **kw)), \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(argv)
    return code, out.getvalue(), err.getvalue()


class LogLevelTest(unittest.TestCase):
    """B3: --quiet, normal, --verbose and --debug, each with a token in a CLI error line, a log line and a traceback."""

    LEVELS = {"quiet": ["--quiet"], "normal": [], "verbose": ["--verbose"], "debug": ["--debug"]}

    def leaky_run(self, tmp, flags):
        t = fake_tokens()

        def polls(n, env):
            if n == 1:
                return rec(phase=leak_lines(t)[2])  # a poll record carrying a token: it reaches the --verbose line
            raise RuntimeError("crashed at " + " ".join(leak_lines(t)))  # the RUN_ERROR line and the traceback

        colab = TokenLeakingColab(t, polls=polls, starts=["drop"])  # the failed start's error reaches the log
        argv = ["run", "--session", "s", "--out-dir", str(Path(tmp) / "out"), "--script", "/c/x.sh", "--wall-s",
                "3600", "--max-rate", "1", "--cap", "5", "--cleanup-bound-s", "600"] + flags
        return t, cli(argv, colab)

    def test_each_level_prints_what_it_should_and_no_token(self):
        for level, flags in self.LEVELS.items():
            with self.subTest(level=level), tempfile.TemporaryDirectory() as tmp:
                t, (code, out, err) = self.leaky_run(tmp, flags)
                self.assertEqual(code, 2)
                for v in values(t):
                    self.assertNotIn(v, out + err)
                lines = err.splitlines()
                self.assertTrue(any(ln.startswith("RUN_ERROR: UNEXPECTED_ERROR: RuntimeError: crashed at") for ln in lines))
                self.assertIn("hint: read runner_result.json", err)
                self.assertIn("Bearer [REDACTED]", err)  # the error line had the token, and was scrubbed
                has_log = any(ln.startswith("start attempt 1 failed") for ln in lines)
                has_poll = any(ln.startswith("poll ok:") for ln in lines)
                has_tb = "Traceback (most recent call last)" in err
                self.assertEqual(
                    (has_log, has_poll, has_tb),
                    {"quiet": (False, False, False), "normal": (True, False, False),
                     "verbose": (True, True, False), "debug": (True, True, True)}[level],
                )
                if level == "quiet":
                    self.assertEqual(len(lines), 2, err)  # only the error and its hint
                if has_log:
                    self.assertIn("hf_[REDACTED]", next(ln for ln in lines if ln.startswith("start attempt 1")))
                if has_poll:
                    self.assertIn("Bearer [REDACTED]", next(ln for ln in lines if ln.startswith("poll ok:")))
                if has_tb:
                    tb = err[err.index("Traceback"):]
                    self.assertIn("RuntimeError: crashed at", tb)
                    self.assertIn("colab-runtime-proxy-token=[REDACTED]", tb)

    def test_stop_takes_the_levels_too(self):
        t = fake_tokens()
        for level, flags in self.LEVELS.items():
            with self.subTest(level=level), tempfile.TemporaryDirectory() as tmp:
                (Path(tmp) / "session_open.json").write_text(json.dumps({"session": "s1"}))
                code, out, err = cli(["stop", "--out-dir", tmp] + flags, TokenLeakingColab(t, stops=[1, 1, 1]))
                self.assertEqual(code, 2)
                self.assertIn("STOP_END session=s1 confirmed=False", out)
                for v in values(t):
                    self.assertNotIn(v, out + err)
                self.assertEqual(any(ln.startswith("stop attempt 1 failed") for ln in err.splitlines()),
                                 level != "quiet")

    def test_a_stop_error_traceback_only_at_debug_and_scrubbed(self):
        from colab_runner.runner import Runner

        t = fake_tokens()
        for level, flags in self.LEVELS.items():
            with self.subTest(level=level), tempfile.TemporaryDirectory() as tmp:
                (Path(tmp) / "session_open.json").write_text(json.dumps({"session": "s1"}))
                with mock.patch.object(Runner, "stop_session", side_effect=OSError(" ".join(leak_lines(t)))):
                    code, out, err = cli(["stop", "--out-dir", tmp] + flags, TokenLeakingColab(t))
                self.assertEqual(code, 2)
                self.assertIn("STOP_ERROR: UNEXPECTED_ERROR: OSError:", err)
                self.assertEqual("Traceback (most recent call last)" in err, level == "debug")
                for v in values(t):
                    self.assertNotIn(v, out + err)


if __name__ == "__main__":
    unittest.main()
