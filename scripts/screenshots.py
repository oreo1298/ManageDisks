#!/usr/bin/env python3
"""Render screenshots of the GUI offscreen (used for the README and for
visual regression checks):

    QT_QPA_PLATFORM=offscreen python3 scripts/screenshots.py OUTDIR [light|dark] [--demo]
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from managedisks.gui.qt import QtCore, QtGui, QtWidgets  # noqa: E402


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else "screenshots"
    scheme = sys.argv[2] if len(sys.argv) > 2 else "light"
    os.makedirs(out, exist_ok=True)
    app = QtWidgets.QApplication(["managedisks"])
    app.setStyle("Fusion")
    font = QtGui.QFont("Noto Sans" if "Noto Sans" in QtGui.QFontDatabase.families() else "DejaVu Sans", 10)
    app.setFont(font)
    from managedisks.gui import theme
    theme.set_override(scheme)
    theme.apply(app)
    settings = QtCore.QSettings("ManageDisks", "ManageDisks")
    settings.clear()
    settings.setValue("show_loop", True)
    from managedisks.core import scanner
    from managedisks.gui.main_window import MainWindow
    from managedisks.gui.dialogs import partition as pd
    from managedisks.gui.dialogs.misc import SupportDialog

    win = MainWindow()
    win.resize(1280, 800)
    win.show()

    def pump(n=20):
        for _ in range(n):
            app.processEvents()

    while win.scanning:  # let the startup scan finish first
        pump(1)
    disks = scanner.scan(include_loop=True)
    scanner.fill_usage(disks)
    if "--demo" in sys.argv:
        # Show only the sample loop disks, presented as example drives.
        disks = [d for d in disks if d.kind == "loop" and d.partitions]
        looks = [("Example NVMe SSD", "nvme", False), ("Example USB stick", "usb", True)]
        for d, (model, tran, removable) in zip(disks, looks):
            d.model, d.transport, d.removable, d.backing_file = model, tran, removable, ""
            d.kind, d.rotational = "disk", False
    win._scanned(disks)
    pump()
    # Select the first loop disk with partitions.
    target = next((i for i, d in enumerate(win.preview) if d.partitions), 0)
    win.devices.setCurrentRow(target)
    pump()
    win.grab().save(os.path.join(out, f"main-{scheme}.png"))

    d = win.current_disk()
    free = next(iter(d.free_spaces()), None)
    if free is not None:
        dlg = pd.CreatePartitionDialog(win, d, free)
        dlg.show()
        pump()
        dlg.grab().save(os.path.join(out, f"create-{scheme}.png"))
        op = dlg.operation()
        dlg.close()
        win.queue(op)
    part = next((p for p in d.partitions if p.fstype == "ntfs"), None)
    if part is not None:
        dlg = pd.ResizeDialog(win, win.current_disk(), part, None)
        dlg.show()
        pump()
        dlg.grab().save(os.path.join(out, f"resize-{scheme}.png"))
        dlg.close()
        from managedisks.core.operations import FormatPartition
        win.queue(FormatPartition(d.path, start=part.start, fstype="exfat", label="Shared"))
    win.key = ("p", free.start) if free is not None else win.key
    win._show_disk()
    pump()
    win.grab().save(os.path.join(out, f"pending-{scheme}.png"))
    sup = SupportDialog(win)
    sup.show()
    pump()
    sup.grab().save(os.path.join(out, f"support-{scheme}.png"))
    sup.close()
    win.plan.clear()
    win.close()
    print("saved to", out)


if __name__ == "__main__":
    main()
