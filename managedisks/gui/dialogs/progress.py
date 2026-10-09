"""Progress window for applying operations and running long tasks."""

from __future__ import annotations

import html
import time

from ...core.units import format_size
from .. import icons
from ..qt import QtCore, QtGui, QtWidgets

Qt = QtCore.Qt


class ProgressDialog(QtWidgets.QDialog):
    """Shows per-operation status, overall progress and a detailed log.

    ``start(on_event, on_result, on_error)`` must start the work and return a
    request id usable by ``cancel(rid)``.
    """

    def __init__(self, parent, title: str, items: list[str], start, cancel, cancellable: bool = True):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self._start = start
        self._cancel = cancel
        self.items = items
        self.count = len(items)
        self.current = -1
        self.completed = 0
        self.finished_ok: bool | None = None
        self.rid = None
        self.started = time.monotonic()
        self.step_started = time.monotonic()

        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(18, 16, 18, 14)
        lay.setSpacing(10)
        self.heading = QtWidgets.QLabel(title)
        f = self.heading.font()
        f.setPointSizeF(f.pointSizeF() * 1.25)
        f.setWeight(QtGui.QFont.Weight.DemiBold)
        self.heading.setFont(f)
        lay.addWidget(self.heading)
        self.list = QtWidgets.QListWidget()
        self.list.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.NoSelection)
        self.list.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        for text in items:
            it = QtWidgets.QListWidgetItem(icons.icon("info", prefer_builtin=True), text)
            it.setForeground(self.palette().color(QtGui.QPalette.ColorRole.PlaceholderText)
                             if hasattr(QtGui.QPalette.ColorRole, "PlaceholderText") else
                             self.palette().color(QtGui.QPalette.ColorRole.Text))
            self.list.addItem(it)
        self.list.setMaximumHeight(min(220, 26 * max(2, len(items)) + 8))
        lay.addWidget(self.list)
        self.step = QtWidgets.QLabel("Waiting for authorization…")
        self.step.setWordWrap(True)
        lay.addWidget(self.step)
        self.bar = QtWidgets.QProgressBar()
        self.bar.setRange(0, 0)
        self.bar.setTextVisible(False)
        lay.addWidget(self.bar)
        self.overall = QtWidgets.QProgressBar()
        self.overall.setRange(0, 1000)
        self.overall.setFormat("Overall %p%")
        if self.count > 1:
            lay.addWidget(self.overall)
        self.rate = QtWidgets.QLabel()
        lay.addWidget(self.rate)
        self.note = QtWidgets.QLabel("Do not unplug the disks or power off the computer until this finishes.")
        self.note.setWordWrap(True)
        lay.addWidget(self.note)
        self.details_btn = QtWidgets.QToolButton()
        self.details_btn.setText("Details")
        self.details_btn.setCheckable(True)
        self.details_btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.details_btn.setArrowType(Qt.ArrowType.RightArrow)
        self.details_btn.setAutoRaise(True)
        lay.addWidget(self.details_btn)
        self.log = QtWidgets.QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setFont(QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.SystemFont.FixedFont))
        self.log.setMaximumBlockCount(20000)
        self.log.setVisible(False)
        self.log.setMinimumHeight(200)
        lay.addWidget(self.log, 1)
        self.details_btn.toggled.connect(self._toggle_details)
        bb = QtWidgets.QHBoxLayout()
        self.save_btn = QtWidgets.QPushButton("Save Log…")
        self.save_btn.clicked.connect(self._save)
        self.cancel_btn = QtWidgets.QPushButton("Cancel")
        self.cancel_btn.setEnabled(cancellable)
        self.cancel_btn.clicked.connect(self._do_cancel)
        self.close_btn = QtWidgets.QPushButton("Close")
        self.close_btn.setEnabled(False)
        self.close_btn.clicked.connect(self.accept)
        bb.addWidget(self.save_btn)
        bb.addStretch(1)
        bb.addWidget(self.cancel_btn)
        bb.addWidget(self.close_btn)
        lay.addLayout(bb)
        self.resize(620, 360)
        QtCore.QTimer.singleShot(0, self._begin)

    # -- lifecycle -------------------------------------------------------------
    def _begin(self):
        self.rid = self._start(self.on_event, self._on_result, self._on_error)

    def _toggle_details(self, on):
        self.log.setVisible(on)
        self.details_btn.setArrowType(Qt.ArrowType.DownArrow if on else Qt.ArrowType.RightArrow)
        self.resize(self.width(), 640 if on else 360)

    def _do_cancel(self):
        if self.rid is not None:
            self._cancel(self.rid)
        self.cancel_btn.setEnabled(False)
        self.step.setText("Cancelling… (the current step finishes first if it cannot be interrupted safely)")

    def closeEvent(self, e):
        if self.finished_ok is None:
            e.ignore()
            return
        super().closeEvent(e)

    def reject(self):
        if self.finished_ok is None:
            return  # Esc must not hide a running job
        super().reject()

    def _save(self):
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Save log", "managedisks.log", "Log files (*.log *.txt)")
        if path:
            with open(path, "w", encoding="utf-8") as f:
                f.write(self.log.toPlainText())

    # -- events ---------------------------------------------------------------
    def _set_item(self, idx, state, extra=""):
        if not 0 <= idx < self.list.count():
            return
        it = self.list.item(idx)
        name = {"running": "refresh", "done": "ok", "failed": "error"}.get(state, "info")
        it.setIcon(icons.icon(name, prefer_builtin=True))
        it.setForeground(self.palette().color(QtGui.QPalette.ColorRole.Text))
        if state == "failed":
            it.setForeground(QtGui.QColor("#e01b24"))
        if extra:
            it.setToolTip(extra)
        if state == "running":
            self.list.scrollToItem(it)

    def _update_overall(self, frac):
        if self.count:
            v = (self.completed + (frac or 0.0)) / self.count
            self.overall.setValue(int(v * 1000))

    def on_event(self, ev: dict):
        t = ev.get("type")
        if t == "op_start":
            self.current = ev["index"]
            self.step_started = time.monotonic()
            self._set_item(self.current, "running")
            self.step.setText(ev.get("text", ""))
            self.bar.setRange(0, 0)
            self._update_overall(0)
        elif t == "step":
            self.step.setText(ev["text"])
            if self.current < 0 and self.count == 1:
                self.current = 0
                self._set_item(0, "running")
        elif t == "progress":
            frac = ev.get("fraction")
            if frac is None:
                self.bar.setRange(0, 0)
                self.rate.clear()
            else:
                self.bar.setRange(0, 1000)
                self.bar.setValue(int(frac * 1000))
                self._update_overall(frac)
                rate = ev.get("rate")
                txt = ""
                if rate:
                    txt = f"{format_size(rate)}/s"
                    if 0 < frac < 1:
                        elapsed = time.monotonic() - self.step_started
                        remaining = elapsed * (1 - frac) / max(frac, 1e-6)
                        txt += f" · about {_duration(remaining)} left"
                if ev.get("bad"):
                    txt += f" · {ev['bad']} bad region(s)"
                self.rate.setText(txt)
        elif t == "log":
            self.log.appendPlainText(ev["line"])
        elif t == "op_done":
            self.completed = ev["index"] + 1
            self._set_item(ev["index"], "done")
            self._update_overall(0)
        elif t == "op_failed":
            self._set_item(ev["index"], "failed", ev.get("error", ""))
            self.log.appendPlainText("ERROR: " + ev.get("error", ""))
        elif t == "done":
            self._finish(bool(ev.get("ok")), ev.get("error"))

    def _on_result(self, result):
        if self.finished_ok is None:
            ok = True
            if isinstance(result, dict) and "ok" in result:
                ok = bool(result["ok"])
            if ok and self.count == 1 and self.current <= 0:
                self.completed = 1
                self._set_item(0, "done")
            self._finish(ok, None if ok else "Failed")

    def _on_error(self, err):
        if self.current < 0 and self.count:
            self._set_item(0, "failed", str(err))
        self.log.appendPlainText("ERROR: " + str(err))
        self._finish(False, str(err))

    def _finish(self, ok: bool, error: str | None):
        if self.finished_ok is not None:
            return
        self.finished_ok = ok
        self.bar.setRange(0, 1000)
        self.bar.setValue(1000 if ok else self.bar.value())
        if ok:
            self.overall.setValue(1000)
        self.cancel_btn.setEnabled(False)
        self.close_btn.setEnabled(True)
        self.close_btn.setDefault(True)
        self.close_btn.setFocus()
        self.note.hide()
        took = _duration(time.monotonic() - self.started)
        if ok:
            self.heading.setText("Completed successfully")
            self.step.setText(f"Finished in {took}.")
        else:
            self.heading.setText("Stopped")
            self.step.setText(f"<span style='color:#e01b24'>{html.escape(error or 'Failed')}</span>")
            self.step.setTextFormat(Qt.TextFormat.RichText)
            if not self.details_btn.isChecked():
                self.details_btn.setChecked(True)


def _duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds} s"
    m, s = divmod(seconds, 60)
    if m < 60:
        return f"{m} min {s} s"
    h, m = divmod(m, 60)
    return f"{h} h {m} min"
