"""Talks to the privileged helper (see managedisks.helper) over a QProcess."""

from __future__ import annotations

import json
import os
import shutil
import sys
from typing import Callable

from .qt import QtCore, Signal

import managedisks


def helper_command() -> tuple[str, list[str]]:
    """Program and arguments that start the helper as root."""
    override = os.environ.get("MANAGEDISKS_HELPER")
    pkg_dir = os.path.dirname(os.path.abspath(managedisks.__file__))
    installed = None
    # Installed layout: <prefix>/share/managedisks/managedisks -> <prefix>/lib/managedisks/managedisks-helper
    share = os.path.dirname(pkg_dir)
    prefix = os.path.dirname(os.path.dirname(share))
    for cand in (os.path.join(prefix, "lib", "managedisks", "managedisks-helper"),
                 os.path.join(prefix, "libexec", "managedisks", "managedisks-helper"),
                 "/usr/lib/managedisks/managedisks-helper", "/usr/libexec/managedisks/managedisks-helper",
                 "/usr/local/lib/managedisks/managedisks-helper"):
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            installed = cand
            break
    if override:
        target = [override]
    elif installed:
        target = [installed]
    else:
        # Running from a source checkout or a pip install.
        target = [sys.executable, "-I", os.path.join(pkg_dir, "helper", "entry.py")]
    if os.geteuid() == 0:
        return target[0], target[1:]
    pkexec = shutil.which("pkexec")
    if pkexec:
        return pkexec, target
    raise RuntimeError("pkexec (polkit) is not installed, so ManageDisks cannot gain administrator "
                       "rights. Install polkit, or run the command line tool with sudo.")


class HelperClient(QtCore.QObject):
    ready = Signal()
    failed = Signal(str)        # could not start / authentication refused
    stopped = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.proc: QtCore.QProcess | None = None
        self.is_ready = False
        self._buf = b""
        self._next = 1
        self._calls: dict[int, tuple] = {}
        self._queue: list[dict] = []
        self._stderr = b""

    @property
    def running(self) -> bool:
        return self.proc is not None

    def start(self) -> None:
        if self.proc is not None:
            return
        try:
            program, args = helper_command()
        except RuntimeError as e:
            text = str(e)
            QtCore.QTimer.singleShot(0, lambda: (self._fail_all(text), self.failed.emit(text)))
            return
        self.proc = QtCore.QProcess(self)
        self.proc.setProgram(program)
        self.proc.setArguments(args)
        self.proc.readyReadStandardOutput.connect(self._on_stdout)
        self.proc.readyReadStandardError.connect(self._on_stderr)
        self.proc.finished.connect(self._on_finished)
        self.proc.errorOccurred.connect(self._on_error)
        self._stderr = b""
        self.proc.start()

    def stop(self) -> None:
        if self.proc is None:
            return
        try:
            self.proc.closeWriteChannel()
            if not self.proc.waitForFinished(1500):
                self.proc.terminate()
        except RuntimeError:
            pass

    def call(self, method: str, params: dict | None = None, *,
             on_result: Callable | None = None, on_error: Callable | None = None,
             on_event: Callable | None = None) -> int:
        rid = self._next
        self._next += 1
        self._calls[rid] = (on_result, on_error, on_event)
        msg = {"id": rid, "method": method, "params": params or {}}
        if self.is_ready:
            self._send(msg)
        else:
            self._queue.append(msg)
            self.start()
        return rid

    def cancel(self, rid: int) -> None:
        if self.is_ready:
            self._send({"id": self._next, "method": "cancel", "params": {"target": rid}})
            self._next += 1

    def _send(self, msg: dict) -> None:
        assert self.proc is not None
        self.proc.write((json.dumps(msg) + "\n").encode())

    # -- process events -------------------------------------------------------
    def _on_stdout(self):
        assert self.proc is not None
        self._buf += bytes(self.proc.readAllStandardOutput().data())
        while b"\n" in self._buf:
            line, self._buf = self._buf.split(b"\n", 1)
            if not line.strip():
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            self._dispatch(msg)

    def _on_stderr(self):
        assert self.proc is not None
        self._stderr += bytes(self.proc.readAllStandardError().data())
        self._stderr = self._stderr[-8000:]

    def _dispatch(self, msg: dict):
        if "id" not in msg:
            ev = msg.get("event", {})
            if ev.get("type") == "ready":
                self.is_ready = True
                queued, self._queue = self._queue, []
                for m in queued:
                    self._send(m)
                self.ready.emit()
            return
        rid = msg["id"]
        cbs = self._calls.get(rid)
        if not cbs:
            return
        on_result, on_error, on_event = cbs
        if "event" in msg:
            if on_event:
                on_event(msg["event"])
            return
        self._calls.pop(rid, None)
        if "error" in msg:
            if on_error:
                on_error(msg["error"])
        elif on_result:
            on_result(msg.get("result"))

    def _fail_all(self, text: str):
        calls, self._calls = self._calls, {}
        self._queue = []
        for on_result, on_error, on_event in calls.values():
            if on_error:
                on_error(text)

    def _on_error(self, err):
        if err == QtCore.QProcess.ProcessError.FailedToStart:
            self.proc = None
            self.is_ready = False
            text = "Could not start the administrator helper (pkexec)."
            self._fail_all(text)
            self.failed.emit(text)

    def _on_finished(self, code, status=None):
        was_ready = self.is_ready
        self.is_ready = False
        stderr = self._stderr.decode("utf-8", "replace").strip()
        self.proc = None
        if not was_ready:
            if code in (126, 127):
                text = "Authentication was cancelled or refused."
                if "No authentication agent" in stderr or "polkit-agent" in stderr.lower():
                    text = ("No polkit authentication agent is running, so the password prompt cannot "
                            "be shown. Start your desktop's polkit agent (e.g. polkit-kde-agent, "
                            "polkit-gnome or lxqt-policykit).")
            else:
                text = f"The administrator helper exited (code {code}). {stderr[-400:]}"
            self._fail_all(text)
            self.failed.emit(text)
        else:
            self._fail_all("The administrator helper stopped unexpectedly. " + stderr[-400:])
            self.stopped.emit()
