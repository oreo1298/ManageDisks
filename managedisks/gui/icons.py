"""Icons: the desktop icon theme first, crisp built-in vector icons otherwise.

Built-in icons are drawn with QPainter (no SVG plugin needed), are tinted
with the current palette text color and stay sharp at any scale.
"""

from __future__ import annotations

import math

from .qt import QtCore, QtGui, QtWidgets

Qt = QtCore.Qt

# name -> freedesktop/Breeze/Adwaita theme names to try first
THEME_NAMES = {
    "add": ["list-add", "list-add-symbolic"],
    "delete": ["edit-delete", "edit-delete-symbolic"],
    "resize": ["transform-scale", "zoom-fit-best"],
    "format": ["edit-clear-all", "draw-eraser"],
    "label": ["edit-rename", "tag"],
    "check": ["tools-check-spelling", "checkbox"],
    "mount": ["media-mount", "folder-open"],
    "unmount": ["media-eject", "media-eject-symbolic"],
    "lock": ["object-locked", "changes-prevent", "system-lock-screen"],
    "unlock": ["object-unlocked", "changes-allow"],
    "apply": ["dialog-ok-apply", "dialog-ok"],
    "undo": ["edit-undo", "edit-undo-symbolic"],
    "clear": ["edit-clear-history", "edit-clear"],
    "refresh": ["view-refresh", "view-refresh-symbolic"],
    "properties": ["document-properties", "document-properties-symbolic"],
    "copy": ["edit-copy", "edit-copy-symbolic"],
    "paste": ["edit-paste", "edit-paste-symbolic"],
    "health": ["emblem-favorite", "help-about"],
    "table": ["view-list-details", "document-new"],
    "convert": ["exchange-positions", "view-refresh"],
    "wipe": ["edit-delete-shred", "edit-clear"],
    "image-write": ["document-import", "document-open"],
    "image-create": ["document-export", "document-save-as"],
    "clone": ["edit-copy", "edit-copy-symbolic"],
    "surface": ["system-search", "edit-find"],
    "support": ["preferences-system", "applications-system"],
    "menu": ["open-menu", "application-menu", "open-menu-symbolic"],
    "info": ["dialog-information", "help-about"],
    "warning": ["dialog-warning", "dialog-warning-symbolic"],
    "error": ["dialog-error", "dialog-error-symbolic"],
    "ok": ["emblem-ok-symbolic", "emblem-default"],
    "uuid": ["fingerprint", "view-barcode"],
    "flags": ["flag", "emblem-important"],
    "eject": ["media-eject", "media-eject-symbolic"],
    "log": ["text-x-generic", "document-preview"],
    "swap": ["media-playlist-repeat", "view-refresh"],
    "hdd": ["drive-harddisk", "drive-harddisk-symbolic"],
    "ssd": ["drive-harddisk-solidstate", "drive-harddisk"],
    "nvme": ["drive-harddisk-solidstate", "drive-harddisk"],
    "usb": ["drive-removable-media-usb", "drive-removable-media", "media-removable"],
    "sdcard": ["media-flash-sd-mmc", "media-flash", "drive-removable-media"],
    "loop": ["application-x-cd-image", "media-optical"],
    "app": ["io.github.oreo1298.ManageDisks", "drive-harddisk"],
}

_cache: dict[tuple[str, bool], QtGui.QIcon] = {}
_use_theme = True


def set_use_theme(value: bool) -> None:
    global _use_theme
    _use_theme = value
    _cache.clear()


def clear_cache() -> None:
    _cache.clear()


def _theme_icon(name: str) -> QtGui.QIcon | None:
    for candidate in THEME_NAMES.get(name, [name]):
        if QtGui.QIcon.hasThemeIcon(candidate):
            ic = QtGui.QIcon.fromTheme(candidate)
            # An SVG-only theme without the Qt SVG plugin yields empty pixmaps.
            if not ic.pixmap(22, 22).isNull():
                return ic
    return None


def icon(name: str, prefer_builtin: bool = False) -> QtGui.QIcon:
    key = (name, prefer_builtin)
    if key in _cache:
        return _cache[key]
    ic = None
    if _use_theme and not prefer_builtin:
        ic = _theme_icon(name)
    if ic is None:
        ic = QtGui.QIcon(DrawnIconEngine(name))
    _cache[key] = ic
    return ic


class DrawnIconEngine(QtGui.QIconEngine):
    def __init__(self, name: str):
        super().__init__()
        self.name = name

    def clone(self):
        return DrawnIconEngine(self.name)

    def paint(self, painter: QtGui.QPainter, rect: QtCore.QRect, mode, state):
        pal = QtWidgets.QApplication.palette()
        color = pal.color(QtGui.QPalette.ColorRole.WindowText)
        if mode == QtGui.QIcon.Mode.Disabled:
            color.setAlphaF(0.35)
        elif mode == QtGui.QIcon.Mode.Selected:
            color = pal.color(QtGui.QPalette.ColorRole.HighlightedText)
        painter.save()
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        side = min(rect.width(), rect.height())
        painter.translate(rect.x() + (rect.width() - side) / 2, rect.y() + (rect.height() - side) / 2)
        painter.scale(side / 24.0, side / 24.0)
        draw(painter, self.name, color)
        painter.restore()

    def pixmap(self, size, mode, state):
        pm = QtGui.QPixmap(size)
        pm.fill(Qt.GlobalColor.transparent)
        p = QtGui.QPainter(pm)
        self.paint(p, QtCore.QRect(QtCore.QPoint(0, 0), size), mode, state)
        p.end()
        return pm


def _pen(color, width=1.8):
    pen = QtGui.QPen(color, width)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    return pen


def _line(p, *pts):
    path = QtGui.QPainterPath(QtCore.QPointF(*pts[0]))
    for pt in pts[1:]:
        path.lineTo(QtCore.QPointF(*pt))
    p.drawPath(path)


def _rrect(p, x, y, w, h, r=2.0):
    p.drawRoundedRect(QtCore.QRectF(x, y, w, h), r, r)


def _drive(p, color, accent=False):
    _rrect(p, 3, 6, 18, 12, 2.5)
    _line(p, (3, 13.5), (21, 13.5))
    p.setBrush(color)
    p.drawEllipse(QtCore.QPointF(17.5, 15.8), 0.9, 0.9)
    p.setBrush(Qt.BrushStyle.NoBrush)


def draw(p: QtGui.QPainter, name: str, color: QtGui.QColor) -> None:
    p.setPen(_pen(color))
    p.setBrush(Qt.BrushStyle.NoBrush)
    n = name
    if n == "add":
        _line(p, (12, 5), (12, 19)); _line(p, (5, 12), (19, 12))
    elif n in ("delete",):
        _line(p, (5, 7), (19, 7)); _line(p, (10, 4.5), (14, 4.5))
        _line(p, (7, 7), (8, 19.5), (16, 19.5), (17, 7))
        _line(p, (10.5, 10.5), (10.5, 16.5)); _line(p, (13.5, 10.5), (13.5, 16.5))
    elif n == "resize":
        _rrect(p, 3, 7, 18, 10, 2)
        _line(p, (7, 12), (11, 12)); _line(p, (8.5, 10), (6.5, 12), (8.5, 14))
        _line(p, (13, 12), (17, 12)); _line(p, (15.5, 10), (17.5, 12), (15.5, 14))
    elif n in ("format", "wipe"):
        _line(p, (14, 4), (20, 10), (11, 19), (7, 19), (4, 16), (14, 4))
        _line(p, (9, 9.5), (15, 15.5)); _line(p, (12, 19), (20, 19))
    elif n == "label":
        _line(p, (4, 4), (12, 4), (20, 12), (12, 20), (4, 12), (4, 4))
        p.drawEllipse(QtCore.QPointF(8.5, 8.5), 1.4, 1.4)
    elif n == "check":
        p.drawEllipse(QtCore.QPointF(12, 12), 8, 8)
        _line(p, (8, 12.5), (11, 15.5), (16.5, 9))
    elif n in ("ok", "apply"):
        _line(p, (5, 12.5), (10, 17.5), (19, 7))
    elif n == "mount":
        _drive(p, color)
        _line(p, (12, 2), (12, 9)); _line(p, (9.5, 6.5), (12, 9), (14.5, 6.5))
    elif n in ("unmount", "eject"):
        _line(p, (5, 14), (12, 6), (19, 14), (5, 14)); _line(p, (5, 18.5), (19, 18.5))
    elif n in ("lock", "unlock"):
        _rrect(p, 5, 10.5, 14, 10, 2)
        if n == "lock":
            path = QtGui.QPainterPath(QtCore.QPointF(8, 10.5))
            path.lineTo(8, 7.5); path.arcTo(QtCore.QRectF(8, 3.5, 8, 8), 180, -180); path.lineTo(16, 10.5)
        else:
            path = QtGui.QPainterPath(QtCore.QPointF(8, 10.5))
            path.lineTo(8, 7.5); path.arcTo(QtCore.QRectF(8, 3.5, 8, 8), 180, -150)
        p.drawPath(path)
        _line(p, (12, 14.5), (12, 16.5))
    elif n == "undo":
        path = QtGui.QPainterPath(QtCore.QPointF(6, 10))
        path.lineTo(15, 10); path.arcTo(QtCore.QRectF(10, 10, 9, 9), 90, -180); path.lineTo(9, 19)
        p.drawPath(path)
        _line(p, (9.5, 6.5), (6, 10), (9.5, 13.5))
    elif n == "clear":
        _line(p, (6, 6), (18, 18)); _line(p, (18, 6), (6, 18))
    elif n in ("refresh", "swap", "convert"):
        if n == "convert":
            _line(p, (4, 8.5), (19, 8.5)); _line(p, (15.5, 5), (19, 8.5), (15.5, 12))
            _line(p, (20, 15.5), (5, 15.5)); _line(p, (8.5, 12), (5, 15.5), (8.5, 19))
        else:
            p.drawArc(QtCore.QRectF(5, 5, 14, 14), 30 * 16, 300 * 16)
            _line(p, (19.2, 4.5), (18.6, 8.6), (14.6, 8))
    elif n in ("properties", "info"):
        p.drawEllipse(QtCore.QPointF(12, 12), 8.5, 8.5)
        _line(p, (12, 11), (12, 16.5))
        p.setBrush(color)
        p.drawEllipse(QtCore.QPointF(12, 7.8), 0.6, 0.6)
    elif n in ("warning", "error"):
        _line(p, (12, 4), (21, 19.5), (3, 19.5), (12, 4))
        _line(p, (12, 9.5), (12, 14))
        p.setBrush(color)
        p.drawEllipse(QtCore.QPointF(12, 16.8), 0.5, 0.5)
    elif n in ("copy", "clone"):
        _rrect(p, 8, 8, 12, 12, 2)
        _line(p, (16, 5.5), (16, 4), (4, 4), (4, 16), (5.5, 16))
    elif n == "paste":
        _rrect(p, 5, 5, 14, 16, 2)
        _rrect(p, 9, 3, 6, 4, 1)
        _line(p, (8.5, 12), (15.5, 12)); _line(p, (8.5, 15.5), (13.5, 15.5))
    elif n == "health":
        path = QtGui.QPainterPath(QtCore.QPointF(12, 20))
        path.cubicTo(2, 13, 3, 4, 9, 5)
        path.cubicTo(10.5, 5.3, 11.5, 6.5, 12, 7.5)
        path.cubicTo(12.5, 6.5, 13.5, 5.3, 15, 5)
        path.cubicTo(21, 4, 22, 13, 12, 20)
        p.drawPath(path)
        _line(p, (6, 12), (9, 12), (10.5, 9.5), (13, 14.5), (14.5, 12), (18, 12))
    elif n == "table":
        _rrect(p, 3.5, 5, 17, 14, 2)
        _line(p, (3.5, 10), (20.5, 10)); _line(p, (9, 10), (9, 19))
    elif n == "image-write":
        p.drawEllipse(QtCore.QPointF(12, 12), 8.5, 8.5)
        p.drawEllipse(QtCore.QPointF(12, 12), 2.2, 2.2)
    elif n == "image-create":
        _rrect(p, 4, 4, 16, 16, 2)
        _line(p, (8, 4), (8, 9), (16, 9), (16, 4))
        _line(p, (8, 20), (8, 14), (16, 14), (16, 20))
    elif n == "surface":
        p.drawEllipse(QtCore.QPointF(10.5, 10.5), 6, 6)
        _line(p, (15, 15), (20, 20))
    elif n == "support":
        for i in range(8):
            a = i * math.pi / 4
            _line(p, (12 + 6.2 * math.cos(a), 12 + 6.2 * math.sin(a)), (12 + 8.6 * math.cos(a), 12 + 8.6 * math.sin(a)))
        p.drawEllipse(QtCore.QPointF(12, 12), 5.6, 5.6)
        p.drawEllipse(QtCore.QPointF(12, 12), 2.2, 2.2)
    elif n == "menu":
        for y in (7, 12, 17):
            _line(p, (5, y), (19, y))
    elif n == "uuid":
        for i, y in enumerate((6, 9.5, 13, 16.5)):
            _line(p, (6 + i % 2, y), (18 - i % 2, y))
    elif n == "flags":
        _line(p, (6, 21), (6, 4)); _line(p, (6, 5), (17, 5), (14.5, 9), (17, 13), (6, 13))
    elif n == "log":
        _rrect(p, 5, 3.5, 14, 17, 2)
        for y in (8, 12, 16):
            _line(p, (8.5, y), (15.5, y))
    elif n in ("hdd", "ssd", "nvme", "loop", "app"):
        _drive(p, color)
        if n in ("ssd", "nvme"):
            _line(p, (6, 10), (9, 10))
        if n == "loop":
            p.drawEllipse(QtCore.QPointF(8, 9.8), 1.5, 1.5)
    elif n == "usb":
        _rrect(p, 7, 9, 10, 12, 2)
        _rrect(p, 9, 3.5, 6, 5.5, 1)
        _line(p, (11, 5.8), (11, 6.8)); _line(p, (13, 5.8), (13, 6.8))
    elif n == "sdcard":
        _line(p, (7, 3.5), (17, 3.5), (18, 4.5), (18, 20.5), (6, 20.5), (6, 7), (7, 3.5))
        for x in (9, 11.5, 14):
            _line(p, (x, 6.5), (x, 9))
    else:
        p.drawEllipse(QtCore.QPointF(12, 12), 7, 7)


def media_icon(kind: str) -> QtGui.QIcon:
    return icon(kind if kind in ("hdd", "ssd", "nvme", "usb", "sdcard", "loop") else "hdd")


def app_icon() -> QtGui.QIcon:
    from .. import APP_ID
    if QtGui.QIcon.hasThemeIcon(APP_ID):
        return QtGui.QIcon.fromTheme(APP_ID)
    import os
    here = os.path.dirname(os.path.abspath(__file__))
    for path in (os.path.join(here, "..", "..", "data", "icons", f"{APP_ID}.svg"),
                 os.path.join(here, "app-icon.png")):
        if os.path.exists(path):
            ic = QtGui.QIcon(path)
            if not ic.pixmap(32, 32).isNull():
                return ic
    return QtGui.QIcon(AppIconEngine())


class AppIconEngine(QtGui.QIconEngine):
    """Fallback application icon: a stylised disk with a colorful partition bar."""

    def clone(self):
        return AppIconEngine()

    def paint(self, p, rect, mode, state):
        p.save()
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        s = min(rect.width(), rect.height()) / 64.0
        p.translate(rect.x(), rect.y())
        p.scale(s, s)
        grad = QtGui.QLinearGradient(0, 6, 0, 58)
        grad.setColorAt(0, QtGui.QColor("#4a90e2"))
        grad.setColorAt(1, QtGui.QColor("#2c5fb3"))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(grad)
        p.drawRoundedRect(QtCore.QRectF(4, 8, 56, 48), 10, 10)
        p.setBrush(QtGui.QColor("#ffffff"))
        p.drawRoundedRect(QtCore.QRectF(10, 30, 44, 14), 4, 4)
        x = 12.0
        for w, c in ((8, "#3fa34d"), (20, "#3d7cd6"), (9, "#e0a526")):
            p.setBrush(QtGui.QColor(c))
            p.drawRoundedRect(QtCore.QRectF(x, 32, w, 10), 2, 2)
            x += w + 1.5
        p.setBrush(QtGui.QColor(255, 255, 255, 200))
        p.drawEllipse(QtCore.QPointF(50, 18), 3, 3)
        p.restore()

    def pixmap(self, size, mode, state):
        pm = QtGui.QPixmap(size)
        pm.fill(Qt.GlobalColor.transparent)
        p = QtGui.QPainter(pm)
        self.paint(p, QtCore.QRect(QtCore.QPoint(0, 0), size), mode, state)
        p.end()
        return pm
