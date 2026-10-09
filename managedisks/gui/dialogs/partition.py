"""Dialogs that create, resize, format and edit partitions."""

from __future__ import annotations

from ...core import filesystems, parttypes
from ...core.model import Disk, FreeSpace, Partition
from ...core.operations import (CopyPartition, CreatePartition, FormatPartition, ResizeMovePartition,
                                SetLabel, SetPartitionProps)
from ...core.units import MiB, align_down, align_up, format_size
from .. import widgets
from ..qt import QtCore, QtGui, QtWidgets
from .common import Dialog, FsCombo, PassphraseFields, info_grid, warning_label

Qt = QtCore.Qt


class _Geometry(QtWidgets.QWidget):
    """Resizer + before/size/after fields kept in sync (all in sectors)."""

    def __init__(self, disk: Disk, lo: int, hi: int, start: int, size: int, *, min_size: int,
                 max_size: int | None = None, can_move=True, can_resize_left=True, used=0,
                 color="#3d7cd6", parent=None):
        super().__init__(parent)
        self.disk = disk
        self.ss = disk.sector_size
        self.lo, self.hi = lo, hi
        self.min_size = max(1, min_size)
        self.max_size = max_size if max_size else hi - lo + 1
        self.start, self.size = start, size
        self.callbacks = []
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.resizer = widgets.PartitionResizer()
        self.resizer.configure(lo, hi, start, size, min_size=self.min_size, max_size=self.max_size,
                               align=disk.alignment, can_move=can_move, can_resize_left=can_resize_left,
                               used=used, color=color, sector_size=self.ss)
        lay.addWidget(self.resizer)
        form = QtWidgets.QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.before = widgets.SizeEdit()
        self.size_edit = widgets.SizeEdit()
        self.after = widgets.SizeEdit()
        total = (hi - lo + 1) * self.ss
        for w in (self.before, self.size_edit, self.after):
            w.setRange(0, total)
        self.size_edit.setRange(self.min_size * self.ss, min(self.max_size, hi - lo + 1) * self.ss)
        form.addRow("Free space before:", self.before)
        form.addRow("Size:", self.size_edit)
        form.addRow("Free space after:", self.after)
        self.before.setEnabled(can_move or can_resize_left)
        lay.addLayout(form)
        self._lock = False
        self._refresh()
        self.resizer.changed.connect(self._from_resizer)
        self.before.valueChanged.connect(self._from_before)
        self.size_edit.valueChanged.connect(self._from_size)
        self.after.valueChanged.connect(self._from_after)
        self.can_move = can_move

    def on_change(self, fn):
        self.callbacks.append(fn)

    def _emit(self):
        for fn in self.callbacks:
            fn()

    def _refresh(self):
        self._lock = True
        self.before.setValue((self.start - self.lo) * self.ss)
        self.size_edit.setValue(self.size * self.ss)
        self.after.setValue((self.hi + 1 - self.start - self.size) * self.ss)
        self.resizer.set_geometry(self.start, self.size)
        self._lock = False
        self._emit()

    def _snap(self, sectors: int) -> int:
        a = self.disk.alignment
        return align_down(sectors, a) if sectors >= a else sectors

    def _from_resizer(self, start, size):
        self.start, self.size = start, size
        self._refresh()

    def _from_before(self, b):
        if self._lock:
            return
        before = self._snap(b // self.ss)
        start = self.lo + before
        if not self.can_move:
            # Moving the left edge without moving: change size instead.
            end = self.start + self.size
            start = min(start, end - self.min_size)
            self.size = end - start
        start = max(self.lo, min(start, self.hi + 1 - self.size))
        self.start = start
        self._refresh()

    def _from_size(self, b):
        if self._lock:
            return
        size = self._snap(b // self.ss)
        size = max(self.min_size, min(size, self.max_size))
        if self.start + size - 1 > self.hi:
            if self.can_move:
                self.start = max(self.lo, self.hi + 1 - size)
            size = self.hi + 1 - self.start
        self.size = size
        self._refresh()

    def _from_after(self, b):
        if self._lock:
            return
        after = self._snap(b // self.ss)
        end = self.hi + 1 - after
        size = max(self.min_size, min(end - self.start, self.max_size))
        if self.start + size - 1 > self.hi:
            size = self.hi + 1 - self.start
        self.size = size
        self._refresh()

    def set_min_size(self, sectors: int):
        self.min_size = max(1, sectors)
        self.resizer.min_size = self.min_size
        self.size_edit.setRange(self.min_size * self.ss, min(self.max_size, self.hi - self.lo + 1) * self.ss)
        if self.size < self.min_size:
            self.size = self.min_size
        self._refresh()

    def set_color(self, color: str):
        self.resizer.color = QtGui.QColor(color)
        self.resizer.update()


def _can_encrypt(fs) -> bool:
    # Macs cannot open LUKS volumes, so encrypting Mac filesystems is pointless.
    return bool(fs) and fs.id not in ("cleared", "linux-swap", "hfsplus", "apfs")


def _show_fs_warning(widget: QtWidgets.QWidget, fs) -> None:
    text = fs.warning if fs is not None else ""
    widget.findChildren(QtWidgets.QLabel)[-1].setText(text)
    widget.setVisible(bool(text))
    dlg = widget.window()
    if hasattr(dlg, "fit_height") and dlg.isVisible():  # grow instead of squeezing the fields
        QtCore.QTimer.singleShot(0, dlg.fit_height)


class CreatePartitionDialog(Dialog):
    def __init__(self, parent, disk: Disk, free: FreeSpace):
        super().__init__(parent, "New Partition", "Create a new partition", ok_text="Add",
                         subtitle=f"On {disk.title()} ({disk.path}), in {format_size(free.size * disk.sector_size)} "
                                  "of unallocated space.")
        self.disk = disk
        self.free = free
        ustart, usize = disk.usable_extent(free)
        self.geo = _Geometry(disk, ustart, free.end, ustart, usize or free.size, min_size=disk.alignment,
                             color=filesystems.BY_ID["ext4"].color)
        self.body.addWidget(self.geo)
        form = self.form()
        self.kind_box = None
        if disk.table == "dos":
            self.kind_box = QtWidgets.QComboBox()
            if free.inside_extended:
                self.kind_box.addItem("Logical partition", "logical")
            else:
                if disk.primary_count() < 4:
                    self.kind_box.addItem("Primary partition", "primary")
                    if not disk.extended():
                        self.kind_box.addItem("Extended partition (container for logical partitions)",
                                              "extended")
            form.addRow("Create as:", self.kind_box)
            self.kind_box.currentIndexChanged.connect(self._kind_changed)
        self.fs = FsCombo(allow_none=True, table=disk.table)
        self.fs_desc = QtWidgets.QLabel()
        self.fs_desc.setWordWrap(True)
        self.fs_desc.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Preferred)
        fs_row = QtWidgets.QHBoxLayout()
        fs_row.setSpacing(10)
        fs_row.addWidget(self.fs)
        fs_row.addWidget(self.fs_desc, 1)
        form.addRow("Filesystem:", fs_row)
        self.label = QtWidgets.QLineEdit()
        self.label.setPlaceholderText("Optional")
        form.addRow("Label:", self.label)
        self.name = None
        if disk.table == "gpt":
            self.name = QtWidgets.QLineEdit()
            self.name.setPlaceholderText("Optional GPT partition name")
            self.name.setMaxLength(36)
            form.addRow("Partition name:", self.name)
        self.type = QtWidgets.QComboBox()
        self.type.addItem("Automatic (matches the filesystem)", "")
        for pt in parttypes.choices(disk.table or "gpt"):
            self.type.addItem(pt.name, pt.key)
        form.addRow("Partition type:", self.type)
        self.crypt = PassphraseFields()
        self.body.addWidget(self.crypt)
        self.fs_warning = warning_label("")
        self.body.addWidget(self.fs_warning)
        self.fs.currentIndexChanged.connect(self._fs_changed)
        self._fs_changed()
        self._kind_changed()
        self.resize(560, self.sizeHint().height())

    def _kind_changed(self, *_):
        ext = self.kind_box is not None and self.kind_box.currentData() == "extended"
        for w in (self.fs, self.label, self.type, self.crypt, self.fs_desc):
            w.setEnabled(not ext)

    def _fs_changed(self, *_):
        fs = self.fs.fs()
        if fs:
            self.fs_desc.setText(f"<small>{fs.description}</small>")
            self.label.setMaxLength(fs.max_label if fs.id != "cleared" else 0)
            self.label.setEnabled(fs.id != "cleared")
            self.geo.set_color(fs.color)
        _show_fs_warning(self.fs_warning, fs)
        self.crypt.setEnabled(_can_encrypt(fs))
        if not self.crypt.isEnabled():
            self.crypt.encrypt.setChecked(False)

    def kind(self) -> str:
        if self.kind_box is None:
            return "normal"
        return self.kind_box.currentData() or "primary"

    def operation(self) -> CreatePartition:
        fs_id = self.fs.fs_id()
        kind = self.kind()
        return CreatePartition(
            self.disk.path, start=self.geo.start, size=self.geo.size, kind=kind,
            fstype="" if kind == "extended" or fs_id == "cleared" else fs_id,
            label=self.label.text().strip() if kind != "extended" else "",
            name=self.name.text().strip() if self.name else "",
            type_id=self.type.currentData() or "",
            encrypt=self.crypt.enabled(), passphrase=self.crypt.passphrase())

    def validate(self):
        if self.kind_box is not None and self.kind_box.count() == 0:
            return "No more partitions can be created here (MBR allows 4 primary partitions)."
        err = self.crypt.validate()
        if err:
            return err
        fs = self.fs.fs()
        if fs and self.kind() != "extended" and fs.id != "cleared":
            size = self.geo.size * self.disk.sector_size
            if size < fs.min_size:
                return f"{fs.name} needs at least {format_size(fs.min_size)}"
            if fs.max_size and size > fs.max_size:
                return f"{fs.name} can be at most {format_size(fs.max_size)}"
            lab = fs.normalize_label(self.label.text())
            if lab:
                e = fs.validate_label(lab)
                if e:
                    return e
        return None


class ResizeDialog(Dialog):
    def __init__(self, parent, disk: Disk, part: Partition, request_min_size=None):
        name = part.display_name()
        super().__init__(parent, "Resize/Move", f"Resize or move {name}", ok_text="Resize/Move")
        self.disk = disk
        self.part = part
        fs = filesystems.lookup(part.fstype, part.fsversion)
        if fs and fs.id == "cleared":
            fs = None
        lo, hi = disk.neighbours(part)
        if part.is_logical:
            lo = max(lo, align_up(lo, disk.alignment))
        mounted = part.busy
        if part.is_extended:
            can_move = not disk.logicals()
        else:
            can_move = not mounted and (fs is None or fs.movable)
        if part.fstype and fs is None and part.fstype != "crypto_LUKS":
            can_grow = can_shrink = False
        elif part.fstype == "crypto_LUKS":
            can_grow = can_shrink = False
        elif fs is None:
            can_grow = can_shrink = True
        else:
            can_grow = fs.supports("grow") and (not mounted or fs.grow_online)
            can_shrink = fs.supports("shrink") and (not mounted or fs.shrink_online)
        min_size = part.size if not can_shrink else disk.alignment
        if fs and can_shrink:
            min_size = max(min_size, -(-fs.min_size // disk.sector_size))
        used = 0
        if part.fs_used is not None and fs is not None:
            used = -(-part.fs_used // disk.sector_size)
            if can_shrink:
                min_size = max(min_size, used)
        if part.is_extended and disk.logicals():
            lg = disk.logicals()
            min_size = lg[-1].end - part.start + 1
            hi_cap = hi
            lo = part.start
            hi = hi_cap
        max_size = None if can_grow else part.size
        if fs and fs.max_size and can_grow:
            max_size = min(fs.max_size // disk.sector_size, hi - lo + 1)
        color = widgets.part_color(part, self.palette()).name()
        self.geo = _Geometry(disk, lo, hi, part.start, part.size, min_size=min_size, max_size=max_size,
                             can_move=can_move, can_resize_left=can_move, used=used, color=color)
        self.body.addWidget(self.geo)
        notes = []
        if mounted:
            notes.append("The partition is in use: only online-capable changes are possible "
                         "(no moving). Unmount it for more options.")
        if not can_grow and not can_shrink and not can_move:
            notes.append("This partition can neither be resized nor moved.")
        elif fs is not None:
            if not can_shrink:
                notes.append(f"{fs.name} cannot be shrunk" + (" while mounted" if fs.supports("shrink") else "") + ".")
            if not can_grow:
                notes.append(f"{fs.name} cannot be grown" + (" while mounted" if fs.supports("grow") else "") + ".")
        if can_move:
            notes.append("Moving a partition copies all of its data and can take a long time. "
                         "Moving a boot partition may require repairing the boot loader.")
        self.min_label = QtWidgets.QLabel()
        self.body.addWidget(self.min_label)
        for n in notes:
            self.body.addWidget(warning_label(n))
        self._fs = fs
        if fs and can_shrink and fs.implemented("shrink"):
            self.min_label.setText(f"Minimum size: {format_size(min_size * disk.sector_size)}"
                                   + (" (calculating…)" if request_min_size else ""))
            if request_min_size:
                request_min_size(self._got_min_size)
        else:
            self.min_label.hide()
        self.resize(560, self.sizeHint().height())

    def _got_min_size(self, value):
        if not value:
            return
        ss = self.disk.sector_size
        sectors = -(-int(value) // ss)
        sectors = int(sectors * 1.02) + (16 * MiB) // ss     # safety margin
        sectors = min(sectors, self.part.size)
        if sectors > self.geo.min_size:
            self.geo.set_min_size(sectors)
        self.min_label.setText(f"Minimum size: {format_size(self.geo.min_size * ss)}")

    def operation(self) -> ResizeMovePartition | None:
        if self.geo.start == self.part.start and self.geo.size == self.part.size:
            return None
        return ResizeMovePartition(self.disk.path, start=self.part.start, new_start=self.geo.start,
                                   new_size=self.geo.size)


class FormatDialog(Dialog):
    def __init__(self, parent, disk: Disk, part: Partition):
        super().__init__(parent, "Format Partition", f"Format {part.display_name()}", ok_text="Format")
        self.disk = disk
        self.part = part
        size = part.size * disk.sector_size
        form = self.form()
        self.fs = FsCombo(allow_none=True, size_bytes=size, table=disk.table)
        cur = filesystems.lookup(part.fstype, part.fsversion)
        if cur:
            self.fs.set_fs(cur.id)
        form.addRow("Filesystem:", self.fs)
        self.label = QtWidgets.QLineEdit(part.label)
        form.addRow("Label:", self.label)
        self.crypt = PassphraseFields()
        self.body.addWidget(self.crypt)
        self.fs_warning = warning_label("")
        self.body.addWidget(self.fs_warning)
        self.body.addWidget(warning_label(
            f"<b>All data on {part.display_name()} ({format_size(size)}) will be erased.</b>"))
        self.fs.currentIndexChanged.connect(self._fs_changed)
        self._fs_changed()
        self.resize(480, self.sizeHint().height())

    def _fs_changed(self, *_):
        fs = self.fs.fs()
        self.label.setEnabled(bool(fs) and fs.id != "cleared")
        if fs:
            self.label.setMaxLength(max(1, fs.max_label))
        _show_fs_warning(self.fs_warning, fs)
        self.crypt.setEnabled(_can_encrypt(fs))
        if not self.crypt.isEnabled():
            self.crypt.encrypt.setChecked(False)

    def validate(self):
        err = self.crypt.validate()
        if err:
            return err
        fs = self.fs.fs()
        if fs is None:
            return "Choose a filesystem"
        lab = fs.normalize_label(self.label.text())
        if lab and fs.id != "cleared":
            return fs.validate_label(lab)
        return None

    def operation(self) -> FormatPartition:
        return FormatPartition(self.disk.path, start=self.part.start, fstype=self.fs.fs_id(),
                               label=self.label.text().strip() if self.label.isEnabled() else "",
                               encrypt=self.crypt.enabled(), passphrase=self.crypt.passphrase())


GPT_ATTRS = [
    ("RequiredPartition", "Required by the platform (do not delete)"),
    ("NoBlockIOProtocol", "No EFI block I/O protocol"),
    ("LegacyBIOSBootable", "Legacy BIOS bootable"),
    ("GUID:60", "Read-only (Microsoft basic data)"),
    ("GUID:62", "Hidden (Microsoft basic data)"),
    ("GUID:63", "Do not automount (Microsoft basic data)"),
]


class PropertiesDialog(Dialog):
    """Details of a partition, plus editing of label, name, type and flags."""

    def __init__(self, parent, disk: Disk, part: Partition, editable: bool = True):
        super().__init__(parent, "Partition Properties", part.display_name(), ok_text="Apply" if editable else "Close")
        self.disk = disk
        self.part = part
        self.editable = editable
        tabs = QtWidgets.QTabWidget()
        self.body.addWidget(tabs)
        ss = disk.sector_size
        used = ""
        u, s = widgets.usage_of(part)
        if u is not None and s:
            used = f"{format_size(u)} of {format_size(s)} ({u * 100 // s}%)"
        rows = [
            ("Device", part.path or "(not created yet)"),
            ("Filesystem", widgets.part_fs_text(part) + (f" (version {part.fsversion})" if part.fsversion else "")),
            ("Label", part.label),
            ("Filesystem UUID", part.fsuuid),
            ("Usage", used),
            ("Mounted at", ", ".join(part.all_mountpoints()) or ("swap (active)" if part.is_swap_active else "")),
            ("Size", f"{format_size(part.size * ss)} ({part.size:,} sectors)"),
            ("First sector", f"{part.start:,}"),
            ("Last sector", f"{part.end:,}"),
            ("Alignment", "1 MiB aligned" if part.start % disk.alignment == 0 else "Not aligned to 1 MiB"),
            ("Partition number", str(part.number)),
            ("Partition type", f"{parttypes.type_name(part.type_id, disk.table)}  ({part.type_id})"),
            ("Partition UUID", part.uuid),
        ]
        if disk.table == "gpt":
            rows.append(("Partition name", part.name))
            rows.append(("Attributes", ", ".join(part.attrs)))
        else:
            rows.append(("Kind", part.kind.capitalize()))
            rows.append(("Bootable (active)", "Yes" if part.bootable else "No"))
        for h in part.holders:
            rows.append(("Used by", f"{h.get('name')} ({h.get('type')}"
                                    + (f", {filesystems.display_name(h.get('fstype', ''))}" if h.get("fstype") else "")
                                    + ")"))
        overview = QtWidgets.QScrollArea()
        overview.setWidgetResizable(True)
        overview.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        inner = info_grid(rows)
        inner.setContentsMargins(8, 8, 8, 8)
        overview.setWidget(inner)
        tabs.addTab(overview, "Overview")

        edit = QtWidgets.QWidget()
        form = QtWidgets.QFormLayout(edit)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        fs = filesystems.lookup(part.fstype, part.fsversion)
        self.fs = fs
        self.label = QtWidgets.QLineEdit(part.label)
        can_label = bool(fs and fs.implemented("label") and fs.id != "cleared")
        self.label.setEnabled(can_label and editable and fs.supports("label") and
                              (fs.label_online or not part.busy))
        if fs and can_label:
            self.label.setMaxLength(fs.max_label)
        if not self.label.isEnabled():
            self.label.setToolTip("The label of this filesystem cannot be changed right now")
        form.addRow("Filesystem label:", self.label)
        self.type = QtWidgets.QComboBox()
        found = False
        for pt in parttypes.choices(disk.table or "gpt"):
            tid = pt.gpt if disk.table == "gpt" else pt.mbr
            self.type.addItem(f"{pt.name}", tid)
            if tid == part.type_id:
                self.type.setCurrentIndex(self.type.count() - 1)
                found = True
        if not found:
            self.type.insertItem(0, f"{parttypes.type_name(part.type_id, disk.table)} (current)", part.type_id)
            self.type.setCurrentIndex(0)
        self.type.setEnabled(editable and not part.is_extended)
        form.addRow("Partition type:", self.type)
        self.name = None
        self.attrs: dict[str, QtWidgets.QCheckBox] = {}
        self.boot = None
        if disk.table == "gpt":
            self.name = QtWidgets.QLineEdit(part.name)
            self.name.setMaxLength(36)
            self.name.setEnabled(editable)
            form.addRow("Partition name:", self.name)
            box = QtWidgets.QGroupBox("Attributes")
            bl = QtWidgets.QVBoxLayout(box)
            for key, text in GPT_ATTRS:
                cb = QtWidgets.QCheckBox(text)
                cb.setChecked(key in part.attrs)
                cb.setEnabled(editable)
                bl.addWidget(cb)
                self.attrs[key] = cb
            self.extra_attrs = [a for a in part.attrs if a not in self.attrs]
            form.addRow(box)
        else:
            self.boot = QtWidgets.QCheckBox("Bootable (active) — used by legacy BIOS boot loaders")
            self.boot.setChecked(part.bootable)
            self.boot.setEnabled(editable and not part.is_logical and not part.is_extended)
            form.addRow("", self.boot)
        tabs.addTab(edit, "Edit")
        if not editable:
            self.buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Cancel).hide()
        self.resize(560, 520)

    def operations(self) -> list:
        if not self.editable:
            return []
        ops = []
        kw = {}
        tid = self.type.currentData()
        if self.type.isEnabled() and tid and tid != self.part.type_id:
            kw["type_id"] = tid
        if self.name is not None and self.name.text().strip() != self.part.name:
            kw["name"] = self.name.text().strip()
        if self.attrs:
            new_attrs = [k for k, cb in self.attrs.items() if cb.isChecked()] + self.extra_attrs
            if sorted(new_attrs) != sorted(self.part.attrs):
                kw["attrs"] = new_attrs
        if self.boot is not None and self.boot.isEnabled() and self.boot.isChecked() != self.part.bootable:
            kw["bootable"] = self.boot.isChecked()
        if kw:
            ops.append(SetPartitionProps(self.disk.path, start=self.part.start, **kw))
        if self.label.isEnabled() and self.label.text().strip() != self.part.label:
            ops.append(SetLabel(self.disk.path, start=self.part.start, label=self.label.text().strip()))
        return ops

    def validate(self):
        if self.fs and self.label.isEnabled() and self.label.text().strip():
            return self.fs.validate_label(self.fs.normalize_label(self.label.text()))
        if self.name is not None and any(c in self.name.text() for c in '"\\'):
            return "Partition names cannot contain quotes or backslashes"
        return None


class PasteDialog(Dialog):
    """Choose where the copy of a partition goes."""

    def __init__(self, parent, disk: Disk, free: FreeSpace, src_disk: Disk, src: Partition):
        super().__init__(parent, "Paste Partition", f"Paste a copy of {src.display_name()}", ok_text="Paste",
                         subtitle=f"The copy needs at least {format_size(src.size * src_disk.sector_size)}.")
        self.disk, self.free, self.src_disk, self.src = disk, free, src_disk, src
        ustart, usize = disk.usable_extent(free)
        fs = filesystems.lookup(src.fstype, src.fsversion)
        can_grow = fs is None or fs.supports("grow")
        min_size = src.size
        max_size = None if can_grow else src.size
        self.geo = _Geometry(disk, ustart, free.end, ustart, min(src.size, free.end - ustart + 1),
                             min_size=min_size, max_size=max_size,
                             color=widgets.part_color(src, self.palette()).name())
        self.body.addWidget(self.geo)
        self.new_uuid = QtWidgets.QCheckBox("Give the copy a new filesystem UUID (recommended)")
        self.new_uuid.setChecked(True)
        self.new_uuid.setEnabled(bool(fs and fs.supports("uuid")))
        if not self.new_uuid.isEnabled():
            self.new_uuid.setChecked(False)
        self.body.addWidget(self.new_uuid)
        if src.size > free.end - ustart + 1:
            self.show_error("There is not enough unallocated space here for the copy.")
            self.ok_button.setEnabled(False)
        self.resize(560, self.sizeHint().height())

    def operation(self) -> CopyPartition:
        return CopyPartition(self.src_disk.path, start=self.src.start, dest_disk=self.disk.path,
                             dest_start=self.geo.start, dest_size=self.geo.size,
                             new_uuid=self.new_uuid.isChecked())

