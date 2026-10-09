"""The main window."""

from __future__ import annotations

import os
import shutil

from .. import APP_NAME
from ..core import filesystems, parttypes, scanner
from ..core.model import Disk, FreeSpace, Partition, find_disk
from ..core.operations import (CheckFilesystem, ConvertTable, DeletePartition, NewUUID, Operation, OpError,
                               Plan, simulate)
from ..core.units import format_size
from . import icons, theme, widgets
from .dialogs import disk as disk_dialogs
from .dialogs import partition as part_dialogs
from .dialogs.common import ask_passphrase, confirm, message
from .dialogs.misc import ApplyConfirmDialog, SupportDialog, about
from .dialogs.progress import ProgressDialog
from .helper_client import HelperClient
from .qt import QtCore, QtGui, QtWidgets, Signal

Qt = QtCore.Qt
QPalette = QtGui.QPalette

ROLE_DISK = Qt.ItemDataRole.UserRole
ROLE_KEY = Qt.ItemDataRole.UserRole + 3
ROLE_PENDING = Qt.ItemDataRole.UserRole + 2


# -- background work -------------------------------------------------------------
class _Signals(QtCore.QObject):
    done = Signal(object)
    error = Signal(str)


class _Job(QtCore.QRunnable):
    def __init__(self, fn, signals):
        super().__init__()
        self.fn = fn
        self.signals = signals

    def run(self):
        try:
            res = self.fn()
        except Exception as e:  # noqa: BLE001
            self.signals.error.emit(str(e))
            return
        self.signals.done.emit(res)


# -- device list -------------------------------------------------------------------
class DeviceDelegate(QtWidgets.QStyledItemDelegate):
    def sizeHint(self, option, index):
        fm = option.fontMetrics
        return QtCore.QSize(220, fm.height() * 2 + 26)

    def paint(self, p, option, index):
        disk: Disk = index.data(ROLE_DISK)
        opt = QtWidgets.QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        opt.text = ""
        opt.icon = QtGui.QIcon()
        style = opt.widget.style() if opt.widget else QtWidgets.QApplication.style()
        style.drawControl(QtWidgets.QStyle.ControlElement.CE_ItemViewItem, opt, p, opt.widget)
        if disk is None:
            return
        selected = bool(opt.state & QtWidgets.QStyle.StateFlag.State_Selected)
        pal = opt.palette
        fg = pal.color(QPalette.ColorRole.HighlightedText if selected else QPalette.ColorRole.Text)
        r = opt.rect.adjusted(8, 6, -8, -6)
        p.save()
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        ic = icons.media_icon(disk.media_kind())
        ic.paint(p, QtCore.QRect(r.x(), r.y() + (r.height() - 32) // 2, 32, 32),
                 Qt.AlignmentFlag.AlignCenter,
                 QtGui.QIcon.Mode.Selected if selected else QtGui.QIcon.Mode.Normal)
        tx = r.x() + 42
        tw = r.right() - tx
        f = QtGui.QFont(opt.font)
        f.setWeight(QtGui.QFont.Weight.DemiBold)
        fm = QtGui.QFontMetrics(f)
        p.setFont(f)
        p.setPen(fg)
        pending = index.data(ROLE_PENDING)
        title = disk.title()
        p.drawText(QtCore.QPointF(tx, r.y() + fm.ascent()),
                   fm.elidedText(title, Qt.TextElideMode.ElideRight, tw - (14 if pending else 0)))
        if pending:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(pal.color(QPalette.ColorRole.Highlight) if not selected else fg)
            p.drawEllipse(QtCore.QPointF(r.right() - 4, r.y() + fm.ascent() / 2 + 2), 4, 4)
        f2 = QtGui.QFont(opt.font)
        f2.setPointSizeF(max(7.0, f2.pointSizeF() * 0.9))
        fm2 = QtGui.QFontMetrics(f2)
        p.setFont(f2)
        sub_color = QtGui.QColor(fg)
        sub_color.setAlphaF(0.72)
        p.setPen(sub_color)
        sub = f"{format_size(disk.size)} · {disk.name}"
        if disk.table:
            sub += f" · {parttypes.table_label(disk.table)}"
        y2 = r.y() + fm.height() + 2
        p.drawText(QtCore.QPointF(tx, y2 + fm2.ascent()), fm2.elidedText(sub, Qt.TextElideMode.ElideRight, tw))
        bar = QtCore.QRectF(tx, y2 + fm2.height() + 4, min(tw, 160), 4)
        ratio = disk.used_ratio() if disk.table else (1.0 if disk.fstype else 0.0)
        widgets.paint_usage_bar(p, bar, ratio, pal.color(QPalette.ColorRole.Highlight) if not selected
                                else fg, pal, warn_full=False)
        p.restore()


# -- main window -----------------------------------------------------------------
class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(icons.app_icon())
        self.settings = QtCore.QSettings("ManageDisks", "ManageDisks")
        self.real: list[Disk] = []
        self.preview: list[Disk] = []
        self.plan = Plan()
        self.helper = HelperClient(self)
        self.helper.ready.connect(self._helper_ready)
        self.helper.failed.connect(self._helper_failed)
        self.helper.stopped.connect(self._helper_stopped)
        self.disk_path: str | None = None
        self.key: tuple[str, int] | None = None
        self.clipboard: tuple[str, int] | None = None
        self.scanning = False
        self.modal_busy = False
        self._signals = []
        self._last_proc = ""
        self.show_loop = self.settings.value("show_loop", False, type=bool)
        self._build_actions()
        self._build_ui()
        self._restore_state()
        self._poll = QtCore.QTimer(self)
        self._poll.setInterval(2000)
        self._poll.timeout.connect(self._check_changes)
        self._poll.start()
        self.refresh()

    # ===== construction ===========================================================
    def _act(self, name, text, icon_name=None, shortcut=None, slot=None, tip=None):
        a = QtGui.QAction(text, self)
        if icon_name:
            a.setIcon(icons.icon(icon_name))
        if shortcut:
            if isinstance(shortcut, (list, tuple)):
                a.setShortcuts([QtGui.QKeySequence(s) for s in shortcut])
            else:
                a.setShortcut(QtGui.QKeySequence(shortcut))
        if slot:
            a.triggered.connect(lambda *_: slot())
        if tip:
            a.setToolTip(tip)
            a.setStatusTip(tip)
        self.addAction(a)
        setattr(self, "a_" + name, a)
        return a

    def _build_actions(self):
        A = self._act
        SK = QtGui.QKeySequence.StandardKey
        A("new", "New", "add", ["Ins", "Ctrl+N"], self.new_partition, "Create a partition in the selected unallocated space")
        A("delete", "Delete", "delete", SK.Delete, self.delete_partition, "Delete the selected partition")
        A("resize", "Resize/Move", "resize", "Ctrl+R", self.resize_partition, "Grow, shrink or move the selected partition")
        A("format", "Format", "format", "Ctrl+Shift+F", self.format_partition, "Format the selected partition")
        A("copy", "Copy", "copy", SK.Copy, self.copy_partition, "Copy the selected partition (paste it into unallocated space)")
        A("paste", "Paste", "paste", SK.Paste, self.paste_partition, "Paste the copied partition here")
        A("label", "Label && Flags…", "label", "F2", self.properties, "Change label, name, type and flags")
        A("uuid", "New UUID", "uuid", None, self.new_uuid, "Give the filesystem a new random UUID")
        A("check", "Check && Repair", "check", None, self.check_fs, "Check the filesystem for errors and repair them")
        A("mount", "Mount", "mount", None, self.mount, "Mount the filesystem")
        A("unmount", "Unmount", "unmount", None, self.unmount, "Unmount the filesystem")
        A("unlock", "Unlock…", "unlock", None, self.unlock_luks, "Unlock the encrypted partition")
        A("lock", "Lock", "lock", None, self.lock_luks, "Lock the encrypted partition")
        A("swapon", "Activate Swap", "swap", None, lambda: self._simple_helper("swapon"), "Use this swap partition")
        A("swapoff", "Deactivate Swap", "swap", None, lambda: self._simple_helper("swapoff"), "Stop using this swap partition")
        A("wipe_part", "Wipe…", "wipe", None, self.wipe_partition, "Erase the partition's contents")
        A("props", "Properties", "properties", "Alt+Return", self.properties, "Partition details")

        A("new_table", "New Partition Table…", "table", None, self.new_table, "Erase the disk and create an empty partition table")
        A("convert", "Convert to GPT", "convert", None, self.convert_table, "Switch between MBR and GPT keeping all data")
        A("wipe_disk", "Wipe Disk…", "wipe", None, self.wipe_disk, "Erase the whole disk")
        A("smart", "Health (S.M.A.R.T.)", "health", None, self.smart, "Show the drive's health and run self-tests")
        A("surface", "Surface Test", "surface", None, self.surface_test, "Read the whole disk to find bad sectors")
        A("clone", "Clone Disk…", "clone", None, self.clone_disk, "Copy this disk onto another disk")
        A("img_create", "Create Image…", "image-create", None, self.create_image, "Save the selected disk or partition to an image file")
        A("img_write", "Write Image…", "image-write", None, self.write_image, "Write an ISO/IMG file to the selected disk (bootable USB)")
        A("poweroff", "Safely Remove", "eject", None, self.power_off, "Power off a removable drive so it can be unplugged")
        A("disk_props", "Disk Properties", "properties", None, self.disk_properties, "Details of the selected disk")

        A("undo", "Undo", "undo", SK.Undo, self.undo, "Undo the last pending operation")
        A("clear", "Clear All", "clear", None, self.clear_ops, "Discard all pending operations")
        A("apply", "Apply", "apply", "Ctrl+Return", self.apply, "Apply all pending operations")
        A("refresh", "Refresh", "refresh", SK.Refresh, self.refresh, "Rescan disks")
        A("unlock_admin", "Unlock", "unlock", None, self.unlock_admin, "Authenticate to make changes")
        A("support", "Filesystem Support", "support", None, lambda: SupportDialog(self).exec(),
          "Which filesystems and features are available")
        A("about", f"About {APP_NAME}", "info", None, lambda: about(self))
        A("quit", "Quit", None, SK.Quit, self.close)
        self.a_show_loop = QtGui.QAction("Show Loop Devices", self, checkable=True)
        self.a_show_loop.setChecked(self.show_loop)
        self.a_show_loop.toggled.connect(self._toggle_loop)
        self.scheme_group = QtGui.QActionGroup(self)
        self.scheme_actions = {}
        for key, text in (("system", "Follow System"), ("light", "Light"), ("dark", "Dark")):
            a = QtGui.QAction(text, self, checkable=True)
            a.setData(key)
            self.scheme_group.addAction(a)
            self.scheme_actions[key] = a
            a.triggered.connect(lambda *_, k=key: self._set_scheme(k))
        self.scheme_actions[self.settings.value("color_scheme", "system")].setChecked(True)

    def _build_ui(self):
        tb = QtWidgets.QToolBar("Main")
        tb.setObjectName("main-toolbar")
        tb.setMovable(False)
        tb.setFloatable(False)
        tb.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        tb.setIconSize(QtCore.QSize(20, 20))
        for a in (self.a_new, self.a_delete, self.a_resize, self.a_format, self.a_check, self.a_props):
            tb.addAction(a)
        spacer = QtWidgets.QWidget()
        spacer.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Preferred)
        tb.addWidget(spacer)
        tb.addAction(self.a_undo)
        self.apply_btn = QtWidgets.QToolButton()
        self.apply_btn.setDefaultAction(self.a_apply)
        self.apply_btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.apply_btn.setObjectName("apply-button")
        tb.addWidget(self.apply_btn)
        menu_btn = QtWidgets.QToolButton()
        menu_btn.setIcon(icons.icon("menu"))
        menu_btn.setToolTip("Main menu")
        menu_btn.setPopupMode(QtWidgets.QToolButton.ToolButtonPopupMode.InstantPopup)
        menu_btn.setMenu(self._main_menu())
        tb.addWidget(menu_btn)
        self.addToolBar(tb)
        self.toolbar = tb
        for a in (self.a_new, self.a_delete, self.a_resize, self.a_format, self.a_check, self.a_props):
            w = tb.widgetForAction(a)
            if w:
                w.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)

        split = QtWidgets.QSplitter(Qt.Orientation.Horizontal)
        split.setChildrenCollapsible(False)
        self.splitter = split
        # sidebar
        side = QtWidgets.QWidget()
        sl = QtWidgets.QVBoxLayout(side)
        sl.setContentsMargins(8, 8, 4, 8)
        sl.setSpacing(6)
        lab = QtWidgets.QLabel("Devices")
        f = lab.font()
        f.setWeight(QtGui.QFont.Weight.DemiBold)
        lab.setFont(f)
        lab.setContentsMargins(4, 0, 0, 0)
        sl.addWidget(lab)
        self.devices = QtWidgets.QListWidget()
        self.devices.setItemDelegate(DeviceDelegate(self.devices))
        self.devices.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        self.devices.setVerticalScrollMode(QtWidgets.QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.devices.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.devices.customContextMenuRequested.connect(self._device_menu)
        self.devices.currentItemChanged.connect(self._device_changed)
        self.devices.setAccessibleName("Devices")
        sl.addWidget(self.devices, 1)
        side.setMinimumWidth(220)
        split.addWidget(side)

        # main pane
        main = QtWidgets.QWidget()
        ml = QtWidgets.QVBoxLayout(main)
        ml.setContentsMargins(10, 8, 10, 8)
        ml.setSpacing(10)
        self.banner = widgets.Banner("You are viewing your disks in read-only mode. Unlock to make changes and "
                                     "to see details such as the usage of unmounted filesystems.",
                                     "info", "Unlock")
        self.banner.button.clicked.connect(self.unlock_admin)
        ml.addWidget(self.banner)

        header = QtWidgets.QHBoxLayout()
        header.setSpacing(12)
        self.disk_icon = QtWidgets.QLabel()
        header.addWidget(self.disk_icon, 0, Qt.AlignmentFlag.AlignTop)
        hv = QtWidgets.QVBoxLayout()
        hv.setSpacing(2)
        self.disk_title = QtWidgets.QLabel()
        tf = self.disk_title.font()
        tf.setPointSizeF(tf.pointSizeF() * 1.35)
        tf.setWeight(QtGui.QFont.Weight.DemiBold)
        self.disk_title.setFont(tf)
        self.disk_title.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.disk_sub = QtWidgets.QLabel()
        self.disk_sub.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.chips = QtWidgets.QHBoxLayout()
        self.chips.setSpacing(6)
        hv.addWidget(self.disk_title)
        hv.addWidget(self.disk_sub)
        hv.addLayout(self.chips)
        header.addLayout(hv, 1)
        self.health_btn = QtWidgets.QPushButton(icons.icon("health"), "Health")
        self.health_btn.clicked.connect(self.smart)
        self.disk_btn = QtWidgets.QPushButton(icons.icon("hdd"), "Disk")
        self.disk_menu = QtWidgets.QMenu(self)
        self.disk_menu.aboutToShow.connect(lambda: self._fill_disk_menu(self.disk_menu))
        self.disk_btn.setMenu(self.disk_menu)
        header.addWidget(self.health_btn, 0, Qt.AlignmentFlag.AlignTop)
        header.addWidget(self.disk_btn, 0, Qt.AlignmentFlag.AlignTop)
        ml.addLayout(header)

        self.map = widgets.DiskMap()
        self.map.selected.connect(self._map_selected)
        self.map.activated.connect(self._activate)
        self.map.contextRequested.connect(lambda item, pos: self._item_menu(item, pos))
        ml.addWidget(self.map)

        self.tree = QtWidgets.QTreeWidget()
        self.tree.setColumnCount(7)
        self.tree.setHeaderLabels(["Partition", "Label / Name", "Filesystem", "Mount point", "Size", "Used", "Flags"])
        self.tree.setAlternatingRowColors(True)
        self.tree.setUniformRowHeights(True)
        self.tree.setRootIsDecorated(True)
        self.tree.setItemsExpandable(True)
        self.tree.setAllColumnsShowFocus(True)
        self.tree.setItemDelegateForColumn(5, widgets.UsageDelegate(self.tree))
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._tree_menu)
        self.tree.currentItemChanged.connect(self._tree_changed)
        self.tree.itemDoubleClicked.connect(lambda it, col: self._activate(self._item_for_key(it.data(0, ROLE_KEY))))
        hdr = self.tree.header()
        hdr.setStretchLastSection(True)
        hdr.setSectionResizeMode(QtWidgets.QHeaderView.ResizeMode.Interactive)
        self.tree.setAccessibleName("Partitions")
        ml.addWidget(self.tree, 1)

        # pending operations panel
        self.pending = QtWidgets.QFrame()
        self.pending.setObjectName("pending")
        self.pending.setFrameShape(QtWidgets.QFrame.Shape.StyledPanel)
        pl = QtWidgets.QVBoxLayout(self.pending)
        pl.setContentsMargins(10, 8, 10, 8)
        ph = QtWidgets.QHBoxLayout()
        self.pending_title = QtWidgets.QLabel()
        pf = self.pending_title.font()
        pf.setWeight(QtGui.QFont.Weight.DemiBold)
        self.pending_title.setFont(pf)
        ph.addWidget(self.pending_title, 1)
        for a in (self.a_undo, self.a_clear):
            b = QtWidgets.QToolButton()
            b.setDefaultAction(a)
            b.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
            b.setAutoRaise(True)
            ph.addWidget(b)
        self.pending_apply = QtWidgets.QPushButton(icons.icon("apply"), "Apply")
        self.pending_apply.clicked.connect(self.apply)
        self.pending_apply.setDefault(True)
        ph.addWidget(self.pending_apply)
        pl.addLayout(ph)
        self.pending_list = QtWidgets.QListWidget()
        self.pending_list.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        self.pending_list.setMaximumHeight(120)
        self.pending_list.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.NoSelection)
        pl.addWidget(self.pending_list)
        ml.addWidget(self.pending)

        self.empty = QtWidgets.QLabel("No disks found")
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.stack = QtWidgets.QStackedWidget()
        self.stack.addWidget(main)
        self.stack.addWidget(self.empty)
        split.addWidget(self.stack)
        split.setStretchFactor(1, 1)
        split.setSizes([260, 900])
        self.setCentralWidget(split)

        sb = self.statusBar()
        self.priv_label = QtWidgets.QLabel()
        sb.addPermanentWidget(self.priv_label)
        self._style_apply_button()
        self._update_priv()
        self._update_pending()

    def _main_menu(self) -> QtWidgets.QMenu:
        m = QtWidgets.QMenu(self)
        m.addAction(self.a_refresh)
        m.addAction(self.a_unlock_admin)
        m.addSeparator()
        view = m.addMenu("View")
        view.addAction(self.a_show_loop)
        scheme = view.addMenu("Color Scheme")
        for a in self.scheme_actions.values():
            scheme.addAction(a)
        m.addAction(self.a_support)
        m.addSeparator()
        m.addAction(self.a_about)
        m.addAction(self.a_quit)
        return m

    def _style_apply_button(self):
        # The one accent-colored control: Apply.
        self.apply_btn.setStyleSheet(
            "QToolButton#apply-button { background: palette(highlight); color: palette(highlighted-text);"
            " border-radius: 5px; padding: 4px 12px; font-weight: 600; }"
            "QToolButton#apply-button:disabled { background: transparent; color: palette(mid);"
            " font-weight: normal; }")

    def _restore_state(self):
        geo = self.settings.value("geometry")
        if geo is not None:
            self.restoreGeometry(geo)
        else:
            self.resize(1180, 740)
        st = self.settings.value("splitter")
        if st is not None:
            self.splitter.restoreState(st)
        hs = self.settings.value("tree_header")
        if hs is not None:
            self.tree.header().restoreState(hs)
        else:
            for i, w in enumerate((170, 150, 120, 150, 90, 170, 120)):
                self.tree.setColumnWidth(i, w)

    def closeEvent(self, e):
        if self.plan.ops and not confirm(self, "Discard Pending Operations",
                                         f"There are {len(self.plan.ops)} pending operations that have not been "
                                         "applied. Quit anyway?", "Quit", destructive=True):
            e.ignore()
            return
        self.settings.setValue("geometry", self.saveGeometry())
        self.settings.setValue("splitter", self.splitter.saveState())
        self.settings.setValue("tree_header", self.tree.header().saveState())
        self.helper.stop()
        super().closeEvent(e)

    def changeEvent(self, e):
        if e.type() in (QtCore.QEvent.Type.PaletteChange, QtCore.QEvent.Type.StyleChange):
            icons.clear_cache()
            QtCore.QTimer.singleShot(0, self._retheme)
        super().changeEvent(e)

    def _retheme(self):
        self._populate(keep=True)

    # ===== privilege ==============================================================
    def unlock_admin(self):
        if self.helper.is_ready:
            return
        self.statusBar().showMessage("Waiting for authentication…")
        self.helper.start()

    def _helper_ready(self):
        self.banner.hide()
        self._update_priv()
        self.statusBar().showMessage("Administrator mode", 3000)
        self.refresh()

    def _helper_failed(self, text):
        self.banner.set_kind("error")
        self.banner.label.setText(text)
        self.banner.button.setText("Try Again")
        self.banner.show()
        self._update_priv()
        self.statusBar().showMessage(text, 6000)

    def _helper_stopped(self):
        self.banner.set_kind("warning")
        self.banner.label.setText("The administrator helper stopped. Unlock again to make changes.")
        self.banner.button.setText("Unlock")
        self.banner.show()
        self._update_priv()

    def _update_priv(self):
        if self.helper.is_ready:
            self.priv_label.setText("Administrator")
            self.a_unlock_admin.setEnabled(False)
            self.banner.hide()
        else:
            self.priv_label.setText("Read-only")
            self.a_unlock_admin.setEnabled(True)
            if os.geteuid() == 0:
                self.banner.hide()

    # ===== scanning ===============================================================
    def _toggle_loop(self, on):
        self.show_loop = on
        self.settings.setValue("show_loop", on)
        self.refresh()

    def _set_scheme(self, key):
        self.settings.setValue("color_scheme", key)
        theme.set_override(key)
        theme.apply(QtWidgets.QApplication.instance())

    def refresh(self):
        if self.scanning:
            return
        self.scanning = True
        self.statusBar().showMessage("Scanning disks…")
        if self.helper.is_ready:
            self.helper.call("scan", {"include_loop": self.show_loop},
                             on_result=lambda res: self._scanned([Disk.from_dict(d) for d in res]),
                             on_error=self._scan_failed)
        else:
            sig = _Signals()
            sig.done.connect(self._scanned)
            sig.error.connect(self._scan_failed)
            self._signals.append(sig)
            include = self.show_loop
            QtCore.QThreadPool.globalInstance().start(_Job(lambda: scanner.scan(include_loop=include), sig))

    def _scan_failed(self, err):
        self.scanning = False
        self.statusBar().showMessage(f"Scanning failed: {err}", 8000)

    def _scanned(self, disks: list[Disk]):
        self.scanning = False
        self._signals = [s for s in self._signals if s is not self.sender()]
        self._last_proc = self._proc_fingerprint()
        self.real = disks
        dropped = self.plan.prune(self.real)
        if dropped:
            message(self, "Pending Operations Removed",
                    f"{len(dropped)} pending operation(s) no longer apply because the disks changed and were "
                    "removed:", "warning", "\n".join(o.describe() for o in dropped))
        self._recompute()
        self.statusBar().showMessage(f"{len(disks)} disk{'s' if len(disks) != 1 else ''} found", 3000)

    def _proc_fingerprint(self) -> str:
        out = []
        for path in ("/proc/partitions", "/proc/self/mountinfo", "/proc/swaps"):
            try:
                with open(path, encoding="utf-8", errors="replace") as f:
                    out.append(f.read())
            except OSError:
                pass
        return "\n".join(out)

    def _check_changes(self):
        if self.scanning or self.modal_busy or QtWidgets.QApplication.activeModalWidget() is not None:
            return
        fp = self._proc_fingerprint()
        if fp != self._last_proc:
            self._last_proc = fp
            self.refresh()

    # ===== model / view sync ======================================================
    def _recompute(self):
        try:
            self.preview = simulate(self.real, self.plan.ops)
        except OpError as e:
            # Should not happen (ops are validated when queued); recover by dropping the queue.
            self.plan.clear()
            self.preview = [d.clone() for d in self.real]
            message(self, "Pending Operations Discarded", str(e), "warning")
        self._populate(keep=True)
        self._update_pending()

    def current_disk(self) -> Disk | None:
        return find_disk(self.preview, self.disk_path) if self.disk_path else None

    def real_disk(self) -> Disk | None:
        return find_disk(self.real, self.disk_path) if self.disk_path else None

    def current_item(self):
        d = self.current_disk()
        if d is None or self.key is None:
            return None
        return self._item_for_key(self.key)

    def _item_for_key(self, key):
        d = self.current_disk()
        if d is None or key is None:
            return None
        kind, start = key
        if kind == "p":
            return d.find(start)
        for f in d.free_spaces():
            if f.start == start:
                return f
        return None

    def _populate(self, keep=True):
        pending_disks = {p for o in self.plan.ops for p in o.disks()}
        self.devices.blockSignals(True)
        cur = self.disk_path
        self.devices.clear()
        sel_row = 0
        for i, d in enumerate(self.preview):
            it = QtWidgets.QListWidgetItem()
            it.setData(ROLE_DISK, d)
            it.setData(ROLE_PENDING, d.path in pending_disks)
            it.setToolTip(f"{d.title()}\n{d.path}\n{format_size(d.size)}")
            self.devices.addItem(it)
            if d.path == cur:
                sel_row = i
        if self.preview:
            self.devices.setCurrentRow(sel_row)
            self.disk_path = self.preview[sel_row].path
        else:
            self.disk_path = None
        self.devices.blockSignals(False)
        self.stack.setCurrentIndex(0 if self.preview else 1)
        self._show_disk(keep=keep)

    def _device_changed(self, cur, prev):
        if cur is None:
            return
        d = cur.data(ROLE_DISK)
        if d is not None and d.path != self.disk_path:
            self.disk_path = d.path
            self.key = None
            self._show_disk(keep=False)

    def _show_disk(self, keep=True):
        d = self.current_disk()
        while self.chips.count():
            it = self.chips.takeAt(0)
            if it.widget():
                it.widget().hide()
                it.widget().deleteLater()
        if d is None:
            self.map.set_disk(None)
            self.tree.clear()
            self._update_actions()
            return
        self.disk_icon.setPixmap(icons.media_icon(d.media_kind()).pixmap(48, 48))
        self.disk_title.setText(d.title())
        bits = [d.path, format_size(d.size)]
        if d.serial:
            bits.append(f"S/N {d.serial}")
        if d.backing_file:
            bits.append(d.backing_file)
        self.disk_sub.setText(" · ".join(bits))
        chips = [(parttypes.table_label(d.table), None)]
        if d.kind == "loop":
            chips.append(("Loop device", None))
        else:
            if d.transport:
                chips.append((d.transport.upper(), None))
            chips.append(("HDD" if d.rotational else "SSD", None))
        if d.removable:
            chips.append(("Removable", "#3584e4"))
        if d.read_only:
            chips.append(("Read-only", "#e5a50a"))
        if d.busy:
            chips.append(("In use", "#2ec27e"))
        free = d.free_bytes()
        if free:
            chips.append((f"{format_size(free)} unallocated", None))
        for text, color in chips:
            self.chips.addWidget(widgets.Chip(text, color))
        self.chips.addStretch(1)
        if not keep or self._item_for_key(self.key) is None:
            first = next(iter(d.segments()), None)
            self.key = widgets.seg_key(first)
        self.map.set_disk(d, self.key)
        self._fill_tree(d)
        self._update_actions()

    def _fill_tree(self, d: Disk):
        self.tree.blockSignals(True)
        self.tree.clear()
        pal = self.palette()
        dim = pal.color(QPalette.ColorRole.PlaceholderText) if hasattr(QPalette.ColorRole, "PlaceholderText") \
            else pal.color(QPalette.ColorRole.Mid)
        hl = pal.color(QPalette.ColorRole.Highlight)
        sel_item = None
        ext_item = None

        def make(item) -> QtWidgets.QTreeWidgetItem:
            if isinstance(item, Partition):
                p = item
                used, size = widgets.usage_of(p)
                cols = [widgets.part_title(p), p.label or p.name, widgets.part_fs_text(p),
                        ", ".join(p.all_mountpoints()) or ("[swap]" if p.is_swap_active else ""),
                        format_size(p.size * d.sector_size),
                        f"{format_size(used)} ({used * 100 // size}%)" if used is not None and size else "",
                        self._flags_text(d, p)]
                ti = QtWidgets.QTreeWidgetItem(cols)
                color = widgets.part_color(p, pal)
                ti.setIcon(2, widgets.color_swatch(color))
                if used is not None and size:
                    ti.setData(5, widgets.UsageDelegate.ROLE, (used, size, color.name()))
                if p.pending:
                    f = ti.font(0)
                    f.setItalic(True)
                    for c in range(7):
                        ti.setFont(c, f)
                    ti.setForeground(0, QtGui.QBrush(hl))
                    ti.setToolTip(0, "Pending: " + ", ".join(p.pending))
                if p.all_mountpoints() or p.is_swap_active:
                    ti.setIcon(0, icons.icon("mount", prefer_builtin=True))
                elif p.fstype == "crypto_LUKS":
                    ti.setIcon(0, icons.icon("unlock" if p.holders and not p.holders[0].get("virtual")
                                             else "lock", prefer_builtin=True))
            else:
                f_ = item
                ti = QtWidgets.QTreeWidgetItem(["Unallocated", "", "", "", format_size(f_.size * d.sector_size),
                                                "", ""])
                for c in range(7):
                    ti.setForeground(c, QtGui.QBrush(dim))
                ti.setIcon(2, widgets.color_swatch(theme.fs_color(widgets.UNALLOCATED_COLOR, pal)))
            for c in (4,):
                ti.setTextAlignment(c, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            ti.setData(0, ROLE_KEY, widgets.seg_key(item))
            return ti

        if not d.table:
            if d.fstype:
                ti = QtWidgets.QTreeWidgetItem([d.name, d.label, filesystems.display_name(d.fstype),
                                                ", ".join(d.mountpoints), format_size(d.size), "", "whole disk"])
                self.tree.addTopLevelItem(ti)
            else:
                ti = QtWidgets.QTreeWidgetItem(["No partition table", "", "", "", format_size(d.size), "", ""])
                ti.setForeground(0, QtGui.QBrush(dim))
                self.tree.addTopLevelItem(ti)
        else:
            for seg in d.segments():
                ti = make(seg)
                self.tree.addTopLevelItem(ti)
                if widgets.seg_key(seg) == self.key:
                    sel_item = ti
                if isinstance(seg, Partition) and seg.is_extended:
                    ext_item = ti
                    for child in d.segments_in_extended():
                        ci = make(child)
                        ti.addChild(ci)
                        if widgets.seg_key(child) == self.key:
                            sel_item = ci
            if ext_item:
                ext_item.setExpanded(True)
        if sel_item is not None:
            self.tree.setCurrentItem(sel_item)
        self.tree.blockSignals(False)

    def _flags_text(self, d: Disk, p: Partition) -> str:
        flags = []
        tname = parttypes.type_name(p.type_id, d.table)
        if d.table == "gpt":
            special = {"EFI System": "esp", "BIOS boot": "bios_grub", "Microsoft reserved": "msftres",
                       "Linux swap": "swap", "Linux LVM": "lvm", "Linux RAID": "raid",
                       "Windows recovery environment": "recovery", "Linux extended boot (/boot)": "boot"}
            if tname in special:
                flags.append(special[tname])
            if "LegacyBIOSBootable" in p.attrs:
                flags.append("legacy_boot")
            if "GUID:62" in p.attrs:
                flags.append("hidden")
        else:
            if p.bootable:
                flags.append("boot")
            if p.is_logical:
                flags.append("logical")
            if p.type_id in ("82",):
                flags.append("swap")
            if p.type_id == "8e":
                flags.append("lvm")
            if p.type_id == "fd":
                flags.append("raid")
            if p.type_id == "ef":
                flags.append("esp")
        return ", ".join(flags)

    def _map_selected(self, item):
        self.key = widgets.seg_key(item)
        self._select_tree_key(self.key)
        self._update_actions()

    def _select_tree_key(self, key):
        self.tree.blockSignals(True)
        it = QtWidgets.QTreeWidgetItemIterator(self.tree)
        while it.value():
            ti = it.value()
            if ti.data(0, ROLE_KEY) == key:
                self.tree.setCurrentItem(ti)
                break
            it += 1
        self.tree.blockSignals(False)

    def _tree_changed(self, cur, prev):
        if cur is None:
            return
        key = cur.data(0, ROLE_KEY)
        if key is not None:
            key = tuple(key)
        self.key = key
        self.map.select_key(key)
        self._update_actions()

    def _activate(self, item):
        if isinstance(item, FreeSpace):
            self.new_partition()
        elif isinstance(item, Partition):
            self.properties()

    # ===== action state ===========================================================
    def _update_actions(self):
        d = self.current_disk()
        item = self.current_item()
        part = item if isinstance(item, Partition) else None
        free = item if isinstance(item, FreeSpace) else None
        fs = filesystems.lookup(part.fstype, part.fsversion) if part else None
        pending_here = bool(d and any(d.path in o.disks() for o in self.plan.ops))
        real_part = None
        if part and not part.pending and not pending_here:
            rd = self.real_disk()
            real_part = rd.find(part.start) if rd else None
        ok_table = bool(d and d.table and not d.read_only)

        def en(action, on, why=None):
            action.setEnabled(bool(on))

        en(self.a_new, ok_table and free is not None)
        en(self.a_delete, ok_table and part is not None and not part.busy and
           not (part.is_extended and d.logicals()))
        en(self.a_resize, ok_table and part is not None and (not part.busy or (fs and (fs.grow_online or fs.shrink_online))))
        en(self.a_format, ok_table and part is not None and not part.is_extended and not part.busy)
        en(self.a_copy, part is not None and not part.is_extended and not part.busy and bool(part.fstype))
        en(self.a_paste, ok_table and free is not None and self.clipboard is not None)
        en(self.a_label, part is not None)
        en(self.a_props, part is not None)
        en(self.a_uuid, part is not None and fs is not None and fs.implemented("uuid") and not part.busy)
        en(self.a_check, part is not None and fs is not None and fs.implemented("check") and not part.is_new
           and (not part.busy or fs.id == "btrfs"))
        en(self.a_wipe_part, part is not None and not part.busy and not part.is_extended)
        mountable = real_part is not None and fs is not None and fs.id not in ("linux-swap", "luks", "cleared")
        inner_mountable = real_part is not None and real_part.fstype == "crypto_LUKS" and any(
            h.get("fstype") for h in real_part.holders)
        en(self.a_mount, (mountable and not real_part.is_mounted) or
           (inner_mountable and not any(h.get("mountpoints") for h in real_part.holders)))
        en(self.a_unmount, real_part is not None and bool(real_part.all_mountpoints()))
        en(self.a_unlock, real_part is not None and real_part.fstype == "crypto_LUKS" and not real_part.holders)
        en(self.a_lock, real_part is not None and real_part.fstype == "crypto_LUKS" and bool(real_part.holders)
           and not real_part.all_mountpoints())
        en(self.a_swapon, real_part is not None and real_part.fstype == "swap" and not real_part.is_swap_active)
        en(self.a_swapoff, real_part is not None and real_part.is_swap_active)

        whole_free = d is not None and not d.busy and not d.read_only
        en(self.a_new_table, whole_free)
        en(self.a_convert, d is not None and d.table is not None and not d.busy and not pending_here)
        if d is not None and d.table:
            self.a_convert.setText("Convert to MBR" if d.table == "gpt" else "Convert to GPT")
        en(self.a_wipe_disk, whole_free)
        en(self.a_smart, d is not None and d.kind == "disk")
        self.health_btn.setEnabled(d is not None and d.kind == "disk")
        en(self.a_surface, d is not None and not pending_here)
        en(self.a_clone, d is not None and not d.busy and not pending_here)
        en(self.a_img_create, d is not None and not pending_here)
        en(self.a_img_write, whole_free and not pending_here)
        en(self.a_poweroff, d is not None and d.removable and not d.busy)
        en(self.a_disk_props, d is not None)
        en(self.a_undo, bool(self.plan.ops))
        en(self.a_clear, bool(self.plan.ops))
        en(self.a_apply, bool(self.plan.ops))
        self.pending_apply.setEnabled(bool(self.plan.ops))

    def _update_pending(self):
        n = len(self.plan.ops)
        self.pending.setVisible(n > 0)
        self.pending_title.setText(f"{n} pending operation{'s' if n != 1 else ''}")
        self.pending_list.clear()
        for i, op in enumerate(self.plan.ops, 1):
            it = QtWidgets.QListWidgetItem(icons.icon("delete" if op.destructive else "apply", prefer_builtin=True),
                                           f"{i}. {op.describe()}")
            self.pending_list.addItem(it)
        self.pending_list.scrollToBottom()
        self.a_apply.setText(f"Apply {n}" if n else "Apply")
        self.pending_apply.setText(f"Apply {n} Operation{'s' if n != 1 else ''}")
        self._update_actions()

    # ===== menus ==================================================================
    def _fill_disk_menu(self, m: QtWidgets.QMenu):
        m.clear()
        for a in (self.a_new_table, self.a_convert, self.a_wipe_disk, None, self.a_smart, self.a_surface, None,
                  self.a_clone, self.a_img_create, self.a_img_write, None, self.a_poweroff, self.a_disk_props):
            if a is None:
                m.addSeparator()
            else:
                m.addAction(a)

    def _device_menu(self, pos):
        it = self.devices.itemAt(pos)
        if it is None:
            return
        self.devices.setCurrentItem(it)
        m = QtWidgets.QMenu(self)
        self._fill_disk_menu(m)
        m.exec(self.devices.viewport().mapToGlobal(pos))

    def _tree_menu(self, pos):
        it = self.tree.itemAt(pos)
        item = self._item_for_key(tuple(it.data(0, ROLE_KEY))) if it and it.data(0, ROLE_KEY) else None
        self._item_menu(item, self.tree.viewport().mapToGlobal(pos))

    def _item_menu(self, item, global_pos):
        m = QtWidgets.QMenu(self)
        if isinstance(item, FreeSpace):
            for a in (self.a_new, self.a_paste):
                m.addAction(a)
        elif isinstance(item, Partition):
            for a in (self.a_new, self.a_delete, self.a_resize, self.a_format, None, self.a_copy, self.a_paste,
                      None, self.a_mount, self.a_unmount, self.a_unlock, self.a_lock, self.a_swapon, self.a_swapoff,
                      None, self.a_check, self.a_uuid, self.a_wipe_part, self.a_img_create, None, self.a_props):
                if a is None:
                    m.addSeparator()
                elif a.isEnabled() or a in (self.a_delete, self.a_resize, self.a_format, self.a_props):
                    m.addAction(a)
        else:
            self._fill_disk_menu(m)
        m.exec(global_pos)

    # ===== queuing operations =====================================================
    def queue(self, op: Operation | list[Operation]) -> bool:
        ops = op if isinstance(op, list) else [op]
        added = 0
        for o in ops:
            try:
                self.plan.add(self.real, o)
                added += 1
            except OpError as e:
                message(self, "Operation Not Possible", str(e), "warning")
                break
        if added:
            self._recompute()
            warns = [w for o in ops[:added] for w in o.warnings(self.preview)]
            if warns:
                self.statusBar().showMessage(warns[0], 8000)
        return added == len(ops)

    def _sel_part(self) -> Partition | None:
        it = self.current_item()
        return it if isinstance(it, Partition) else None

    def new_partition(self):
        d, item = self.current_disk(), self.current_item()
        if d is None:
            return
        if not d.table:
            if confirm(self, "No Partition Table", f"{d.path} has no partition table yet. Create one now?",
                       "Create Partition Table"):
                self.new_table()
            return
        if not isinstance(item, FreeSpace):
            frees = d.free_spaces()
            if not frees:
                message(self, "No Free Space", "There is no unallocated space on this disk. Shrink or delete a "
                                               "partition first.")
                return
            item = max(frees, key=lambda f: f.size)
        dlg = part_dialogs.CreatePartitionDialog(self, d, item)
        if dlg.exec() == QtWidgets.QDialog.DialogCode.Accepted:
            op = dlg.operation()
            if self.queue(op):
                self.key = ("p", op.start)
                self._show_disk()

    def delete_partition(self):
        d, p = self.current_disk(), self._sel_part()
        if d is None or p is None:
            return
        if p.is_new:
            # Deleting a partition that only exists in the queue: just undo it.
            pass
        self.queue(DeletePartition(d.path, start=p.start))

    def resize_partition(self):
        d, p = self.current_disk(), self._sel_part()
        if d is None or p is None:
            return
        req = None
        rp = self.real_disk().find(p.start) if self.real_disk() and not p.is_new else None
        if rp is not None and rp.path and rp.fstype and self.helper.is_ready and "format" not in p.pending:
            def request_min_size(cb, rp=rp):
                self.helper.call("min_size", {"device": rp.path, "fstype": rp.fstype, "fsversion": rp.fsversion,
                                              "mountpoint": next(iter(rp.all_mountpoints()), None)},
                                 on_result=cb, on_error=lambda e: cb(None))
            req = request_min_size
        dlg = part_dialogs.ResizeDialog(self, d, p, req)
        if dlg.exec() == QtWidgets.QDialog.DialogCode.Accepted:
            op = dlg.operation()
            if op and self.queue(op):
                self.key = ("p", op.new_start)
                self._show_disk()

    def format_partition(self):
        d, p = self.current_disk(), self._sel_part()
        if d is None or p is None:
            return
        dlg = part_dialogs.FormatDialog(self, d, p)
        if dlg.exec() == QtWidgets.QDialog.DialogCode.Accepted:
            self.queue(dlg.operation())

    def properties(self):
        d, p = self.current_disk(), self._sel_part()
        if d is None or p is None:
            return
        dlg = part_dialogs.PropertiesDialog(self, d, p, editable=bool(d.table) and not d.read_only)
        if dlg.exec() == QtWidgets.QDialog.DialogCode.Accepted:
            ops = dlg.operations()
            if ops:
                self.queue(ops)

    def new_uuid(self):
        d, p = self.current_disk(), self._sel_part()
        if d and p:
            self.queue(NewUUID(d.path, start=p.start))

    def check_fs(self):
        d, p = self.current_disk(), self._sel_part()
        if d and p:
            self.queue(CheckFilesystem(d.path, start=p.start, repair=True))

    def wipe_partition(self):
        d, p = self.current_disk(), self._sel_part()
        if d is None or p is None:
            return
        dlg = disk_dialogs.WipeDialog(self, d, p)
        if dlg.exec() == QtWidgets.QDialog.DialogCode.Accepted:
            self.queue(dlg.operation())

    def copy_partition(self):
        d, p = self.current_disk(), self._sel_part()
        if d is None or p is None:
            return
        self.clipboard = (d.path, p.start)
        self.statusBar().showMessage(f"Copied {p.display_name()}. Select unallocated space and choose Paste.", 6000)
        self._update_actions()

    def paste_partition(self):
        d, item = self.current_disk(), self.current_item()
        if d is None or not isinstance(item, FreeSpace) or self.clipboard is None:
            return
        sd = find_disk(self.preview, self.clipboard[0])
        src = sd.find(self.clipboard[1]) if sd else None
        if src is None:
            message(self, "Paste", "The copied partition no longer exists.", "warning")
            self.clipboard = None
            return
        dlg = part_dialogs.PasteDialog(self, d, item, sd, src)
        if dlg.exec() == QtWidgets.QDialog.DialogCode.Accepted:
            self.queue(dlg.operation())

    def new_table(self):
        d = self.current_disk()
        if d is None:
            return
        dlg = disk_dialogs.NewTableDialog(self, d)
        if dlg.exec() == QtWidgets.QDialog.DialogCode.Accepted:
            self.queue(dlg.operation())

    def convert_table(self):
        d = self.current_disk()
        if d is None or not d.table:
            return
        op = ConvertTable(d.path, to="dos" if d.table == "gpt" else "gpt")
        target = "MBR" if d.table == "gpt" else "GPT"
        if confirm(self, f"Convert to {target}", f"Convert {d.title()} to {target}?",
                   f"Convert to {target}", informative="\n\n".join(op.warnings(self.preview))):
            self.queue(op)

    def wipe_disk(self):
        d = self.current_disk()
        if d is None:
            return
        dlg = disk_dialogs.WipeDialog(self, d, None)
        if dlg.exec() == QtWidgets.QDialog.DialogCode.Accepted:
            self.queue(dlg.operation())

    def undo(self):
        if self.plan.undo():
            self._recompute()

    def clear_ops(self):
        if self.plan.ops and confirm(self, "Clear Pending Operations",
                                     f"Discard all {len(self.plan.ops)} pending operations?", "Discard"):
            self.plan.clear()
            self._recompute()

    # ===== applying ===============================================================
    def apply(self):
        if not self.plan.ops:
            return
        ops = list(self.plan.ops)
        warns = []
        for o in ops:
            for w in o.warnings(self.preview):
                if w not in warns:
                    warns.append(w)
        if ApplyConfirmDialog(self, ops, warns).exec() != QtWidgets.QDialog.DialogCode.Accepted:
            return
        payload = [o.to_dict() for o in ops]

        def start(on_event, on_result, on_error):
            return self.helper.call("apply", {"ops": payload}, on_event=on_event, on_result=on_result,
                                    on_error=on_error)
        self.modal_busy = True
        try:
            dlg = ProgressDialog(self, "Applying operations", [o.describe() for o in ops], start, self.helper.cancel)
            dlg.exec()
        finally:
            self.modal_busy = False
        if dlg.finished_ok is not None and (dlg.finished_ok or dlg.completed > 0 or self.helper.is_ready):
            self.plan.clear()
            self.clipboard = None
        self._recompute()
        self.refresh()

    # ===== immediate actions ======================================================
    def _run_task(self, title, items, method, params, cancellable=True, on_done=None):
        def start(on_event, on_result, on_error):
            return self.helper.call(method, params, on_event=on_event, on_result=on_result, on_error=on_error)
        self.modal_busy = True
        try:
            dlg = ProgressDialog(self, title, items, start, self.helper.cancel, cancellable=cancellable)
            dlg.exec()
        finally:
            self.modal_busy = False
        if on_done:
            on_done(dlg.finished_ok)
        self.refresh()

    def _call(self, method, params, done_text=None, refresh=True):
        self.statusBar().showMessage("Working…")

        def ok(res):
            if done_text:
                self.statusBar().showMessage(done_text.format(res=res), 5000)
            if refresh:
                self.refresh()

        def err(e):
            self.statusBar().clearMessage()
            message(self, "Action Failed", str(e), "error")
            if refresh:
                self.refresh()
        self.helper.call(method, params, on_result=ok, on_error=err)

    def _real_sel(self) -> Partition | None:
        p = self._sel_part()
        rd = self.real_disk()
        return rd.find(p.start) if (p and rd) else None

    def _udisks(self, args, fallback):
        """Try udisksctl first (mounts like the file manager does), then the helper."""
        if os.geteuid() == 0 or not shutil.which("udisksctl"):
            fallback()
            return
        proc = QtCore.QProcess(self)

        def finished(code, status=None):
            out = bytes(proc.readAllStandardOutput().data()).decode(errors="replace").strip()
            if code == 0:
                self.statusBar().showMessage(out, 5000)
                self.refresh()
            else:
                fallback()
            proc.deleteLater()
        proc.finished.connect(finished)
        proc.setProcessChannelMode(QtCore.QProcess.ProcessChannelMode.MergedChannels)
        proc.start("udisksctl", args)

    def _mount_target(self) -> tuple[str, str, str] | None:
        p = self._real_sel()
        if p is None:
            return None
        if p.fstype == "crypto_LUKS":
            for h in p.holders:
                if h.get("fstype"):
                    return h["path"], h["fstype"], h.get("label", "")
            return None
        return p.path, p.fstype, p.label

    def mount(self):
        t = self._mount_target()
        if not t:
            return
        dev, fstype, label = t
        opts = None
        if fstype in ("vfat", "exfat", "ntfs"):
            opts = f"uid={os.getuid()},gid={os.getgid()}"
        self._udisks(["mount", "-b", dev],
                     lambda: self._call("mount", {"device": dev, "options": opts, "label": label}, "Mounted at {res}"))

    def unmount(self):
        p = self._real_sel()
        if p is None:
            return
        devs = [p.path] if p.is_mounted else [h["path"] for h in p.holders if h.get("mountpoints")]
        for dev in devs:
            self._udisks(["unmount", "-b", dev], lambda dev=dev: self._call("unmount", {"device": dev}, "Unmounted"))

    def unlock_luks(self):
        p = self._real_sel()
        if p is None:
            return
        pw = ask_passphrase(self, "Unlock Encrypted Partition", f"Enter the passphrase for {p.path}")
        if pw:
            self._call("luks_open", {"device": p.path, "passphrase": pw}, "Unlocked as {res}")

    def lock_luks(self):
        p = self._real_sel()
        if p is not None:
            self._call("luks_close", {"device": p.path}, "Locked")

    def _simple_helper(self, method):
        p = self._real_sel()
        if p is not None:
            self._call(method, {"device": p.path}, "Done")

    def power_off(self):
        d = self.real_disk()
        if d is None:
            return
        self._udisks(["power-off", "-b", d.path],
                     lambda: self._call("power_off", {"device": d.path}, "You can now unplug the drive"))

    def smart(self):
        d = self.real_disk()
        if d is None:
            return

        def fetch(path, ok, err):
            self.helper.call("smart", {"device": path}, on_result=ok, on_error=err)

        def test(path, kind, ok, err):
            self.helper.call("smart_test", {"device": path, "kind": kind}, on_result=ok, on_error=err)
        disk_dialogs.SmartDialog(self, d, fetch, test).exec()

    def surface_test(self):
        d = self.real_disk()
        p = self._real_sel()
        target = p.path if p is not None else (d.path if d else None)
        if not target:
            return
        if not confirm(self, "Surface Test", f"Read every sector of {target} to look for unreadable areas? "
                                             "Nothing is written. This can take hours on large disks.", "Start"):
            return
        self._run_task("Surface test", [f"Read all of {target}"], "surface_test", {"device": target})

    def clone_disk(self):
        d = self.real_disk()
        if d is None:
            return
        dlg = disk_dialogs.DiskPicker(self, "Clone Disk", f"Clone {d.title()} to…", self.real, d)
        if dlg.exec() != QtWidgets.QDialog.DialogCode.Accepted or not dlg.target():
            return
        tgt = dlg.target()
        if not confirm(self, "Clone Disk", f"Overwrite everything on {tgt} with a copy of {d.path}?",
                       "Clone", destructive=True):
            return
        self._run_task("Cloning disk", [f"Clone {d.path} → {tgt}"], "clone_disk",
                       {"source": d.path, "target": tgt, "new_ids": dlg.new_ids.isChecked()})

    def create_image(self):
        d = self.real_disk()
        p = self._real_sel()
        src = p.path if p is not None else (d.path if d else None)
        if not src:
            return
        res = disk_dialogs.pick_image_to_save(self, os.path.basename(src) + ".img")
        if not res:
            return
        path, compress = res
        self._run_task("Creating image", [f"Save {src} to {os.path.basename(path)}"], "create_image",
                       {"device": src, "image": path, "compress": compress, "owner": [os.getuid(), os.getgid()]})

    def write_image(self):
        d = self.real_disk()
        if d is None:
            return
        path = disk_dialogs.pick_image_to_write(self)
        if not path:
            return
        if not confirm(self, "Write Image", f"Write {os.path.basename(path)} to {d.title()} ({d.path})?",
                       "Write", destructive=True,
                       informative=f"Everything on {d.path} ({format_size(d.size)}) will be overwritten."):
            return
        self._run_task("Writing image", [f"Write {os.path.basename(path)} → {d.path}"], "restore_image",
                       {"image": path, "device": d.path})

    def disk_properties(self):
        d = self.current_disk()
        if d is not None:
            disk_dialogs.disk_properties(self, d)
