#!/usr/bin/env python3

import contextlib
import io
import json
import os
import unittest

import rsyncy


class FakeStream(io.StringIO):
    def __init__(self, tty=False):
        super().__init__()
        self._tty = tty

    def isatty(self):
        return self._tty


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


class ConfigTests(unittest.TestCase):
    def cfg(self, *args, name="rsyncy", env=None):
        return rsyncy.RsyncyConfig.parse([name, *args], env=env or {})

    def test_legacy_stat_command_name(self):
        self.assertTrue(self.cfg(name="rsyncy-stat").status_only)
        self.assertFalse(self.cfg(name="rsyncy").status_only)

    def test_formal_status_only_flag(self):
        c = self.cfg("--status-only", "-a", "src/", "dst")
        self.assertTrue(c.status_only)
        self.assertEqual(c.rsync_args, ["-a", "src/", "dst"])

    def test_no_progress(self):
        self.assertEqual(self.cfg("--no-progress").progress, "off")

    def test_progress_forms(self):
        self.assertEqual(self.cfg("--progress", "line").progress, "line")
        self.assertEqual(self.cfg("--progress=line").progress, "line")

    def test_invalid_progress(self):
        with self.assertRaises(rsyncy.ConfigError):
            self.cfg("--progress=nope")

    def test_status_fd_forms(self):
        self.assertEqual(self.cfg("--status-fd", "3").status_fd, 3)
        self.assertEqual(self.cfg("--status-fd=3").status_fd, 3)

    def test_double_dash_passthrough(self):
        c = self.cfg("--status-only", "--", "--status-only", "-a")
        self.assertTrue(c.status_only)
        self.assertEqual(c.rsync_args, ["--status-only", "-a"])

    def test_env_overrides(self):
        c = self.cfg(
            env={
                "RSYNCY_STATUS_ONLY": "1",
                "RSYNCY_PROGRESS": "line",
                "RSYNCY_STATUS_FD": "4",
                "RSYNCY_STATUS_INTERVAL": "1.0",
            }
        )
        self.assertTrue(c.status_only)
        self.assertEqual(c.progress, "line")
        self.assertEqual(c.status_fd, 4)
        self.assertEqual(c.status_interval, 1.0)

    def test_rsync_options_stop_parsing(self):
        c = self.cfg("-a", "--status-only")
        self.assertFalse(c.status_only)
        self.assertEqual(c.rsync_args, ["-a", "--status-only"])


class TTYProbeTests(unittest.TestCase):
    def test_probe(self):
        info = rsyncy.TTYInfo.probe(
            FakeStream(True), FakeStream(False), FakeStream(True)
        )
        self.assertTrue(info.stdin)
        self.assertFalse(info.stdout)
        self.assertTrue(info.stderr)


class OutputPlanTests(unittest.TestCase):
    def make_plan(self, args=(), ttys=(False, False, False), env=None, clock=None):
        clock = FakeClock() if clock is None else clock
        config = rsyncy.RsyncyConfig.parse(["rsyncy", *args], env=env or {})
        tty = rsyncy.TTYInfo(*ttys)
        stdout, stderr = FakeStream(ttys[1]), FakeStream(ttys[2])
        plan = rsyncy.OutputPlan.build(config, tty, stdout, stderr, clock=clock)
        return plan, stdout, stderr, clock

    def test_tty_stdout_interactive_colocated(self):
        plan, out, err, clock = self.make_plan(ttys=(True, True, False))
        self.assertIsInstance(plan.status_sink, rsyncy.InteractiveStatusSink)
        self.assertTrue(plan.status_sink.co_located)
        self.assertIs(plan.status_stream, out)

    def test_tty_stderr_when_stdout_pipe(self):
        plan, out, err, clock = self.make_plan(ttys=(False, False, True))
        self.assertIsInstance(plan.status_sink, rsyncy.InteractiveStatusSink)
        self.assertFalse(plan.status_sink.co_located)
        self.assertIs(plan.status_stream, err)

    def test_no_tty_uses_line_backend_on_stderr(self):
        plan, out, err, clock = self.make_plan(ttys=(False, False, False))
        self.assertIsInstance(plan.status_sink, rsyncy.LineStatusSink)
        self.assertIs(plan.status_stream, err)

    def test_progress_off(self):
        plan, out, err, clock = self.make_plan(
            args=("--no-progress",), ttys=(True, True, True)
        )
        self.assertIsInstance(plan.status_sink, rsyncy.NullStatusSink)

    def test_force_tty_without_tty_disables(self):
        plan, out, err, clock = self.make_plan(args=("--progress=tty",))
        self.assertIsInstance(plan.status_sink, rsyncy.NullStatusSink)
        self.assertIn("no TTY", err.getvalue())

    def test_force_line(self):
        plan, out, err, clock = self.make_plan(
            args=("--progress=line",), ttys=(True, True, True)
        )
        self.assertIsInstance(plan.status_sink, rsyncy.LineStatusSink)
        self.assertIs(plan.status_stream, err)

    def test_status_only_uses_stdout_and_suppresses_records(self):
        plan, out, err, clock = self.make_plan(
            args=("--status-only",), ttys=(False, False, False)
        )
        self.assertIsInstance(plan.status_sink, rsyncy.LineStatusSink)
        self.assertIs(plan.status_stream, out)
        plan.write_record("secret.txt")
        self.assertEqual(out.getvalue(), "")

    def test_stdout_data_pipe_contract(self):
        plan, out, err, clock = self.make_plan(ttys=(False, False, False))
        plan.write_record("file1.txt")
        plan.update_status("50% status", 0, {"percent": 0.5})
        plan.write_record("file2.txt")
        plan.close(0)

        # stdout: plain, line-delimited records only
        self.assertEqual(out.getvalue(), "file1.txt\nfile2.txt\n")
        for banned in ("\r", "\033", "rsyncy:"):
            self.assertNotIn(banned, out.getvalue())

        # human status backend is on stderr, also plain and line delimited
        self.assertEqual(err.getvalue(), "50% status\n")

    def test_diagnostics_go_to_stderr(self):
        plan, out, err, clock = self.make_plan(ttys=(False, False, False))
        plan.diagnostic("boom")
        self.assertEqual(out.getvalue(), "")
        self.assertIn("rsyncy: error: boom\n", err.getvalue())

    def test_usage_goes_to_stderr(self):
        plan, out, err, clock = self.make_plan(ttys=(True, False, False))
        plan.usage()
        self.assertEqual(out.getvalue(), "")
        self.assertIn("Usage:", err.getvalue())


class LineStatusSinkTests(unittest.TestCase):
    def test_rate_limited_plain_and_line_delimited(self):
        stream = io.StringIO()
        clock = FakeClock()
        sink = rsyncy.LineStatusSink(stream, 0.5, clock)

        self.assertTrue(sink.render("frame1", 0))
        self.assertFalse(sink.render("frame2", 0))
        self.assertFalse(sink.render("frame3", 0))
        self.assertEqual(stream.getvalue(), "frame1\n")

        clock.advance(0.5)
        self.assertTrue(sink.render("frame4", 0))
        self.assertEqual(stream.getvalue(), "frame1\nframe4\n")

        for banned in ("\r", "\033"):
            self.assertNotIn(banned, stream.getvalue())

    def test_finish_flushes_pending_frame(self):
        stream = io.StringIO()
        clock = FakeClock()
        sink = rsyncy.LineStatusSink(stream, 0.5, clock)
        sink.render("frame1", 0)
        sink.render("pending", 0)
        self.assertTrue(sink.finish())
        self.assertEqual(stream.getvalue(), "frame1\npending\n")
        self.assertFalse(sink.finish())


class InteractiveStatusSinkTests(unittest.TestCase):
    def test_frame_uses_carriage_returns_and_clears(self):
        stream = FakeStream(tty=True)
        sink = rsyncy.InteractiveStatusSink(stream)
        sink.attach_style("", rsyncy.CLI.style.reset)
        sink.render("status", 3)
        output = stream.getvalue()
        self.assertIn("\r", output)
        # standard CSI sequences as literals, not via the constant under test
        self.assertIn("\033[0K", output)
        self.assertIn("\033[C" * 3, output)

    def test_clear_erases_frame(self):
        stream = FakeStream(tty=True)
        sink = rsyncy.InteractiveStatusSink(stream)
        sink.clear()
        self.assertEqual(stream.getvalue(), "\r\033[0K")


class StatusFDTests(unittest.TestCase):
    def _build(self, argv_extra=()):
        r, w = os.pipe()
        config = rsyncy.RsyncyConfig.parse(["rsyncy", *argv_extra], env={})
        # replace the placeholder FD with the real write end
        config.status_fd = w
        tty = rsyncy.TTYInfo(False, False, False)
        stdout, stderr = FakeStream(False), FakeStream(False)
        plan = rsyncy.OutputPlan.build(config, tty, stdout, stderr)
        return r, w, plan, stdout, stderr

    def test_jsonl_events_on_independent_fd(self):
        r, w, plan, stdout, stderr = self._build()
        try:
            plan.write_record("file1.txt")
            plan.update_status(
                "50%",
                5,
                {
                    "percent": 0.5,
                    "transferred": "100",
                    "speed": "1MB/s",
                    "elapsed": "0:00:01",
                    "xfr": "1",
                    "scan": None,
                    "scan_finished": False,
                },
            )
            plan.close(0)
        finally:
            os.close(w)

        data = os.read(r, 65536).decode()
        os.close(r)

        events = [json.loads(line) for line in data.splitlines()]
        self.assertEqual(
            [e["type"] for e in events], ["start", "record", "status", "end"]
        )
        self.assertEqual(events[0]["mode"], "normal")
        self.assertEqual(events[1]["line"], "file1.txt")
        self.assertEqual(events[2]["percent"], 0.5)
        self.assertEqual(events[3]["returncode"], 0)

        # independent: machine events never land on stdout/stderr
        self.assertNotIn('{"type"', stdout.getvalue())
        self.assertNotIn('{"type"', stderr.getvalue())

    def test_parse_error_is_diagnostic_and_event(self):
        r, w, plan, stdout, stderr = self._build()
        rstyle = rsyncy.build_rstyle(1, plain=True)
        rs = rsyncy.Rsyncy(rstyle, plan)
        rs.parse_stat("bad percent x 1 2")
        plan.close(0)
        os.close(w)

        data = os.read(r, 65536).decode()
        os.close(r)

        types = [json.loads(line)["type"] for line in data.splitlines()]
        self.assertIn("diagnostic", types)
        self.assertIn("rsyncy: error", stderr.getvalue())
        self.assertEqual(stdout.getvalue(), "")

    def test_status_only_still_emits_records_as_events(self):
        r, w = os.pipe()
        config = rsyncy.RsyncyConfig.parse(["rsyncy", "--status-only"], env={})
        config.status_fd = w
        tty = rsyncy.TTYInfo(False, False, False)
        stdout, stderr = FakeStream(False), FakeStream(False)
        plan = rsyncy.OutputPlan.build(config, tty, stdout, stderr)

        plan.write_record("file1.txt")
        plan.close(0)
        os.close(w)

        data = os.read(r, 65536).decode()
        os.close(r)

        events = [json.loads(line) for line in data.splitlines()]
        self.assertEqual(events[0]["mode"], "status-only")
        self.assertEqual(events[1]["type"], "record")
        self.assertEqual(events[1]["line"], "file1.txt")
        self.assertNotIn("file1.txt", stdout.getvalue())


class ReadIntegrationTests(unittest.TestCase):
    def test_read_end_to_end(self):
        r, w = os.pipe()
        config = rsyncy.RsyncyConfig.parse(["rsyncy"], env={})
        tty = rsyncy.TTYInfo(False, False, False)
        stdout, stderr = FakeStream(False), FakeStream(False)
        plan = rsyncy.OutputPlan.build(config, tty, stdout, stderr, clock=FakeClock())
        rstyle = rsyncy.build_rstyle(1, plain=True)
        rs = rsyncy.Rsyncy(rstyle, plan)

        payload = (
            b"somedir/\n"
            b"file1.txt\n"
            b"6,672,528  96%    1.04MB/s    0:00:06 (xfr#1, to-chk=7/12)\r"
        )
        os.write(w, payload)
        os.close(w)

        rs.read(r)
        plan.close(0)

        self.assertEqual(stdout.getvalue(), "file1.txt\n")
        status_lines = stderr.getvalue().splitlines()
        self.assertTrue(any("96%" in line for line in status_lines))
        for line in status_lines:
            self.assertNotIn("\033", line)
            self.assertNotIn("\r", line)


class MainTests(unittest.TestCase):
    def test_invalid_option_returns_2(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = rsyncy.main(["rsyncy", "--progress=bad"])
        self.assertEqual(code, 2)
        self.assertIn("rsyncy: error", err.getvalue())


if __name__ == "__main__":
    unittest.main()
