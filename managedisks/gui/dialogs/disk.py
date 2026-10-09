"""Disk level dialogs: partition table, wipe, health, clone, images."""

from __future__ import annotations

import os

from ...core import parttypes, smart
from ...core.model import Disk, Partition
from ...core.operations import WIPE_METHODS, ConvertTable, CreateTable, Wipe
from ...core.units import format_size
from .. import icons, widgets
from ..qt import QtCore, QtGui, QtWidgets
from .common import Dialog, info_grid, warning_label

Qt = QtCore.Qt


class NewTableDialog(Dialog):
    def __init__(self, parent, disk: Disk):
        super().__init__(parent, "New Partition Table", f"Create a partition table on {disk.title()}",
                         ok_text="Create")
        self.disk = disk
        self.gpt = QtWidgets.QRadioButton("GPT (recommended)")
        self.gpt.setChecked(True)
        gpt_desc = QtWidgets.QLabel("<small>Modern standard. Required for UEFI boot and disks larger than "
                                    "2 TiB; up to 128 partitions.</small>")
        self.mbr = QtWidgets.QRadioButton("MBR / DOS")
        mbr_desc = QtWidgets.QLabel("<small>Legacy BIOS systems and very old devices. Max 2 TiB and "
                                    "4 primary partitions (more via an extended partition).</small>")
        for w in (gpt_desc, mbr_desc):
            w.setWordWrap(True)
            w.setContentsMargins(24, 0, 0, 0)
        for w in (self.gpt, gpt_desc, self.mbr, mbr_desc):
            self.body.addWidget(w)
        if disk.sectors > 0xFFFFFFFF:
            self.mbr.setEnabled(False)
            mbr_desc.setText(mbr_desc.text() + "<br><small><i>Not possible: this disk is larger than 2 TiB.</i></small>")
        if disk.partitions or disk.fstype:
            self.body.addWidget(warning_label(
                f"<b>Everything on {disk.path} ({format_size(disk.size)}) will be erased.</b> "
                "To keep the data use “Convert to GPT/MBR” instead."))

    def operation(self) -> CreateTable:
        return CreateTable(self.disk.path, "gpt" if self.gpt.isChecked() else "dos")


class WipeDialog(Dialog):
    def __init__(self, parent, disk: Disk, part: Partition | None):
        target = part.display_name() if part else f"{disk.title()} ({disk.path})"
        super().__init__(parent, "Wipe", f"Wipe {target}", ok_text="Wipe")
        self.disk, self.part = disk, part
        self.group = QtWidgets.QButtonGroup(self)
        rotational = disk.rotational
        for key, text in WIPE_METHODS.items():
            name, _, desc = text.partition(":")
            rb = QtWidgets.QRadioButton(name if desc else text)
            rb.setProperty("method", key)
            if key == "discard" and (rotational or part is not None):
                rb.setEnabled(False)
                rb.setToolTip("Only available for whole solid-state disks")
            self.group.addButton(rb)
            self.body.addWidget(rb)
            if desc:
                lab = QtWidgets.QLabel(f"<small>{desc.strip().capitalize()}</small>")
                lab.setContentsMargins(24, 0, 0, 0)
                self.body.addWidget(lab)
            if key == "quick":
                rb.setChecked(True)
        size = (part.size * disk.sector_size) if part else disk.size
        self.body.addWidget(warning_label(f"<b>All data on {target} ({format_size(size)}) will be destroyed.</b> "
                                          "Overwriting takes a long time on large disks."))

    def operation(self) -> Wipe:
        method = self.group.checkedButton().property("method")
        return Wipe(self.disk.path, start=self.part.start if self.part else None, method=method)


def convert_operation(disk: Disk) -> ConvertTable:
    return ConvertTable(disk.path, to="dos" if disk.table == "gpt" else "gpt")


class SmartDialog(QtWidgets.QDialog):
    """Health overview from smartctl."""

    def __init__(self, parent, disk: Disk, fetch, start_test):
        super().__init__(parent)
        self.setWindowTitle(f"Health — {disk.title()}")
        self.disk = disk
        self.fetch = fetch
        self.start_test = start_test
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(18, 16, 18, 14)
        lay.setSpacing(12)
        head = QtWidgets.QHBoxLayout()
        self.badge = widgets.Chip("Reading…")
        title = QtWidgets.QLabel(f"<b style='font-size:large'>{disk.title()}</b><br>{disk.path} · "
                                 f"{format_size(disk.size)}")
        head.addWidget(self._icon_label())
        head.addWidget(title, 1)
        head.addWidget(self.badge)
        lay.addLayout(head)
        self.cards = QtWidgets.QHBoxLayout()
        lay.addLayout(self.cards)
        self.table = QtWidgets.QTreeWidget()
        self.table.setRootIsDecorated(False)
        self.table.setAlternatingRowColors(True)
        self.table.setHeaderLabels(["ID", "Attribute", "Value", "Worst", "Threshold", "Raw", "Status"])
        lay.addWidget(self.table, 1)
        self.message = QtWidgets.QLabel()
        self.message.setWordWrap(True)
        lay.addWidget(self.message)
        bb = QtWidgets.QHBoxLayout()
        self.short_btn = QtWidgets.QPushButton("Run short self-test")
        self.long_btn = QtWidgets.QPushButton("Run extended self-test")
        self.refresh_btn = QtWidgets.QPushButton(icons.icon("refresh"), "Refresh")
        close = QtWidgets.QPushButton("Close")
        close.clicked.connect(self.accept)
        self.short_btn.clicked.connect(lambda: self._test("short"))
        self.long_btn.clicked.connect(lambda: self._test("long"))
        self.refresh_btn.clicked.connect(self.load)
        for b in (self.short_btn, self.long_btn, self.refresh_btn):
            bb.addWidget(b)
        bb.addStretch(1)
        bb.addWidget(close)
        lay.addLayout(bb)
        self.resize(760, 560)
        QtCore.QTimer.singleShot(0, self.load)

    def _icon_label(self):
        lab = QtWidgets.QLabel()
        lab.setPixmap(icons.media_icon(self.disk.media_kind()).pixmap(40, 40))
        return lab

    def load(self):
        self.badge.setText("Reading…")
        self.badge.set_color(None)
        self.fetch(self.disk.path, self._loaded, self._failed)

    def _failed(self, err):
        self.badge.setText("Unavailable")
        self.message.setText(str(err))

    def _card(self, title, value):
        box = QtWidgets.QFrame()
        box.setFrameShape(QtWidgets.QFrame.Shape.StyledPanel)
        v = QtWidgets.QVBoxLayout(box)
        t = QtWidgets.QLabel(title)
        t.setStyleSheet("font-size: small;")
        val = QtWidgets.QLabel(value)
        f = val.font()
        f.setPointSizeF(f.pointSizeF() * 1.35)
        f.setWeight(QtGui.QFont.Weight.DemiBold)
        val.setFont(f)
        v.addWidget(t)
        v.addWidget(val)
        return box

    def _loaded(self, raw):
        r = smart.parse(raw or {})
        while self.cards.count():
            it = self.cards.takeAt(0)
            if it.widget():
                it.widget().hide()
                it.widget().deleteLater()
        self.table.clear()
        if not r.available:
            self.badge.setText("Not supported")
            self.message.setText(r.message)
            self.short_btn.setEnabled(False)
            self.long_btn.setEnabled(False)
            return
        label, color = {"good": ("Healthy", "#2ec27e"), "warning": ("Warning", "#e5a50a"),
                        "bad": ("Failing", "#e01b24"), "unknown": ("Unknown", None)}[r.verdict]
        self.badge.setText(label)
        self.badge.set_color(color)
        if r.temperature is not None:
            self.cards.addWidget(self._card("Temperature", f"{r.temperature} °C"))
        if r.power_on_hours is not None:
            days = r.power_on_hours / 24
            self.cards.addWidget(self._card("Powered on", f"{r.power_on_hours:,} h ({days:,.0f} days)"))
        if r.percentage_used is not None:
            self.cards.addWidget(self._card("Wear", f"{r.percentage_used}% used"))
        if r.data_written is not None:
            self.cards.addWidget(self._card("Data written", format_size(r.data_written)))
        if r.power_cycles is not None:
            self.cards.addWidget(self._card("Power cycles", f"{r.power_cycles:,}"))
        self.cards.addStretch(1)
        for a in r.attributes:
            status = "Failing" if a.failing else "Check" if a.warning else "OK"
            item = QtWidgets.QTreeWidgetItem([str(a.id), a.name, str(a.value), str(a.worst), str(a.threshold),
                                              a.raw, status])
            if a.failing or a.warning:
                for col in range(7):
                    item.setForeground(col, QtGui.QBrush(QtGui.QColor("#e01b24" if a.failing else "#c88800")))
            self.table.addTopLevelItem(item)
        if not r.attributes and r.raw.get("nvme_smart_health_information_log"):
            for k, v in r.raw["nvme_smart_health_information_log"].items():
                self.table.addTopLevelItem(QtWidgets.QTreeWidgetItem(["", k.replace("_", " "), str(v), "", "", "", ""]))
        for i in range(7):
            self.table.resizeColumnToContents(i)
        msg = []
        if r.self_test_status:
            msg.append(f"Last self-test: {r.self_test_status}")
        if r.model:
            msg.append(f"{r.model} · S/N {r.serial} · firmware {r.firmware}")
        self.message.setText("<br>".join(msg))

    def _test(self, kind):
        self.start_test(self.disk.path, kind,
                        lambda text: self.message.setText(str(text)),
                        lambda err: self.message.setText(f"<span style='color:#e01b24'>{err}</span>"))


class DiskPicker(Dialog):
    """Pick a target disk (clone)."""

    def __init__(self, parent, title, heading, disks: list[Disk], source: Disk, ok_text="Clone"):
        super().__init__(parent, title, heading, ok_text=ok_text)
        self.list = QtWidgets.QListWidget()
        self.list.setIconSize(QtCore.QSize(32, 32))
        for d in disks:
            if d.path == source.path:
                continue
            it = QtWidgets.QListWidgetItem(icons.media_icon(d.media_kind()),
                                           f"{d.title()}\n{d.path} · {format_size(d.size)}")
            it.setData(Qt.ItemDataRole.UserRole, d.path)
            if d.size < source.size or d.busy or d.read_only:
                it.setFlags(it.flags() & ~Qt.ItemFlag.ItemIsEnabled)
                it.setToolTip("Too small" if d.size < source.size else "In use or read-only")
            self.list.addItem(it)
        self.body.addWidget(self.list)
        self.new_ids = QtWidgets.QCheckBox("Give the clone new disk and partition identifiers")
        self.new_ids.setChecked(True)
        self.body.addWidget(self.new_ids)
        self.body.addWidget(warning_label("<b>Everything on the selected target disk will be overwritten.</b>"))
        self.resize(520, 420)

    def target(self) -> str | None:
        it = self.list.currentItem()
        if it is None or not (it.flags() & Qt.ItemFlag.ItemIsEnabled):
            return None
        return it.data(Qt.ItemDataRole.UserRole)

    def validate(self):
        return None if self.target() else "Choose a target disk"


def pick_image_to_write(parent) -> str | None:
    path, _ = QtWidgets.QFileDialog.getOpenFileName(
        parent, "Choose a disk image to write", os.path.expanduser("~"),
        "Disk images (*.iso *.img *.raw *.bin *.img.xz *.img.gz *.img.bz2 *.img.zst *.xz *.gz);;All files (*)")
    return path or None


def pick_image_to_save(parent, default_name: str) -> tuple[str, bool] | None:
    path, flt = QtWidgets.QFileDialog.getSaveFileName(
        parent, "Save disk image", os.path.join(os.path.expanduser("~"), default_name),
        "Raw image (*.img);;Compressed image (*.img.gz)")
    if not path:
        return None
    compress = path.endswith(".gz") or "gz" in flt
    if compress and not path.endswith(".gz"):
        path += ".gz"
    return path, compress


def disk_properties(parent, disk: Disk) -> None:
    dlg = Dialog(parent, "Disk Properties", disk.title(), ok_text="Close")
    dlg.buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Cancel).hide()
    rows = [("Device", disk.path), ("Model", disk.title()), ("Serial number", disk.serial),
            ("Capacity", f"{format_size(disk.size)} ({disk.size:,} bytes)"),
            ("Sectors", f"{disk.sectors:,} × {disk.sector_size} bytes (physical {disk.physical_sector_size})"),
            ("Connection", disk.transport.upper() if disk.transport else ""),
            ("Media", "Rotational (HDD)" if disk.rotational else "Solid state"),
            ("Removable", "Yes" if disk.removable else "No"),
            ("Partition table", parttypes.table_label(disk.table)),
            ("Disk identifier", disk.table_uuid),
            ("Usable sectors", f"{disk.first_usable:,} – {disk.last_usable:,}"
             + ("" if disk.geometry_exact else " (estimated; unlock for exact values)")),
            ("Unallocated", format_size(disk.free_bytes()))]
    if disk.backing_file:
        rows.append(("Backing file", disk.backing_file))
    dlg.body.addWidget(info_grid(rows))
    dlg.resize(520, dlg.sizeHint().height())
    dlg.exec()

