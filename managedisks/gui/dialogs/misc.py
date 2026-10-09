"""Filesystem support table, apply confirmation and About."""

from __future__ import annotations

from ... import APP_NAME, __version__
from ...core import distro, filesystems
from ...core.operations import Operation
from .. import icons, theme, widgets
from ..qt import QT_API, QtCore, QtGui, QtWidgets
from .common import Dialog, warning_label

Qt = QtCore.Qt


class SupportDialog(Dialog):
    def __init__(self, parent):
        super().__init__(parent, "Filesystem Support", "Filesystem support",
                         subtitle="What ManageDisks can do with each filesystem using the tools installed "
                                  "on this computer.", ok_text="Close")
        self.buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Cancel).hide()
        rows = filesystems.support_matrix()
        caps = list(filesystems.CAPABILITIES)
        table = QtWidgets.QTableWidget(len(rows), len(caps) + 2)
        table.setHorizontalHeaderLabels(["Filesystem"] + [filesystems.CAPABILITY_NAMES[c] for c in caps]
                                        + ["Install"])
        table.verticalHeader().hide()
        table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.NoSelection)
        table.setAlternatingRowColors(True)
        table.setShowGrid(False)
        for r, row in enumerate(rows):
            name = QtWidgets.QTableWidgetItem(widgets.color_swatch(theme.fs_color(row.fs.color)), row.fs.name)
            name.setToolTip(row.fs.description)
            table.setItem(r, 0, name)
            for c, cap in enumerate(caps, start=1):
                v = row.caps[cap]
                it = QtWidgets.QTableWidgetItem()
                if v is None:
                    it.setText("—")
                    it.setToolTip("Not possible for this filesystem")
                elif v:
                    it.setText("✓")
                    it.setForeground(QtGui.QBrush(QtGui.QColor("#2ec27e")))
                    it.setToolTip("Supported")
                else:
                    it.setText("✗")
                    it.setForeground(QtGui.QBrush(QtGui.QColor("#e01b24")))
                    it.setToolTip("Missing: " + ", ".join(row.fs.missing_tools(cap)))
                it.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                table.setItem(r, c, it)
            table.setItem(r, len(caps) + 1, QtWidgets.QTableWidgetItem(", ".join(row.packages)))
        table.resizeColumnsToContents()
        table.horizontalHeader().setStretchLastSection(True)
        self.body.addWidget(table, 1)
        pkgs = filesystems.missing_packages()
        line = QtWidgets.QHBoxLayout()
        if pkgs:
            cmd = distro.install_command(pkgs)
            lab = QtWidgets.QLabel(f"To enable everything on {distro.pretty_name()}, run:")
            self.body.addWidget(lab)
            edit = QtWidgets.QLineEdit(cmd)
            edit.setReadOnly(True)
            edit.setFont(QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.SystemFont.FixedFont))
            copy = QtWidgets.QPushButton(icons.icon("copy"), "Copy")
            copy.clicked.connect(lambda: QtWidgets.QApplication.clipboard().setText(cmd))
            line.addWidget(edit, 1)
            line.addWidget(copy)
            self.body.addLayout(line)
        else:
            self.body.addWidget(QtWidgets.QLabel("All tools are installed."))
        self.resize(880, 680)


class ApplyConfirmDialog(Dialog):
    def __init__(self, parent, ops: list[Operation], warnings: list[str]):
        destructive = any(o.destructive for o in ops)
        n = len(ops)
        super().__init__(parent, "Apply Operations", f"Apply {n} pending operation{'s' if n != 1 else ''}?",
                         ok_text="Apply")
        lst = QtWidgets.QListWidget()
        for i, o in enumerate(ops, 1):
            it = QtWidgets.QListWidgetItem(f"{i}. {o.describe()}")
            if o.destructive:
                it.setForeground(QtGui.QColor("#e01b24"))
            lst.addItem(it)
        lst.setMinimumHeight(min(260, 26 * n + 10))
        self.body.addWidget(lst)
        for w in warnings:
            self.body.addWidget(warning_label(w))
        if destructive:
            self.body.addWidget(warning_label("<b>Some of these operations permanently delete data.</b> "
                                              "Make sure you have backups of anything important."))
        else:
            self.body.addWidget(warning_label("Changing partitions always carries some risk. "
                                              "Keep backups of important data."))
        if destructive:
            self.ok_button.setStyleSheet("")
        self.resize(600, self.sizeHint().height())


def about(parent):
    box = QtWidgets.QMessageBox(parent)
    box.setWindowTitle(f"About {APP_NAME}")
    box.setIconPixmap(icons.app_icon().pixmap(72, 72))
    box.setText(f"<h2>{APP_NAME} {__version__}</h2>"
                "<p>A modern disk and partition manager for Linux: partitions, filesystems, "
                "encryption, health checks, cloning and imaging.</p>")
    box.setInformativeText(f"Qt {QtCore.qVersion()} ({QT_API}) · {distro.pretty_name()}<br>"
                           "Released under the MIT license.")
    box.exec()
