"""Unit tests for ai_usage_tray.pyw. Run: python test_ai_usage_tray.py"""

import importlib.machinery
import importlib.util
import json
import os
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
_loader = importlib.machinery.SourceFileLoader("ai_usage_tray", str(HERE / "ai_usage_tray.pyw"))
_spec = importlib.util.spec_from_loader("ai_usage_tray", _loader)
tray = importlib.util.module_from_spec(_spec)
_loader.exec_module(tray)

# Captured from `claude -p /usage` on 2026-09-30.
REAL_USAGE = """You are currently using your subscription to power your Claude Code usage

Current session: 17% used \u00b7 resets Sep 30, 6:50pm (America/Los_Angeles)
Current week (all models): 28% used \u00b7 resets Oct 1, 7pm (America/Los_Angeles)
Current week (Fable): 0% used \u00b7 resets Oct 1, 7pm (America/Los_Angeles)

What's contributing to your limits usage?
"""

# Captured from `codex app-server` account/rateLimits/read on 2026-09-30.
REAL_CODEX = {
    "rateLimits": {
        "limitId": "codex",
        "primary": {"usedPercent": 0, "windowDurationMins": 300, "resetsAt": 1790832751},
        "secondary": {"usedPercent": 17, "windowDurationMins": 10080, "resetsAt": 1791152774},
        "individualLimit": {"limit": "2500", "used": "0.0", "remainingPercent": 100,
                            "resetsAt": 1793491200},
        "planType": "team",
    }
}


def envelope(**overrides):
    base = {"type": "result", "is_error": False, "num_turns": 0, "total_cost_usd": 0,
            "result": REAL_USAGE}
    base.update(overrides)
    return json.dumps(base)


def write_script(directory, name, body):
    path = Path(directory) / name
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


class ClaudeParseTest(unittest.TestCase):
    def test_real_output(self):
        windows = tray.parse_claude_usage(REAL_USAGE)
        self.assertEqual([(w.name, w.pct) for w in windows], [("Session", 17), ("Week", 28)])
        self.assertEqual(windows[0].resets, "Sep 30, 6:50pm (America/Los_Angeles)")

    def test_fable_line_ignored(self):
        windows = tray.parse_claude_usage(REAL_USAGE)
        self.assertNotIn(0, [w.pct for w in windows])

    def test_partial_output_marks_missing_window_unknown(self):
        windows = tray.parse_claude_usage("Current week (all models): 5% used")
        self.assertEqual([(w.name, w.pct, w.resets) for w in windows],
                         [("Session", None, ""), ("Week", 5, "")])
        state = tray.ToolState("ok", windows)
        self.assertIsNone(state.binding_pct())   # badge shows ?, not a reassuring 5
        self.assertEqual(state.label(), "Session ?% · Week 5%")

    def test_unparseable(self):
        with self.assertRaises(tray.FetchError):
            tray.parse_claude_usage("something else entirely")


class ClaudeEnvelopeTest(unittest.TestCase):
    def test_zero_turn_ok(self):
        self.assertEqual(tray.check_claude_envelope(envelope())["result"], REAL_USAGE)

    def test_model_turn_is_ambiguous(self):
        with self.assertRaises(tray.AmbiguousClaudeRun):
            tray.check_claude_envelope(envelope(num_turns=1))
        with self.assertRaises(tray.AmbiguousClaudeRun):
            tray.check_claude_envelope(envelope(total_cost_usd=0.2))

    def test_non_json_is_ambiguous(self):
        with self.assertRaises(tray.AmbiguousClaudeRun):
            tray.check_claude_envelope("Warning: no stdin data received\n{...")

    def test_missing_fields_is_ambiguous(self):
        with self.assertRaises(tray.AmbiguousClaudeRun):
            tray.check_claude_envelope(json.dumps({"result": REAL_USAGE}))
        with self.assertRaises(tray.AmbiguousClaudeRun):
            tray.check_claude_envelope(json.dumps([1, 2]))


class ClaudeRunTest(unittest.TestCase):
    """run_claude_usage against fake claude processes."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def fake(self, body):
        return [sys.executable, str(write_script(self.tmp.name, "fake_claude.py", body))]

    def test_success(self):
        argv = self.fake(f"import sys; sys.stdout.write({envelope()!r})")
        self.assertEqual(tray.run_claude_usage(argv), REAL_USAGE)

    def test_stdin_is_closed(self):
        # A child that reads stdin must see EOF at once, not hang.
        argv = self.fake(f"import sys; sys.stdin.read(); sys.stdout.write({envelope()!r})")
        self.assertEqual(tray.run_claude_usage(argv, timeout=10), REAL_USAGE)

    def test_timeout_is_ambiguous(self):
        argv = self.fake("import time; time.sleep(30)")
        with self.assertRaises(tray.AmbiguousClaudeRun):
            tray.run_claude_usage(argv, timeout=1)

    def test_is_error_is_fetch_error(self):
        argv = self.fake(f"import sys; sys.stdout.write({envelope(is_error=True, result='offline')!r})")
        with self.assertRaises(tray.FetchError):
            tray.run_claude_usage(argv)

    def test_missing_exe_is_fetch_error(self):
        with self.assertRaises(tray.FetchError):
            tray.run_claude_usage([str(Path(self.tmp.name) / "nope.exe")])


CONFIG = {"refresh_seconds": 60, "claude_bin": None, "codex_bin": None}


class PollerPauseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)
        self.poller = tray.Poller(CONFIG, state_dir=self.state)
        patcher = mock.patch.object(tray, "resolve_claude", return_value=Path("claude.exe"))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_ambiguous_run_pauses_until_resumed(self):
        with mock.patch.object(tray, "run_claude_usage",
                               side_effect=tray.AmbiguousClaudeRun("timed out")) as run:
            self.assertEqual(self.poller.poll_claude().status, "paused")
            self.assertEqual(self.poller.poll_claude().status, "paused")
            self.assertEqual(run.call_count, 1)  # no second call while paused
        self.poller.resume_claude()
        with mock.patch.object(tray, "run_claude_usage", return_value=REAL_USAGE):
            self.assertEqual(self.poller.poll_claude().status, "ok")

    def test_pause_survives_restart(self):
        with mock.patch.object(tray, "run_claude_usage",
                               side_effect=tray.AmbiguousClaudeRun("turn ran")):
            self.poller.poll_claude()
        restarted = tray.Poller(CONFIG, state_dir=self.state)
        with mock.patch.object(tray, "run_claude_usage") as run:
            self.assertEqual(restarted.poll_claude().status, "paused")
            run.assert_not_called()

    def test_interrupted_call_from_dead_process_pauses(self):
        (self.state / "claude-inflight-999999").write_text("999999")
        with mock.patch.object(tray, "_process_alive", return_value=False):
            poller = tray.Poller(CONFIG, state_dir=self.state)
        self.assertIn("interrupted", poller.claude_paused())
        self.assertFalse((self.state / "claude-inflight-999999").exists())

    def test_live_process_marker_is_left_alone(self):
        (self.state / "claude-inflight-4242").write_text("4242")
        with mock.patch.object(tray, "_process_alive", return_value=True):
            poller = tray.Poller(CONFIG, state_dir=self.state)
        self.assertEqual(poller.claude_paused(), "")
        self.assertTrue((self.state / "claude-inflight-4242").exists())

    def test_inflight_marker_removed_after_classified_call(self):
        with mock.patch.object(tray, "run_claude_usage", return_value=REAL_USAGE):
            self.poller.poll_claude()
        self.assertEqual(list(self.state.glob("claude-inflight-*")), [])

    def test_unreadable_pause_file_fails_closed(self):
        self.state.joinpath("claude-paused.txt").mkdir()   # read_text -> OSError
        self.assertTrue(self.poller.claude_paused())

    def test_fetch_error_does_not_pause(self):
        with mock.patch.object(tray, "run_claude_usage", side_effect=tray.FetchError("x")):
            self.assertEqual(self.poller.poll_claude().status, "error")
        self.assertEqual(self.poller.claude_paused(), "")

    def test_process_alive(self):
        self.assertTrue(tray._process_alive(os.getpid()))
        self.assertFalse(tray._process_alive(0x7FFFFFF0))


class CodexTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def fake_server(self, mode):
        body = f"""
            import json, sys, time
            mode = {mode!r}
            for line in sys.stdin:
                msg = json.loads(line)
                if msg.get("id") == 1:
                    print(json.dumps({{"id": 1, "result": {{}}}}), flush=True)
                if msg.get("id") == 2:
                    if mode == "exit":
                        sys.exit(4)
                    if mode == "hang":
                        time.sleep(60)
                    if mode == "error":
                        print(json.dumps({{"id": 2, "error": {{"message": "nope"}}}}), flush=True)
                    else:
                        print("not json", flush=True)
                        print(json.dumps({{"method": "noise"}}), flush=True)
                        print(json.dumps({{"id": 2, "result": {json.dumps(REAL_CODEX)}}}), flush=True)
        """
        return [sys.executable, str(write_script(self.tmp.name, "fake_codex.py", body))]

    def test_answer(self):
        result = tray.fetch_codex_rate_limits(self.fake_server("ok"), timeout=10)
        self.assertEqual(result, REAL_CODEX)

    def test_error_reply(self):
        with self.assertRaises(tray.FetchError):
            tray.fetch_codex_rate_limits(self.fake_server("error"), timeout=10)

    def test_exit_early(self):
        with self.assertRaises(tray.FetchError):
            tray.fetch_codex_rate_limits(self.fake_server("exit"), timeout=10)

    def test_hang_times_out_and_kills(self):
        start = time.monotonic()
        with self.assertRaises(tray.FetchError):
            tray.fetch_codex_rate_limits(self.fake_server("hang"), timeout=1)
        self.assertLess(time.monotonic() - start, 15)

    def test_parse_real(self):
        windows, extra = tray.parse_codex(REAL_CODEX)
        self.assertEqual([(w.name, w.pct) for w in windows],
                         [("Session", 0), ("Week", 17), ("Month", 0)])
        self.assertIn("Plan: team", extra)
        self.assertIn("Credits: 0.0 of 2500 used", extra)

    def test_parse_sorts_and_names_windows(self):
        result = {"rateLimits": {
            "primary": {"usedPercent": 40.6, "windowDurationMins": 10080},
            "secondary": {"usedPercent": 3, "windowDurationMins": 120}}}
        windows, _ = tray.parse_codex(result)
        self.assertEqual([(w.name, w.pct) for w in windows], [("2h", 3), ("Week", 41)])

    def test_parse_garbage(self):
        for bad in ({"nope": 1}, {"rateLimits": {}}, {"rateLimits": None}, None, [1],
                    {"rateLimits": {"primary": "x"}}, {"rateLimits": {"primary": [1]}}):
            with self.subTest(bad=bad), self.assertRaises(tray.FetchError):
                tray.parse_codex(bad)

    def test_parse_tolerates_odd_fields(self):
        result = {"rateLimits": {
            "primary": {"usedPercent": "lots", "windowDurationMins": "300", "resetsAt": "soon"},
            "individualLimit": "n/a"}}
        windows, _ = tray.parse_codex(result)
        self.assertEqual([(w.name, w.pct, w.resets) for w in windows], [("Window", None, "")])
        self.assertIsNone(tray.ToolState("ok", windows).binding_pct())

    def test_reply_without_result_object(self):
        body = """
            import json, sys
            for line in sys.stdin:
                if json.loads(line).get("id") == 2:
                    print(json.dumps([2]), flush=True)
                    print(json.dumps({"id": 2, "result": None}), flush=True)
        """
        argv = [sys.executable, str(write_script(self.tmp.name, "fake_codex2.py", body))]
        with self.assertRaises(tray.FetchError):
            tray.fetch_codex_rate_limits(argv, timeout=10)


class FormattingTest(unittest.TestCase):
    def test_label_and_binding(self):
        state = tray.ToolState("ok", tray.parse_claude_usage(REAL_USAGE))
        self.assertEqual(state.label(), "Session 17% \u00b7 Week 28%")
        self.assertEqual(state.binding_pct(), 28)

    def test_unknown_states(self):
        self.assertIsNone(tray.ToolState("error").binding_pct())
        self.assertIn("fetch failed", tray.ToolState("error").label())
        self.assertIn("paused", tray.ToolState("paused").label())

    def test_tooltip_limit(self):
        state = tray.ToolState("ok", [tray.Window("W" * 200, 5)])
        self.assertLessEqual(len(tray.tooltip("Claude", state)), tray.TOOLTIP_MAX_CHARS)

    def test_level_colours(self):
        self.assertEqual(tray.level_colour(0), (60, 170, 80))
        self.assertEqual(tray.level_colour(49), (60, 170, 80))
        self.assertEqual(tray.level_colour(50), (230, 160, 20))
        self.assertEqual(tray.level_colour(80), (220, 50, 47))

    def test_format_epoch(self):
        epoch = time.mktime((2026, 9, 30, 18, 5, 0, 0, 0, -1))
        self.assertEqual(tray.format_epoch(epoch), "6:05 PM on 30 Sep")
        epoch = time.mktime((2026, 10, 1, 0, 30, 0, 0, 0, -1))
        self.assertEqual(tray.format_epoch(epoch), "12:30 AM on 1 Oct")
        self.assertEqual(tray.format_epoch(None), "")

    def test_render_badge(self):
        for state in (tray.ToolState("ok", [tray.Window("Week", 100)]),
                      tray.ToolState("error"), tray.ToolState("paused")):
            image = tray.render_badge(state, tray.CLAUDE_COLOUR)
            self.assertEqual(image.size, (64, 64))


class ResolutionTest(unittest.TestCase):
    def test_claude_never_returns_cmd_shim(self):
        with tempfile.TemporaryDirectory() as tmp:
            shim = Path(tmp) / "claude.CMD"
            shim.write_text("@echo off")
            with mock.patch.dict(os.environ, {"APPDATA": tmp}), \
                    mock.patch.object(tray.shutil, "which", return_value=None), \
                    mock.patch.object(tray.Path, "home", return_value=Path(tmp)):
                self.assertIsNone(tray.resolve_claude(str(shim)))

    def test_codex_newest_build(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = Path(tmp) / "OpenAI" / "Codex" / "bin" / "aaa" / "codex.exe"
            new = Path(tmp) / "OpenAI" / "Codex" / "bin" / "bbb" / "codex.exe"
            for i, path in enumerate((old, new)):
                path.parent.mkdir(parents=True)
                path.write_text("x")
                os.utime(path, (1000 + i, 1000 + i))
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": tmp}), \
                    mock.patch.object(tray.shutil, "which", return_value=None):
                self.assertEqual(tray.resolve_codex(), new)


class NpmCodexTest(unittest.TestCase):
    """Layouts from @openai/codex 0.159.3 bin/codex.js (nested and hoisted)."""

    def check(self, relative):
        with tempfile.TemporaryDirectory() as tmp:
            exe = Path(tmp) / relative
            exe.parent.mkdir(parents=True)
            exe.write_text("x")
            (Path(tmp) / "codex.cmd").write_text("@echo off")
            with mock.patch.dict(os.environ, {"APPDATA": tmp, "LOCALAPPDATA": tmp}),                     mock.patch.object(tray.shutil, "which",
                                      side_effect=lambda n: None if n.endswith(".exe")
                                      else str(Path(tmp) / "codex.cmd")):
                self.assertEqual(tray.resolve_codex(), exe)

    def test_nested(self):
        self.check("node_modules/@openai/codex/node_modules/@openai/codex-win32-x64/"
                   "vendor/x86_64-pc-windows-msvc/bin/codex.exe")

    def test_hoisted(self):
        self.check("node_modules/@openai/codex-win32-arm64/vendor/aarch64-pc-windows-msvc/bin/codex.exe")

    def test_legacy_vendor(self):
        self.check("node_modules/@openai/codex/vendor/x86_64-pc-windows-msvc/bin/codex.exe")


class ConfigTest(unittest.TestCase):
    def test_invalid_config_uses_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            with mock.patch.object(tray, "CONFIG_FILE", path):
                path.write_text("{not json")
                self.assertEqual(tray.load_config()["refresh_seconds"], 60)
                path.write_text(json.dumps({"refresh_seconds": 5}))
                self.assertEqual(tray.load_config()["refresh_seconds"], 60)
                path.write_text(json.dumps({"refresh_seconds": 120, "codex_bin": "C:/x.exe"}))
                config = tray.load_config()
                self.assertEqual((config["refresh_seconds"], config["codex_bin"]), (120, "C:/x.exe"))


if __name__ == "__main__":
    unittest.main()
