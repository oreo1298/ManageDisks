"""Shared dialog building blocks."""

from __future__ import annotations

from ...core import distro, filesystems
from ...core.filesystems import Filesystem
from .. import icons
from ..qt import QtCore, QtGui, QtWidgets

Qt = QtCore.Qt


def message(parent, title: str, text: str, kind: str = "info", details: str | None = None) -> None:
    box = QtWidgets.QMessageBox(parent)
    box.setWindowTitle(title)
    box.setText(text)
    box.setIcon({"info": QtWidgets.QMessageBox.Icon.Information,
                 "warning": QtWidgets.QMessageBox.Icon.Warning,
                 "error": QtWidgets.QMessageBox.Icon.Critical}[kind])
    if details:
        box.setDetailedText(details)
    box.exec()


def confirm(parent, title: str, text: str, ok_text: str = "Continue", destructive: bool = False,
            informative: str | None = None) -> bool:
    box = QtWidgets.QMessageBox(parent)
    box.setWindowTitle(title)
    box.setIcon(QtWidgets.QMessageBox.Icon.Warning if destructive else QtWidgets.QMessageBox.Icon.Question)
    box.setText(text)
    if informative:
        box.setInformativeText(informative)
    ok = box.addButton(ok_text, QtWidgets.QMessageBox.ButtonRole.AcceptRole)
    cancel = box.addButton(QtWidgets.QMessageBox.StandardButton.Cancel)
    box.setDefaultButton(cancel if destructive else ok)
    box.exec()
    return box.clickedButton() is ok


class Dialog(QtWidgets.QDialog):
    """Dialog with a heading, a form area and OK/Cancel buttons."""

    def __init__(self, parent, title: str, heading: str | None = None, ok_text: str = "OK",
                 subtitle: str | None = None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self.outer = QtWidgets.QVBoxLayout(self)
        self.outer.setContentsMargins(18, 16, 18, 14)
        self.outer.setSpacing(12)
        if heading:
            h = QtWidgets.QLabel(heading)
            f = h.font()
            f.setPointSizeF(f.pointSizeF() * 1.25)
            f.setWeight(QtGui.QFont.Weight.DemiBold)
            h.setFont(f)
            h.setWordWrap(True)
            self.outer.addWidget(h)
        if subtitle:
            s = QtWidgets.QLabel(subtitle)
            s.setWordWrap(True)
            s.setForegroundRole(QtGui.QPalette.ColorRole.PlaceholderText
                                if hasattr(QtGui.QPalette.ColorRole, "PlaceholderText")
                                else QtGui.QPalette.ColorRole.WindowText)
            self.outer.addWidget(s)
        self.body = QtWidgets.QVBoxLayout()
        self.body.setSpacing(10)
        self.outer.addLayout(self.body, 1)
        self.error = QtWidgets.QLabel()
        self.error.setWordWrap(True)
        self.error.setStyleSheet("color: #e01b24;")
        self.error.hide()
        self.outer.addWidget(self.error)
        self.buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Ok
                                                  | QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        self.ok_button = self.buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Ok)
        self.ok_button.setText(ok_text)
        self.buttons.accepted.connect(self._try_accept)
        self.buttons.rejected.connect(self.reject)
        self.outer.addWidget(self.buttons)

    def fit_height(self):
        """Grow to fit the content at the current width (word-wrapped labels
        need more height than QLayout's plain size hint reports)."""
        lay = self.layout()
        need = lay.totalHeightForWidth(self.width()) if lay.hasHeightForWidth() else -1
        need = max(need, self.sizeHint().height())
        if self.height() < need:
            self.resize(self.width(), need)

    def showEvent(self, e):
        super().showEvent(e)
        QtCore.QTimer.singleShot(0, self.fit_height)

    def form(self) -> QtWidgets.QFormLayout:
        f = QtWidgets.QFormLayout()
        f.setFieldGrowthPolicy(QtWidgets.QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        f.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        f.setHorizontalSpacing(12)
        f.setVerticalSpacing(8)
        self.body.addLayout(f)
        return f

    def show_error(self, text: str | None):
        self.error.setText(text or "")
        self.error.setVisible(bool(text))

    def validate(self) -> str | None:
        """Return an error message to keep the dialog open."""
        return None

    def _try_accept(self):
        err = self.validate()
        if err:
            self.show_error(err)
            return
        self.accept()


def warning_label(text: str) -> QtWidgets.QWidget:
    w = QtWidgets.QWidget()
    lay = QtWidgets.QHBoxLayout(w)
    lay.setContentsMargins(0, 0, 0, 0)
    ic = QtWidgets.QLabel()
    ic.setPixmap(icons.icon("warning", prefer_builtin=True).pixmap(18, 18))
    ic.setAlignment(Qt.AlignmentFlag.AlignTop)
    lab = QtWidgets.QLabel(text)
    lab.setWordWrap(True)
    lay.addWidget(ic)
    lay.addWidget(lab, 1)
    return w


class FsCombo(QtWidgets.QComboBox):
    """Filesystem picker: unavailable filesystems are disabled with a hint
    telling the user which package to install."""

    def __init__(self, allow_none: bool = True, size_bytes: int | None = None, table: str | None = None,
                 parent=None):
        super().__init__(parent)
        model = QtGui.QStandardItemModel(self)
        self.setModel(model)
        from .. import theme, widgets
        for fs in filesystems.creatable():
            if fs.id == "luks" or (fs.id == "cleared" and not allow_none):
                continue
            item = QtGui.QStandardItem(fs.name)
            item.setData(fs.id, Qt.ItemDataRole.UserRole)
            item.setIcon(widgets.color_swatch(theme.fs_color(fs.color)))
            tip = fs.description
            enabled = fs.supports("create")
            if not enabled:
                pkgs = sorted({filesystems.package_for(t) for t in fs.missing_tools("create")})
                tip += f"\nNot available: install {', '.join(pkgs)}\n{distro.install_command(pkgs)}"
                item.setText(f"{fs.name} (not installed)")
            if fs.warning and enabled:
                item.setText(f"{fs.name} (experimental)")
            if fs.gpt_only and table not in (None, "gpt") and enabled:
                enabled = False
                tip += "\nNeeds a GPT partition table"
            if size_bytes is not None and enabled:
                if size_bytes < fs.min_size:
                    enabled = False
                    tip += f"\nNeeds at least {_fmt(fs.min_size)}"
                elif fs.max_size and size_bytes > fs.max_size:
                    enabled = False
                    tip += f"\nCan be at most {_fmt(fs.max_size)}"
            item.setToolTip(tip)
            item.setEnabled(enabled)
            model.appendRow(item)
        self.set_fs("ext4")

    def fs_id(self) -> str:
        return self.currentData(Qt.ItemDataRole.UserRole) or ""

    def fs(self) -> Filesystem | None:
        return filesystems.BY_ID.get(self.fs_id())

    def set_fs(self, fs_id: str):
        for i in range(self.count()):
            if self.itemData(i, Qt.ItemDataRole.UserRole) == fs_id:
                item = self.model().item(i)
                if item is not None and item.isEnabled():
                    self.setCurrentIndex(i)
                    return
        for i in range(self.count()):
            item = self.model().item(i)
            if item is not None and item.isEnabled():
                self.setCurrentIndex(i)
                return


def _fmt(n):
    from ...core.units import format_size
    return format_size(n)


class PassphraseFields(QtWidgets.QWidget):
    """Encrypt checkbox + passphrase/confirmation with a strength hint."""

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QtWidgets.QFormLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.encrypt = QtWidgets.QCheckBox("Encrypt with LUKS2 (password required to unlock)")
        self.encrypt.setEnabled(filesystems.BY_ID["luks"].supports("create"))
        if not self.encrypt.isEnabled():
            self.encrypt.setToolTip("Install cryptsetup to create encrypted partitions")
        self.pass1 = QtWidgets.QLineEdit()
        self.pass1.setEchoMode(QtWidgets.QLineEdit.EchoMode.Password)
        self.pass1.setPlaceholderText("Passphrase")
        self.pass2 = QtWidgets.QLineEdit()
        self.pass2.setEchoMode(QtWidgets.QLineEdit.EchoMode.Password)
        self.pass2.setPlaceholderText("Repeat passphrase")
        self.strength = QtWidgets.QLabel()
        lay.addRow(self.encrypt)
        lay.addRow("Passphrase:", self.pass1)
        lay.addRow("Confirm:", self.pass2)
        lay.addRow("", self.strength)
        self.encrypt.toggled.connect(self._toggle)
        self.pass1.textChanged.connect(self._strength)
        self._toggle(False)

    def _toggle(self, on):
        for w in (self.pass1, self.pass2, self.strength):
            w.setVisible(on)
            lab = self.layout().labelForField(w)
            if lab:
                lab.setVisible(on)

    def _strength(self, text):
        n = len(text)
        classes = sum(bool(any(f(c) for c in text)) for f in (str.islower, str.isupper, str.isdigit,
                                                                 lambda c: not c.isalnum()))
        score = n // 4 + classes
        label, color = (("Weak", "#e01b24") if score < 4 else ("Fair", "#e5a50a") if score < 6
                        else ("Strong", "#2ec27e"))
        self.strength.setText(f"<span style='color:{color}'>{label}</span>" if text else "")

    def enabled(self) -> bool:
        return self.encrypt.isChecked()

    def passphrase(self) -> str:
        return self.pass1.text()

    def validate(self) -> str | None:
        if not self.enabled():
            return None
        if not self.pass1.text():
            return "Enter a passphrase for the encrypted partition"
        if self.pass1.text() != self.pass2.text():
            return "The passphrases do not match"
        return None


def ask_passphrase(parent, title: str, text: str) -> str | None:
    dlg = Dialog(parent, title, title, ok_text="Unlock", subtitle=text)
    edit = QtWidgets.QLineEdit()
    edit.setEchoMode(QtWidgets.QLineEdit.EchoMode.Password)
    edit.setMinimumWidth(320)
    dlg.body.addWidget(edit)
    edit.setFocus()
    if dlg.exec() == QtWidgets.QDialog.DialogCode.Accepted and edit.text():
        return edit.text()
    return None


def info_grid(rows: list[tuple[str, str]]) -> QtWidgets.QWidget:
    w = QtWidgets.QWidget()
    g = QtWidgets.QGridLayout(w)
    g.setContentsMargins(0, 0, 0, 0)
    g.setHorizontalSpacing(16)
    g.setVerticalSpacing(6)
    for i, (k, v) in enumerate(rows):
        kl = QtWidgets.QLabel(k)
        kl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
        kl.setForegroundRole(QtGui.QPalette.ColorRole.PlaceholderText
                             if hasattr(QtGui.QPalette.ColorRole, "PlaceholderText")
                             else QtGui.QPalette.ColorRole.WindowText)
        vl = QtWidgets.QLabel(v or "—")
        vl.setWordWrap(True)
        vl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        g.addWidget(kl, i, 0)
        g.addWidget(vl, i, 1)
    g.setColumnStretch(1, 1)
    return w
