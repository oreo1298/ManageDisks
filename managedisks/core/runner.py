"""Running external programs with logging, progress parsing and cancellation."""

from __future__ import annotations

import os
import re
import select
import shlex
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Callable, Sequence

# Environment for every child: stable, parseable (English) output.
_ENV = dict(os.environ)
_ENV.update({"LC_ALL": "C", "LANG": "C", "LANGUAGE": "C"})
for _k in ("PYTHONPATH", "PYTHONHOME"):
    _ENV.pop(_k, None)

# Make sure sbin directories are searched even for non-root users.
_extra = ["/usr/local/sbin", "/usr/sbin", "/sbin"]
_ENV["PATH"] = ":".join([p for p in _ENV.get("PATH", "/usr/bin:/bin").split(":") if p]
                        + [p for p in _extra if p not in _ENV.get("PATH", "")])


class CommandError(RuntimeError):
    def __init__(self, argv: Sequence[str], returncode: int, output: str):
        self.argv = list(argv)
        self.returncode = returncode
        self.output = output
        tail = "\n".join(output.strip().splitlines()[-8:])
        super().__init__(f"{shlex.join(self.argv)} failed with exit code {returncode}"
                         + (f":\n{tail}" if tail else ""))


class Cancelled(RuntimeError):
    pass


@dataclass
class Result:
    argv: list[str]
    returncode: int
    output: str


def which(tool: str) -> str | None:
    return shutil.which(tool, path=_ENV["PATH"])


def have(tool: str) -> bool:
    return which(tool) is not None


def run_simple(argv: Sequence[str], *, input: str | None = None, timeout: float | None = 60,
               check: bool = True, stderr: bool = True) -> Result:
    """Run a short command synchronously and return its combined output."""
    try:
        proc = subprocess.run(list(argv), input=input, capture_output=True, text=True,
                              env=_ENV, timeout=timeout, errors="replace")
    except FileNotFoundError:
        raise CommandError(argv, 127, f"{argv[0]}: command not found") from None
    out = proc.stdout + (proc.stderr if stderr else "")
    if check and proc.returncode != 0:
        raise CommandError(argv, proc.returncode, proc.stdout + proc.stderr)
    return Result(list(argv), proc.returncode, out if stderr else proc.stdout)


def output(argv: Sequence[str], *, timeout: float | None = 60) -> str:
    """stdout of a command that must succeed."""
    try:
        proc = subprocess.run(list(argv), capture_output=True, text=True, env=_ENV,
                              timeout=timeout, errors="replace")
    except FileNotFoundError:
        raise CommandError(argv, 127, f"{argv[0]}: command not found") from None
    if proc.returncode != 0:
        raise CommandError(argv, proc.returncode, proc.stdout + proc.stderr)
    return proc.stdout


ProgressParser = Callable[[str], "float | None"]
LineCallback = Callable[[str], None]

PERCENT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%")


def percent_parser(line: str) -> float | None:
    m = None
    for m in PERCENT_RE.finditer(line):
        pass
    if m:
        try:
            return max(0.0, min(1.0, float(m.group(1)) / 100.0))
        except ValueError:
            return None
    return None


class Runner:
    """Runs commands for long operations, streaming output line by line.

    ``on_line`` receives every output line; ``on_progress`` receives fractions
    in [0, 1] parsed by a per-command parser. ``cancel`` is a threading.Event
    checked while the command runs.
    """

    def __init__(self, on_line: LineCallback | None = None,
                 on_progress: Callable[[float | None], None] | None = None,
                 cancel: threading.Event | None = None, dry_run: bool = False):
        self.on_line = on_line or (lambda s: None)
        self.on_progress = on_progress or (lambda f: None)
        self.cancel = cancel or threading.Event()
        self.dry_run = dry_run
        self.current: subprocess.Popen | None = None

    def log(self, text: str) -> None:
        for line in str(text).splitlines() or [""]:
            self.on_line(line)

    def check_cancel(self) -> None:
        if self.cancel.is_set():
            raise Cancelled("Operation cancelled")

    def run(self, argv: Sequence[str], *, input: str | None = None, check: bool = True,
            ok_codes: Sequence[int] = (0,), progress: ProgressParser | None = None,
            pty: bool = False, secret_input: bool = False, quiet: bool = False,
            cancellable: bool = False, mutating: bool = True) -> Result:
        """Run a command. Cancellation is honoured *between* commands; a
        running tool is only killed when ``cancellable`` is True, because
        interrupting e.g. resize2fs or btrfstune can corrupt a filesystem."""
        argv = [str(a) for a in argv]
        if not quiet:
            self.on_line("$ " + shlex.join(argv) + ("  < (hidden input)" if secret_input and input else ""))
        if self.dry_run and mutating:
            return Result(argv, 0, "")
        self.check_cancel()
        if pty:
            res = self._run_pty(argv, input, progress, cancellable)
        else:
            res = self._run_pipe(argv, input, progress, cancellable)
        if check and res.returncode not in ok_codes:
            raise CommandError(argv, res.returncode, res.output)
        return res

    # Output is read in raw chunks so that progress updates that end with '\r'
    # (no newline) are seen immediately.
    def _consume(self, fd: int, proc: subprocess.Popen, progress: ProgressParser | None,
                 cancellable: bool) -> str:
        chunks: list[str] = []
        pending = ""
        while True:
            if cancellable and self.cancel.is_set():
                self._terminate(proc)
                raise Cancelled("Operation cancelled")
            try:
                r, _, _ = select.select([fd], [], [], 0.2)
            except InterruptedError:
                continue
            if not r:
                if proc.poll() is not None:
                    # Exited and nothing left to read. Do not block on the pipe:
                    # a daemonised grandchild may still hold it open.
                    break
                continue
            else:
                try:
                    data = os.read(fd, 65536)
                except OSError:  # EIO on pty when the child exits
                    data = b""
                if not data:
                    break
            text = data.decode("utf-8", "replace")
            chunks.append(text)
            pending += text
            parts = re.split(r"[\r\n]", pending)
            pending = parts.pop()
            for line in parts:
                self._handle_line(line, progress)
        if pending:
            self._handle_line(pending, progress)
        return "".join(chunks)

    def _handle_line(self, line: str, progress: ProgressParser | None) -> None:
        stripped = line.strip()
        if progress:
            frac = progress(stripped)
            if frac is not None:
                self.on_progress(frac)
                return  # progress lines are not logged (they would flood the log)
        if stripped:
            self.on_line(stripped)

    def _run_pipe(self, argv, input, progress, cancellable) -> Result:
        try:
            proc = subprocess.Popen(argv, stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=_ENV,
                                    start_new_session=True)
        except FileNotFoundError:
            raise CommandError(argv, 127, f"{argv[0]}: command not found") from None
        self.current = proc
        try:
            if input is not None:
                assert proc.stdin
                try:
                    proc.stdin.write(input.encode())
                    proc.stdin.close()
                except BrokenPipeError:
                    pass
            assert proc.stdout
            out = self._consume(proc.stdout.fileno(), proc, progress, cancellable)
            proc.wait()
        finally:
            self.current = None
        return Result(argv, proc.returncode, out)

    def _run_pty(self, argv, input, progress, cancellable) -> Result:
        import pty as _pty
        master, slave = _pty.openpty()
        try:
            proc = subprocess.Popen(argv, stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
                                    stdout=slave, stderr=slave, env=_ENV, start_new_session=True)
        except FileNotFoundError:
            os.close(master)
            os.close(slave)
            raise CommandError(argv, 127, f"{argv[0]}: command not found") from None
        os.close(slave)
        self.current = proc
        try:
            if input is not None:
                assert proc.stdin
                try:
                    proc.stdin.write(input.encode())
                    proc.stdin.close()
                except BrokenPipeError:
                    pass
            out = self._consume(master, proc, progress, cancellable)
            proc.wait()
        finally:
            os.close(master)
            self.current = None
        return Result(argv, proc.returncode, out)

    @staticmethod
    def _terminate(proc: subprocess.Popen) -> None:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            return
        for _ in range(30):
            if proc.poll() is not None:
                return
            time.sleep(0.1)
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
