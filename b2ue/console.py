# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Abdulkerim Tabakoğlu
"""Readable console output: one line per pipeline step, colours when the
terminal supports them, and Blender's own log noise moved to a log file."""

import contextlib
import os
import sys
import tempfile
import time

# ---------------------------------------------------------------- colours

def _enable_windows_ansi():
    if os.name != "nt":
        return True
    try:
        import ctypes
        kernel = ctypes.windll.kernel32
        handle = kernel.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint32()
        if not kernel.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return bool(kernel.SetConsoleMode(handle, mode.value | 0x0004))
    except Exception:
        return False


def _use_colour():
    if os.environ.get("NO_COLOR") or not hasattr(sys.stdout, "isatty"):
        return False
    return sys.stdout.isatty() and _enable_windows_ansi()


class Style:
    def __init__(self, enabled):
        self.enabled = enabled

    def _wrap(self, code, text):
        return f"\033[{code}m{text}\033[0m" if self.enabled else text

    def bold(self, t): return self._wrap("1", t)
    def dim(self, t): return self._wrap("2", t)
    def green(self, t): return self._wrap("32", t)
    def yellow(self, t): return self._wrap("33", t)
    def red(self, t): return self._wrap("31", t)
    def cyan(self, t): return self._wrap("36", t)


# ---------------------------------------------------------------- quiet

@contextlib.contextmanager
def redirected_output(log_path):
    """Send everything written to stdout/stderr, including Blender's C-level
    output, to log_path while the block runs.

    Two things are redirected: file descriptors 1 and 2 (for C code), and
    Python's sys.stdout / sys.stderr objects (for print()). The second matters
    on Windows, where the console stream writes with WriteConsoleW and fails
    with "The handle is invalid" once fd 1 points at a file.
    """
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception:
        pass
    try:
        saved = os.dup(1), os.dup(2)
        log = open(log_path, "ab")
    except (OSError, ValueError, AttributeError):
        saved = log = None
    text_log = open(log_path, "a", encoding="utf-8", errors="replace", buffering=1)
    old_out, old_err = sys.stdout, sys.stderr
    try:
        if log is not None:
            os.dup2(log.fileno(), 1)
            os.dup2(log.fileno(), 2)
        sys.stdout = sys.stderr = text_log
        yield
    finally:
        sys.stdout, sys.stderr = old_out, old_err
        text_log.close()
        if log is not None:
            os.dup2(saved[0], 1)
            os.dup2(saved[1], 2)
            os.close(saved[0])
            os.close(saved[1])
            log.close()


# ---------------------------------------------------------------- reporters

class Step:
    """Collects the outcome of one pipeline step."""

    def __init__(self, name):
        self.name = name
        self.status = "ok"      # ok | warn | skip | fail
        self.detail = ""
        self.lines = []         # extra lines printed under the step
        self.seconds = 0.0

    def warn(self, detail=None):
        self.status = "warn"
        if detail is not None:
            self.detail = detail

    def skip(self, detail):
        self.status = "skip"
        self.detail = detail


class Reporter:
    """Silent reporter for library use and tests. Steps still time and
    capture failures, nothing is printed."""

    def start_file(self, index, total, path):
        pass

    def end_file(self, result):
        pass

    @contextlib.contextmanager
    def step(self, name):
        st = Step(name)
        t = time.perf_counter()
        try:
            yield st
        except Exception as exc:
            st.status, st.detail = "fail", str(exc) or type(exc).__name__
            raise
        finally:
            st.seconds = time.perf_counter() - t
            self.record(st)

    def record(self, st):
        self.last = st


class ConsoleReporter(Reporter):
    TAGS = {"ok": ("OK  ", "green"), "warn": ("WARN", "yellow"),
            "skip": ("SKIP", "dim"), "fail": ("FAIL", "red")}

    def __init__(self, verbose=False, stream=None):
        self.verbose = verbose
        self.out = stream or sys.stdout
        try:
            self.out.reconfigure(errors="replace")
        except Exception:
            pass
        self.s = Style(_use_colour())
        self.live = getattr(self.out, "isatty", lambda: False)()
        self.log_path = os.path.join(tempfile.gettempdir(), f"b2ue-{os.getpid()}.log")

    def print(self, text=""):
        try:
            self.out.write(text + "\n")
            self.out.flush()
        except (OSError, ValueError):
            pass  # a broken console must not stop the export

    def header(self, version, files, out_dir, opts_text, preset):
        s = self.s
        self.print()
        self.print(s.bold(f"b2ue {version}") + s.dim("  Blender -> Unreal asset pipeline"))
        target = f"  ->  {out_dir}" if out_dir else "  (validate only)"
        count = f"{files} file" + ("s" if files != 1 else "")
        self.print(s.dim(f"{count}{target}"))
        label = f"preset {preset}: " if preset else ""
        self.print(s.dim(f"{label}{opts_text}"))

    def start_file(self, index, total, path):
        self.print()
        self.print(self.s.bold(f"[{index}/{total}] {os.path.basename(str(path))}"))

    @contextlib.contextmanager
    def step(self, name):
        if self.live:
            try:
                self.out.write(self.s.dim(f"  ..    {name:<10}") + " ")
                self.out.flush()
            except (OSError, ValueError):
                pass
        quiet = contextlib.nullcontext() if self.verbose else redirected_output(self.log_path)
        with super().step(name) as st:
            with quiet:
                yield st

    def record(self, st):
        super().record(st)
        tag, colour = self.TAGS[st.status]
        tag = getattr(self.s, colour)(tag)
        secs = self.s.dim(f"{st.seconds:5.1f}s")
        line = f"  {tag}  {st.name:<10} {secs}  {st.detail}"
        if self.live:
            try:
                self.out.write("\r\033[K" if self.s.enabled else "\r")
            except (OSError, ValueError):
                pass
        self.print(line)
        for extra in st.lines:
            self.print(f"              {extra}")

    def issue_line(self, issue):
        colour = {"error": "red", "warning": "yellow", "info": "dim"}[issue["level"]]
        code = getattr(self.s, colour)(f"{issue['code']:<20}")
        return f"{code} {issue['object']}: {issue['message']}"

    def log_tail(self, lines=12):
        try:
            with open(self.log_path, "r", errors="replace") as f:
                tail = f.read().splitlines()[-lines:]
        except OSError:
            return
        if tail:
            self.print(self.s.dim("  last lines of the Blender log:"))
            for line in tail:
                self.print(self.s.dim(f"    {line}"))

    def summary(self, results, seconds):
        s = self.s
        exported = sum(len(r.get("exported", [])) for r in results)
        failed = [r for r in results if r.get("error")]
        warnings = sum(1 for r in results for i in r.get("issues", []) if i["level"] == "warning")
        errors = sum(1 for r in results for i in r.get("issues", []) if i["level"] == "error")
        self.print()
        self.print(s.bold("Summary"))
        for r in results:
            name = os.path.basename(r["file"])
            if r.get("error"):
                self.print(f"  {s.red('FAIL')}  {name}: {r['error']}")
                continue
            outs = ", ".join(f"{os.path.basename(e['path'])} ({e['triangles']:,} tris)"
                             for e in r.get("exported", [])) or "nothing exported"
            mark = s.yellow("WARN") if any(i["level"] != "info" for i in r["issues"]) else s.green("OK  ")
            self.print(f"  {mark}  {name}  ->  {outs}")
        parts = [f"{exported} exported"]
        if warnings:
            parts.append(s.yellow(f"{warnings} warning" + ("s" if warnings != 1 else "")))
        if errors:
            parts.append(s.red(f"{errors} error" + ("s" if errors != 1 else "")))
        if failed:
            parts.append(s.red(f"{len(failed)} failed"))
        self.print()
        self.print(f"Done in {seconds:.1f}s: " + ", ".join(parts))
        if failed and not self.verbose:
            self.print(s.dim(f"Full Blender log: {self.log_path}"))


class JsonReporter(ConsoleReporter):
    """Machine-readable progress for the Blender add-on: one line per event,
    'B2UE ' followed by a JSON object."""

    PREFIX = "B2UE "

    def __init__(self, verbose=False, stream=None):
        super().__init__(verbose=verbose, stream=stream)
        self.live = False
        self.s = Style(False)

    def emit(self, event, **data):
        import json
        try:
            self.out.write(self.PREFIX + json.dumps({"event": event, **data}) + "\n")
            self.out.flush()
        except (OSError, ValueError):
            pass

    def print(self, text=""):
        self.emit("message", text=text)

    def header(self, version, files, out_dir, opts_text, preset):
        self.emit("start", version=version, files=files, out=str(out_dir) if out_dir else None,
                  options=opts_text)

    def start_file(self, index, total, path):
        self.emit("file", index=index, total=total, name=os.path.basename(str(path)))

    @contextlib.contextmanager
    def step(self, name):
        self.emit("begin", step=name)
        with super().step(name) as st:
            yield st

    def record(self, st):
        Reporter.record(self, st)
        self.emit("step", step=st.name, status=st.status, detail=st.detail,
                  seconds=round(st.seconds, 2), lines=st.lines)

    def log_tail(self, lines=12):
        try:
            with open(self.log_path, "r", errors="replace") as f:
                tail = f.read().splitlines()[-lines:]
        except OSError:
            tail = []
        self.emit("log", lines=tail, path=self.log_path)

    def summary(self, results, seconds):
        self.emit("done", seconds=round(seconds, 1), results=results, log=self.log_path)
