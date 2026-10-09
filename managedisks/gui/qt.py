"""Tiny compatibility layer so the GUI runs on PySide6 *or* PyQt6.

Distributions ship one or the other (older Debian/Ubuntu releases only
have PyQt6). Set ``MANAGEDISKS_QT=pyqt6`` or ``pyside6`` to force one.
"""

from __future__ import annotations

import os

_pref = os.environ.get("MANAGEDISKS_QT", "").lower()
_order = ["pyqt6", "pyside6"] if _pref == "pyqt6" else ["pyside6", "pyqt6"]

QT_API = ""
_err: Exception | None = None
for _api in _order:
    try:
        if _api == "pyside6":
            from PySide6 import QtCore, QtGui, QtWidgets  # noqa: F401
            from PySide6.QtCore import Property, Signal, Slot  # noqa: F401
        else:
            from PyQt6 import QtCore, QtGui, QtWidgets  # noqa: F401
            from PyQt6.QtCore import pyqtProperty as Property  # noqa: F401
            from PyQt6.QtCore import pyqtSignal as Signal  # noqa: F401
            from PyQt6.QtCore import pyqtSlot as Slot  # noqa: F401
        QT_API = _api
        break
    except ImportError as e:  # pragma: no cover - depends on installation
        _err = e

if not QT_API:  # pragma: no cover
    raise ImportError(
        "ManageDisks needs PySide6 or PyQt6. Install one of them, for example:\n"
        "  Arch:   sudo pacman -S pyside6\n"
        "  Debian/Ubuntu: sudo apt install python3-pyqt6\n"
        "  Fedora: sudo dnf install python3-pyside6\n"
        "  openSUSE: sudo zypper install python3-PyQt6"
    ) from _err

Qt = QtCore.Qt
QT_VERSION = tuple(int(x) for x in QtCore.qVersion().split(".")[:2])
