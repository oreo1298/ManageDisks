"""CLI parsing, the diskpart-style shell and a GUI smoke test (offscreen)."""

import os

import pytest

from helpers import gpt_disk, mbr_disk
from managedisks.cli import common, main as cli
from managedisks.cli.shell import DiskShell
from managedisks.core.units import MiB

S = MiB // 512


def test_parser_has_all_commands():
    p = cli.build_parser()
    for cmd in ("list", "info", "support", "table", "create", "delete", "format", "resize", "label", "set",
                "uuid", "check", "copy", "wipe", "convert", "mount", "unmount", "open", "close", "smart",
                "surface", "image", "clone", "shell"):
        args = {"table": [cmd, "/dev/x", "gpt"], "convert": [cmd, "/dev/x", "gpt"], "label": [cmd, "/dev/x", "y"],
                "copy": [cmd, "/dev/x1", "/dev/y"], "clone": [cmd, "/dev/x", "/dev/y"],
                "image": [cmd, "create", "/dev/x", "f.img"], "format": [cmd, "/dev/x1", "-f", "ext4"]}.get(
            cmd, [cmd] + (["/dev/x"] if cmd not in ("list", "support", "shell") else []))
        assert p.parse_args(args).fn


def test_placement_defaults_to_whole_region():
    d = gpt_disk(parts=[(1, 512, "vfat")])
    free = common.pick_free(d, None, False)
    start, size = common.placement(d, free, None, None)
    assert start == 513 * S and size % d.alignment == 0
    start, size = common.placement(d, free, None, "1G")
    assert size == 1024 * S
    start, size = common.placement(d, free, "2G", "50%")
    assert start == 2048 * S


def test_fs_choice_aliases():
    assert common.fs_choice("vfat") == "fat32"
    assert common.fs_choice("swap") == "linux-swap"
    with pytest.raises(common.CliError):
        common.fs_choice("reiser5")


def test_shell_dry_run(monkeypatch, capsys):
    d = mbr_disk(path="/dev/sdq")
    monkeypatch.setattr("managedisks.cli.shell.scan", lambda include: [d])
    sh = DiskShell(dry_run=True, assume_yes=True)
    sh.execute("list disk")
    sh.execute("select disk 0")
    sh.execute("create partition primary size=1024")
    out = capsys.readouterr().out
    assert "Disk 0" in out and "mkfs" not in out
    assert "sfdisk" in out  # dry run prints the commands that would run
    with pytest.raises(common.CliError):
        sh.execute("select disk 7")
    with pytest.raises(common.CliError):
        sh.execute("frobnicate")
    assert sh.execute("exit") is False


def test_dry_run_create_cli(monkeypatch, capsys):
    d = gpt_disk(path="/dev/sdq")
    monkeypatch.setattr("managedisks.cli.main.scan", lambda include: [d])
    rc = cli.main(["create", "/dev/sdq", "-s", "2G", "-f", "ext4", "-L", "data", "--dry-run"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "mkfs.ext4" in out and "-L data" in out


qt = pytest.importorskip("managedisks.gui.qt", reason="no Qt bindings")


@pytest.fixture(scope="module")
def qapp():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from managedisks.gui.qt import QtWidgets
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    yield app


def test_disk_map_layout_and_paint(qapp):
    from managedisks.gui.widgets import DiskMap
    d = gpt_disk(parts=[(1, 1, "vfat"), (2, 50000, "ext4")])
    m = DiskMap()
    m.resize(800, 90)
    m.set_disk(d, ("p", S))
    pm = m.grab()
    assert not pm.isNull()
    rects = [r for r, item in m._rects if item is not None]
    assert all(r.width() >= 20 for r in rects)  # tiny partitions stay clickable
    assert abs(sum(r.width() for r in rects) + 3 * (len(rects) - 1) - (800 - 3)) < 2


def test_widths_algorithm():
    from managedisks.gui.widgets import DiskMap
    w = DiskMap._widths([1, 1000, 1], 500, 40)
    assert min(w) >= 40 - 1e-6 and abs(sum(w) - 500) < 1


def test_main_window_smoke(qapp, monkeypatch):
    from managedisks.gui import main_window
    disks = [gpt_disk(parts=[(1, 512, "vfat"), (1024, 4096, "ext4")]), mbr_disk()]
    monkeypatch.setattr(main_window.scanner, "scan", lambda include_loop=False: disks)
    from managedisks.gui.qt import QtCore
    QtCore.QSettings("ManageDisks", "ManageDisks-test").clear()
    w = main_window.MainWindow()
    for _ in range(200):
        qapp.processEvents()
        if not w.scanning:
            break
    w._scanned(disks)
    assert w.devices.count() == 2
    assert w.tree.topLevelItemCount() >= 3
    w.key = ("f", 513 * S)
    w._update_actions()
    assert w.a_new.isEnabled()
    from managedisks.core.operations import DeletePartition
    assert w.queue(DeletePartition(disks[0].path, start=S))
    assert len(w.plan.ops) == 1 and w.pending.isVisibleTo(w)
    w.undo()
    assert not w.plan.ops
    w.plan.clear()
    w.close()


def test_theme_palettes(qapp):
    from managedisks.gui import theme
    dark = theme.make_palette(True)
    light = theme.make_palette(False)
    assert theme.is_dark(dark) and not theme.is_dark(light)


def test_all_dialogs_construct(qapp):
    """Build every dialog once (catches binding-specific runtime errors)."""
    from managedisks.gui.dialogs import disk as dd
    from managedisks.gui.dialogs import partition as pd
    from managedisks.gui.dialogs.misc import ApplyConfirmDialog, SupportDialog
    from managedisks.gui.dialogs.progress import ProgressDialog
    from managedisks.core.operations import CreatePartition, DeletePartition

    d = gpt_disk(parts=[(1, 512, "vfat"), (1024, 4096, "ext4")])
    d.partitions[1].fs_used = 1024 * MiB
    m = mbr_disk()
    free = d.free_spaces()[0]
    p = d.partitions[1]
    dialogs = [
        pd.CreatePartitionDialog(None, d, free),
        pd.CreatePartitionDialog(None, m, m.free_spaces()[0]),
        pd.ResizeDialog(None, d, p, lambda cb: cb(512 * MiB)),
        pd.FormatDialog(None, d, p),
        pd.PropertiesDialog(None, d, p),
        pd.PropertiesDialog(None, m, p, editable=False),
        pd.PasteDialog(None, d, free, d, d.partitions[0]),
        dd.NewTableDialog(None, d),
        dd.WipeDialog(None, d, None),
        dd.WipeDialog(None, d, p),
        dd.DiskPicker(None, "Clone", "Clone", [d, m], d),
        SupportDialog(None),
        ApplyConfirmDialog(None, [DeletePartition(d.path, start=S)], ["careful"]),
    ]
    for dlg in dialogs:
        dlg.show()
        qapp.processEvents()
        assert not dlg.grab().isNull()
        dlg.close()
    create = dialogs[0]
    create.geo.size_edit.setValue(100 * MiB)
    create.geo._from_size(100 * MiB)
    op = create.operation()
    assert isinstance(op, CreatePartition) and op.size == 100 * S
    assert dialogs[2].geo.min_size >= 512 * S          # min size callback applied
    props = dialogs[4]
    props.label.setText("newlabel")
    assert [o.op for o in props.operations()] == ["set_label"]

    def fake_fetch(path, ok, err):
        ok({"smart_status": {"passed": True}, "temperature": {"current": 35},
            "ata_smart_attributes": {"table": [{"id": 5, "name": "Reallocated_Sector_Ct", "value": 100,
                                                "worst": 100, "thresh": 10, "raw": {"value": 0, "string": "0"}}]}})
    sd = dd.SmartDialog(None, d, fake_fetch, lambda *a: None)
    sd.show()
    qapp.processEvents()
    sd.load()
    assert sd.badge.text() == "Healthy" and sd.table.topLevelItemCount() == 1
    sd.close()

    events = []

    def start(on_event, on_result, on_error):
        for ev in ({"type": "op_start", "index": 0, "count": 1, "text": "x"},
                   {"type": "progress", "fraction": 0.5, "rate": 1e6}, {"type": "log", "line": "$ true"},
                   {"type": "op_done", "index": 0}, {"type": "done", "ok": True, "completed": 1}):
            on_event(ev)
        events.append("started")
        return 1
    pdlg = ProgressDialog(None, "Applying", ["x"], start, lambda rid: None)
    pdlg.show()
    for _ in range(5):
        qapp.processEvents()
    assert events == ["started"] and pdlg.finished_ok is True
    pdlg.close()


def test_fs_combo_respects_table(qapp):
    from managedisks.gui.dialogs.common import FsCombo

    def state(combo, fs_id):
        for i in range(combo.count()):
            if combo.itemData(i, qt.QtCore.Qt.ItemDataRole.UserRole) == fs_id:
                item = combo.model().item(i)
                return item.isEnabled(), item.text()
    apfs_gpt = state(FsCombo(table="gpt", size_bytes=1024 * MiB), "apfs")
    apfs_mbr = state(FsCombo(table="dos", size_bytes=1024 * MiB), "apfs")
    if apfs_gpt[0]:                                     # mkapfs installed
        assert "experimental" in apfs_gpt[1]
    assert apfs_mbr[0] is False
    assert state(FsCombo(table="dos"), "hfs") is None   # classic HFS cannot be created
