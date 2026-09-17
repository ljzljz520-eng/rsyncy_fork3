#!/usr/bin/env python3

import json
import os
import queue
import re
import select
import subprocess
import sys
import time
import types
from datetime import datetime
from threading import Thread

_re_chk = re.compile(r"(..)-.+=(\d+)/(\d+)")

PROGRESS_MODES = ("auto", "tty", "line", "off")
DEFAULT_STATUS_INTERVAL = 0.5

# start inline from laktakpy


class CLI:
    NO_COLOR = os.environ.get("NO_COLOR", "")

    class style:
        reset = "\033[0m"
        bold = "\033[01m"

    class esc:
        right = "\033[C"

        @staticmethod
        def clear_line(opt=0):
            # 0=to end, 1=from start, 2=all
            return "\033[" + str(opt) + "K"

    @staticmethod
    def get_col_bits():
        if CLI.NO_COLOR:
            return 1
        c, t = os.environ.get("COLORTERM", ""), os.environ.get("TERM", "")
        if c in ["truecolor", "24bit"]:
            return 24
        elif c == "8bit" or "256" in t:
            return 8
        else:
            return 4

    # 4bit system colors
    @staticmethod
    def fg4(col):
        # black=0,red=1,green=2,orange=3,blue=4,purple=5,cyan=6,lightgrey=7
        # darkgrey=8,lightred=9,lightgreen=10,yellow=11,lightblue=12,pink=13,lightcyan=14
        if CLI.NO_COLOR:
            return ""
        else:
            return f"\033[{(30+col) if col<8 else (90-8+col)}m"

    @staticmethod
    def bg4(col):
        if CLI.NO_COLOR:
            return ""
        else:
            return f"\033[{40+col}m"

    # 8bit xterm colors
    @staticmethod
    def fg8(col):
        if CLI.NO_COLOR:
            return ""
        else:
            return f"\033[38;5;{col}m"

    @staticmethod
    def bg8(col):
        if CLI.NO_COLOR:
            return ""
        else:
            return f"\033[48;5;{col}m"

    @staticmethod
    def get_size(stream):
        # returns {columns, lines}
        try:
            if stream.isatty():
                return os.get_terminal_size()
        except (ValueError, OSError):
            pass
        return types.SimpleNamespace(columns=80, lines=40)


# end inline from laktakpy


class ConfigError(Exception):
    pass


def _env_true(value):
    return str(value).strip().lower() not in ("", "0", "false", "no")


class RsyncyConfig:
    """Startup configuration: formal CLI modes plus legacy argv[0] handling."""

    def __init__(
        self,
        invoked_name,
        status_only,
        progress,
        status_fd,
        status_interval,
        rsync_args,
    ):
        self.invoked_name = invoked_name
        self.status_only = status_only
        self.progress = progress
        self.status_fd = status_fd
        self.status_interval = status_interval
        self.rsync_args = rsync_args

    @classmethod
    def parse(cls, argv, env=None):
        # argv is sys.argv (includes the program name)
        argv = list(argv)
        env = os.environ if env is None else env
        invoked_name = os.path.basename(argv[0]) if argv else "rsyncy"

        # legacy compatibility layer
        status_only = invoked_name == "rsyncy-stat"
        if env.get("RSYNCY_STATUS_ONLY"):
            status_only = _env_true(env["RSYNCY_STATUS_ONLY"])

        progress = env.get("RSYNCY_PROGRESS", "auto")
        status_fd = env.get("RSYNCY_STATUS_FD")
        interval = env.get("RSYNCY_STATUS_INTERVAL", str(DEFAULT_STATUS_INTERVAL))

        rsync_args = []
        i = 1
        while i < len(argv):
            a = argv[i]

            if a == "--":
                # everything after the separator goes to rsync verbatim
                rsync_args.extend(argv[i + 1 :])
                break
            elif a == "--status-only":
                status_only = True
            elif a == "--no-progress":
                progress = "off"
            elif a == "--progress":
                i += 1
                if i >= len(argv):
                    raise ConfigError("--progress requires a value")
                progress = argv[i]
            elif a.startswith("--progress="):
                progress = a.split("=", 1)[1]
            elif a == "--status-fd":
                i += 1
                if i >= len(argv):
                    raise ConfigError("--status-fd requires a file descriptor")
                status_fd = argv[i]
            elif a.startswith("--status-fd="):
                status_fd = a.split("=", 1)[1]
            else:
                # first rsync argument: the rest passes through unchanged
                rsync_args.extend(argv[i:])
                break
            i += 1

        if progress not in PROGRESS_MODES:
            raise ConfigError(
                f"invalid --progress value {progress!r} "
                f"(expected one of {', '.join(PROGRESS_MODES)})"
            )

        try:
            status_fd = int(status_fd) if status_fd not in (None, "") else None
        except ValueError:
            raise ConfigError(f"invalid --status-fd value {status_fd!r}")

        try:
            status_interval = float(interval)
        except ValueError:
            raise ConfigError(f"invalid RSYNCY_STATUS_INTERVAL value {interval!r}")
        if status_interval < 0:
            raise ConfigError("RSYNCY_STATUS_INTERVAL must be >= 0")

        return cls(
            invoked_name, status_only, progress, status_fd, status_interval, rsync_args
        )


class TTYInfo:
    """One-shot probe of which standard streams are TTYs."""

    def __init__(self, stdin, stdout, stderr):
        self.stdin = stdin
        self.stdout = stdout
        self.stderr = stderr

    @classmethod
    def probe(cls, stdin=None, stdout=None, stderr=None):
        stdin = sys.stdin if stdin is None else stdin
        stdout = sys.stdout if stdout is None else stdout
        stderr = sys.stderr if stderr is None else stderr

        def is_tty(stream):
            try:
                return bool(stream.isatty())
            except (ValueError, OSError):
                return False

        return cls(is_tty(stdin), is_tty(stdout), is_tty(stderr))


class NullStatusSink:
    """Progress explicitly disabled."""

    co_located = False

    def render(self, status, pc):
        return False

    def finish(self):
        return False

    def clear(self):
        pass

    def attach_style(self, bg, reset):
        pass


class InteractiveStatusSink:
    """In-place rendering on a TTY (carriage returns / clear-line codes)."""

    co_located = False

    def __init__(self, stream):
        self.stream = stream
        self.bg = ""
        self.reset = CLI.style.reset

    def attach_style(self, bg, reset):
        self.bg, self.reset = bg, reset

    def render(self, status, pc):
        self.stream.write(
            "\r"
            + self.bg
            + status
            + CLI.esc.clear_line()
            + "\r"
            + (CLI.esc.right * pc)
            + self.reset
        )
        self.stream.flush()
        return True

    def clear(self):
        # erase the current frame so external output on the same TTY stays clean
        self.stream.write("\r" + CLI.esc.clear_line())
        self.stream.flush()

    def finish(self):
        # leave the cursor on a fresh line
        self.stream.write("\r" + CLI.esc.clear_line() + "\r\n")
        self.stream.flush()
        return False


class LineStatusSink:
    """Non-TTY backend: no control codes, rate limited, line delimited."""

    co_located = False

    def __init__(self, stream, min_interval, clock=time.monotonic):
        self.stream = stream
        self.min_interval = min_interval
        self.clock = clock
        self._last = None
        self._last_text = None
        self._pending = None

    def render(self, status, pc):
        now = self.clock()
        self._pending = status
        if self._last is None or now - self._last >= self.min_interval:
            self._emit(status, now)
            return True
        return False

    def finish(self):
        # flush the last throttled frame so final state is never lost
        if self._pending is not None and self._pending != self._last_text:
            self._emit(self._pending, self.clock())
            return True
        return False

    def _emit(self, status, now):
        self.stream.write(status + "\n")
        self.stream.flush()
        self._last = now
        self._last_text = status
        self._pending = None

    def clear(self):
        pass

    def attach_style(self, bg, reset):
        pass


class EventSink:
    """Machine-consumable JSON-lines events on an independent FD."""

    def __init__(self, stream):
        self.stream = stream

    def emit(self, type, **fields):
        payload = dict(fields)
        payload["type"] = type
        self.stream.write(
            json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
        )
        self.stream.flush()

    def close(self):
        try:
            self.stream.flush()
        except (ValueError, OSError):
            pass


class OutputPlan:
    """Determined once at startup; assigns every output kind an explicit sink.

    Contracts:
      * records sink (stdout): plain, line-delimited rsync output; never carries
        status frames, diagnostics, ANSI SGR or cursor-control codes.
      * diagnostics sink (stderr): human-readable diagnostics/usage, line
        delimited. In normal non-TTY mode it also carries the rate-limited
        plain status backend so stdout stays a clean data pipe.
      * events sink (--status-fd FD, optional): one JSON object per line; the
        FD is independent and receives no other content.
    """

    def __init__(self, config, tty, stdout, stderr, events, clock=time.monotonic):
        self.config = config
        self.tty = tty
        self.stdout = stdout
        self.stderr = stderr
        self.events = events

        self.status_stream = stderr
        self.status_sink = NullStatusSink()
        self._last_snapshot = None
        self._status_finished = False
        self._closed = False

        self._select_sinks(clock)

    @classmethod
    def build(cls, config, tty, stdout, stderr, clock=time.monotonic):
        events = None
        if config.status_fd is not None:
            # do not own/close an inherited FD
            events = EventSink(os.fdopen(config.status_fd, "w", closefd=False))

        plan = cls(config, tty, stdout, stderr, events, clock)

        if events is not None:
            events.emit(
                "start",
                mode="status-only" if config.status_only else "normal",
                progress=config.progress,
                tty={
                    "stdin": tty.stdin,
                    "stdout": tty.stdout,
                    "stderr": tty.stderr,
                },
            )
        return plan

    def _select_sinks(self, clock):
        cfg = self.config
        interval = cfg.status_interval

        if cfg.progress == "off":
            self.status_stream = self.stderr
            self.status_sink = NullStatusSink()
            return

        if cfg.status_only:
            # formal status-only mode: scrolling line status on stdout,
            # rsync records suppressed
            self.status_stream = self.stdout
            self.status_sink = LineStatusSink(self.stdout, interval, clock)
            return

        if cfg.progress == "line":
            self.status_stream = self.stderr
            self.status_sink = LineStatusSink(self.stderr, interval, clock)
            return

        # auto/tty: render in place on the first available TTY
        if self.tty.stdout:
            sink = InteractiveStatusSink(self.stdout)
            sink.co_located = True
            self.status_stream = self.stdout
            self.status_sink = sink
        elif self.tty.stderr:
            self.status_stream = self.stderr
            self.status_sink = InteractiveStatusSink(self.stderr)
        elif cfg.progress == "tty":
            self.stderr.write(
                "rsyncy: warning: no TTY on stdout or stderr; progress disabled\n"
            )
            self.stderr.flush()
            self.status_stream = self.stderr
            self.status_sink = NullStatusSink()
        else:
            # auto, no TTY: plain line backend on stderr, stdout stays clean
            self.status_stream = self.stderr
            self.status_sink = LineStatusSink(self.stderr, interval, clock)

    def attach_style(self, rstyle):
        self.status_sink.attach_style(rstyle["bg"], CLI.style.reset)

    def diagnostic(self, message, level="error"):
        self.stderr.write(f"rsyncy: {level}: {message}\n")
        self.stderr.flush()
        self._event("diagnostic", level=level, message=message)

    def usage(self):
        self.stderr.write(USAGE)
        self.stderr.flush()

    def write_record(self, line):
        # machine consumers still see records even in status-only mode
        self._event("record", line=line)
        if self.config.status_only:
            return
        if self.status_sink.co_located:
            self.status_sink.clear()
        self.stdout.write(line + "\n")
        self.stdout.flush()

    def update_status(self, status, pc, snapshot):
        self._last_snapshot = snapshot
        if self.status_sink.render(status, pc):
            self._event("status", **snapshot)

    def finish_status(self):
        if self._status_finished:
            return
        if self.status_sink.finish() and self._last_snapshot is not None:
            self._event("status", **self._last_snapshot)
        self._status_finished = True

    def close(self, returncode=None):
        if self._closed:
            return
        self.finish_status()
        self._event("end", returncode=returncode)
        if self.events is not None:
            self.events.close()
        self._closed = True

    def _event(self, type, **fields):
        if self.events is not None:
            self.events.emit(type, **fields)


USAGE = """\
rsyncy - an rsync wrapper with a progress bar

Usage:
  rsyncy [rsyncy options] [rsync arguments]
  rsync ... | rsyncy [rsyncy options]

Options:
  --status-only       only emit status output; suppress rsync records
  --no-progress       do not show any progress output
  --progress=MODE     auto (default), tty, line or off
  --status-fd=FD      write machine-consumable JSON-lines events to FD
  --                  pass the remaining arguments to rsync unchanged

Environment:
  NO_COLOR=1                 disable colors
  RSYNCY_STATUS_ONLY=1       same as --status-only
  RSYNCY_PROGRESS=MODE       same as --progress
  RSYNCY_STATUS_FD=FD        same as --status-fd
  RSYNCY_STATUS_INTERVAL=S   min seconds between status lines (default 0.5)

"rsyncy-stat" is kept as a legacy alias for "rsyncy --status-only".
"""


def build_rstyle(color_bits, plain):
    spinner = ["-", "\\", "|", "/"]
    if plain:
        return {
            "bg": "",
            "dim": "",
            "text": "",
            "bar1": "",
            "bar2": "",
            "spin": "",
            "spinner": spinner,
        }
    if color_bits >= 8:
        return {
            "bg": CLI.bg8(238),
            "dim": CLI.fg8(241),
            "text": CLI.fg8(250),
            "bar1": CLI.fg8(243),
            "bar2": CLI.fg8(43),
            "spin": CLI.fg8(228) + CLI.style.bold,
            "spinner": spinner,
        }
    else:
        return {
            "bg": CLI.bg4(7),
            "dim": CLI.fg4(8),
            "text": CLI.fg4(0),
            "bar1": CLI.fg4(8),
            "bar2": CLI.fg4(0),
            "spin": CLI.fg4(14) + CLI.style.bold,
            "spinner": spinner,
        }


class Rsyncy:
    def __init__(self, rstyle, plan):
        self.bg = rstyle["bg"]
        self.cdim = rstyle["dim"]
        self.ctext = rstyle["text"]
        self.cbar1 = rstyle["bar1"]
        self.cbar2 = rstyle["bar2"]
        self.cspin = rstyle["spin"]
        self.spinner = rstyle["spinner"]
        self.plan = plan
        self.trans = 0
        self.percent = 0
        self.speed = ""
        self.xfr = ""
        self.chk = ""
        self.chk_finished = False
        self.start = datetime.now()

    def parse_stat(self, line):
        # sample: 6,672,528  96%    1.04MB/s    0:00:06 (xfr#1, to-chk=7/12)
        data = [s for s in line.split(" ") if s]
        if len(data) >= 4:
            self.trans, percent, self.speed, timing, *_ = data
            try:
                self.percent = int(percent.strip("%")) / 100
            except Exception as e:
                self.plan.diagnostic(f"can't parse#1: {line!r} {data!r}: {e}")

        # timing is remaining with 4 args, or elapsed with 6
        if len(data) == 6:
            try:
                xfr, chk = data[4:6]
                self.xfr = xfr.strip(",").split("#")[1]
                if self.xfr:
                    self.xfr = "#" + self.xfr

                m = _re_chk.match(chk)
                if m:
                    self.chk_finished = m[1] == "to"
                    todo = int(m[2])
                    total = int(m[3])
                    done = total - todo
                    self.chk = f"{(done/total if total else 0):2.0%} ({total})"
                else:
                    self.chk = ""

            except Exception as e:
                self.plan.diagnostic(f"can't parse#2: {line!r} {data!r}: {e}")

    def draw_stat(self):
        cols = CLI.get_size(self.plan.status_stream).columns
        elapsed = datetime.now() - self.start
        if self.chk_finished:
            spin = ""
        else:
            spin = self.spinner[round(elapsed.total_seconds()) % len(self.spinner)]

        # define status (excl. bar)
        # use \xff as a placeholder for the spinner
        parts = [
            o
            for o in [
                f"{self.trans:>11}",
                f"{self.speed:>14}",
                f"{str(elapsed).split('.')[0]}",
                f"{self.xfr}",
                f"scan {self.chk}\xff",
            ]
            if o
        ]

        # reduce to fit
        plen = lambda: sum(len(s) for s in parts) + len(parts)
        while parts and plen() > cols:
            parts.pop(0)

        # add bar in remaining space
        pc = 0
        rcols = cols - plen()
        if rcols > 12:
            pc_width = min(rcols - 7, 30)
            pc = round(self.percent * pc_width)
            parts.insert(
                0,
                f"{self.cbar1}[{self.cbar2}{'#' * pc}{self.cbar1}{':' * (pc_width-pc)}]{self.ctext}"
                + f"{self.percent:>5.0%}",
            )
            rcols -= pc_width + 7
        elif rcols > 5:
            parts.insert(0, f"{self.percent:>5.0%}")
            rcols -= 5

        # get delimiter size
        delim = f"{self.cdim}|{self.ctext}"
        if rcols > (len(parts) - 1) * 2:
            delim = " " + delim + " "

        # render with delimiter
        status = delim.join(parts).replace("\xff", f"{self.cspin}{spin}{self.ctext}")

        snapshot = {
            "percent": self.percent,
            "transferred": str(self.trans),
            "speed": self.speed,
            "elapsed": str(elapsed).split(".")[0],
            "xfr": self.xfr.lstrip("#") or None,
            "scan": self.chk or None,
            "scan_finished": self.chk_finished,
        }
        self.plan.update_status(status, pc, snapshot)

    def parse_line(self, line, is_stat):
        line = line.decode().strip(" ")
        if not line:
            return

        is_stat = is_stat or line[0] == "\r"
        line = line.replace("\r", "")

        if is_stat:
            self.parse_stat(line)
            self.draw_stat()
        elif line[-1] == "/":
            # skip directories
            pass
        else:
            self.plan.write_record(line)
            if self.plan.status_sink.co_located:
                # the frame was erased for the record; redraw it
                self.draw_stat()

    def read(self, fd):
        line = b""
        while True:
            stat = select.select([fd], [], [], 0.2)
            if fd in stat[0]:
                ch = os.read(fd, 1)
                if ch == b"":
                    # exit
                    self.parse_line(line, False)
                    break

                elif ch == b"\r":
                    self.parse_line(line, True)
                    line = b"\r"
                elif ch == b"\n":
                    self.parse_line(line, False)
                    line = b""
                else:
                    line += ch

            else:
                # no new input
                if line:
                    # assume this is a status update
                    self.parse_line(line, True)
                    line = b""
                else:
                    # waiting for input
                    self.draw_stat()
                    time.sleep(0.5)

        self.plan.finish_status()


def run_rsync(args, write_pipe, rc):
    # prefix rsync and add args required for progress
    args = ["rsync"] + args + ["--info=progress2", "--no-v", "-hv"]
    p = None
    try:
        p = subprocess.Popen(args, stdout=write_pipe)
        p.wait()
    finally:
        os.close(write_pipe)
        rc.put(p.returncode if p else 1)


def main(argv=None):
    argv = list(sys.argv if argv is None else argv)

    try:
        config = RsyncyConfig.parse(argv)
    except ConfigError as e:
        sys.stderr.write(f"rsyncy: error: {e}\n")
        return 2

    tty = TTYInfo.probe(sys.stdin, sys.stdout, sys.stderr)
    plan = OutputPlan.build(config, tty, sys.stdout, sys.stderr)

    plain = bool(CLI.NO_COLOR) or not isinstance(
        plan.status_sink, InteractiveStatusSink
    )
    rstyle = build_rstyle(CLI.get_col_bits(), plain)
    plan.attach_style(rstyle)
    rsyncy = Rsyncy(rstyle, plan)

    try:
        if not config.rsync_args:
            if tty.stdin:
                plan.usage()
                plan.close(None)
                return 1
            else:
                # receive pipe from rsync
                rsyncy.read(sys.stdin.fileno())
                plan.close(None)
        else:
            read_pipe, write_pipe = os.pipe()
            rc = queue.Queue()
            t = Thread(target=run_rsync, args=(config.rsync_args, write_pipe, rc))
            t.start()
            rsyncy.read(read_pipe)
            t.join()

            code = rc.get()
            plan.close(code)
            return code

    except KeyboardInterrupt:
        plan.close(1)
        return 1


if __name__ == "__main__":
    sys.exit(main())
