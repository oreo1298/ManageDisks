"""Follow the desktop's look: light/dark color scheme and accent color.

Strategy:

* Keep whatever style and palette the platform theme provides (Breeze on
  KDE Plasma, qgnomeplatform/adwaita-qt, qt6ct, ...): that *is* the system
  theme.
* If the desktop prefers dark but Qt handed us a light palette (common on
  GNOME, Cinnamon, XFCE without a Qt platform theme), switch to Fusion with
  a matching dark palette, and vice versa.
* Use the desktop accent color (xdg-desktop-portal ``accent-color``) for
  selections when the platform theme did not already do so.
* React to live changes (Qt >= 6.5 colorSchemeChanged, ThemeChange events).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess

from .qt import QT_VERSION, QtCore, QtGui, QtWidgets, Signal

_state = {"override": "system", "applied": None, "native_palette": None, "native_style": None}


def _run(argv: list[str]) -> str:
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=1.5).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def _portal_read(key: str) -> str:
    if shutil.which("gdbus"):
        return _run(["gdbus", "call", "--session", "--dest", "org.freedesktop.portal.Desktop",
                     "--object-path", "/org/freedesktop/portal/desktop",
                     "--method", "org.freedesktop.portal.Settings.ReadOne",
                     "org.freedesktop.appearance", key])
    if shutil.which("busctl"):
        return _run(["busctl", "--user", "call", "org.freedesktop.portal.Desktop",
                     "/org/freedesktop/portal/desktop", "org.freedesktop.portal.Settings",
                     "ReadOne", "ss", "org.freedesktop.appearance", key])
    return ""


def portal_prefers_dark() -> bool | None:
    out = _portal_read("color-scheme")
    m = re.search(r"(?:uint32|u)\s+(\d)", out)
    if not m:
        return None
    return {1: True, 2: False}.get(int(m.group(1)))


def portal_accent() -> QtGui.QColor | None:
    out = _portal_read("accent-color")
    if not out or "Error" in out:
        return None
    out = re.sub(r"\(ddd\)", " ", out)  # busctl prints the D-Bus signature
    vals = [float(x) for x in re.findall(r"[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?", out)]
    if len(vals) >= 3 and all(0.0 <= v <= 1.0 for v in vals[:3]):
        return QtGui.QColor.fromRgbF(*vals[:3])
    return None


def gsettings_prefers_dark() -> bool | None:
    if not shutil.which("gsettings"):
        return None
    out = _run(["gsettings", "get", "org.gnome.desktop.interface", "color-scheme"]).strip()
    if "prefer-dark" in out:
        return True
    if "prefer-light" in out:
        return False
    for schema in ("org.gnome.desktop.interface", "org.cinnamon.desktop.interface", "org.mate.interface"):
        theme = _run(["gsettings", "get", schema, "gtk-theme"]).strip().lower()
        if theme:
            return "dark" in theme
    return None


def xfce_prefers_dark() -> bool | None:
    if "XFCE" not in os.environ.get("XDG_CURRENT_DESKTOP", "").upper() or not shutil.which("xfconf-query"):
        return None
    theme = _run(["xfconf-query", "-c", "xsettings", "-p", "/Net/ThemeName"]).strip().lower()
    return ("dark" in theme) if theme else None


def system_prefers_dark(app: QtWidgets.QApplication) -> bool | None:
    if QT_VERSION >= (6, 5):
        scheme = app.styleHints().colorScheme()
        if scheme == QtCore.Qt.ColorScheme.Dark:
            return True
        if scheme == QtCore.Qt.ColorScheme.Light:
            return False
    gtk = os.environ.get("GTK_THEME", "").lower()
    if gtk:
        return "dark" in gtk
    for probe in (portal_prefers_dark, gsettings_prefers_dark, xfce_prefers_dark):
        v = probe()
        if v is not None:
            return v
    return None


def is_dark(palette: QtGui.QPalette | None = None) -> bool:
    pal = palette or QtWidgets.QApplication.palette()
    return pal.color(QtGui.QPalette.ColorRole.Window).lightnessF() < 0.5


def _is_kde() -> bool:
    return "KDE" in os.environ.get("XDG_CURRENT_DESKTOP", "").upper()


DEFAULT_ACCENT = QtGui.QColor("#3584e4")


def make_palette(dark: bool, accent: QtGui.QColor | None = None) -> QtGui.QPalette:
    accent = QtGui.QColor(accent or DEFAULT_ACCENT)
    R = QtGui.QPalette.ColorRole
    G = QtGui.QPalette.ColorGroup
    p = QtGui.QPalette()
    if dark:
        c = {R.Window: "#242424", R.WindowText: "#f2f2f2", R.Base: "#1d1d1d", R.AlternateBase: "#272727",
             R.ToolTipBase: "#303030", R.ToolTipText: "#f2f2f2", R.Text: "#f2f2f2", R.Button: "#323232",
             R.ButtonText: "#f2f2f2", R.BrightText: "#ffffff", R.Light: "#404040", R.Midlight: "#363636",
             R.Mid: "#2c2c2c", R.Dark: "#151515", R.Shadow: "#000000", R.Link: accent.lighter(130).name(),
             R.LinkVisited: accent.lighter(110).name()}
        disabled = "#7a7a7a"
    else:
        c = {R.Window: "#f6f5f4", R.WindowText: "#1f1f1f", R.Base: "#ffffff", R.AlternateBase: "#f3f3f3",
             R.ToolTipBase: "#ffffff", R.ToolTipText: "#1f1f1f", R.Text: "#1f1f1f", R.Button: "#ececec",
             R.ButtonText: "#1f1f1f", R.BrightText: "#000000", R.Light: "#ffffff", R.Midlight: "#f0f0f0",
             R.Mid: "#d0d0d0", R.Dark: "#a0a0a0", R.Shadow: "#707070", R.Link: accent.darker(110).name(),
             R.LinkVisited: accent.darker(130).name()}
        disabled = "#9a9a9a"
    for role, color in c.items():
        p.setColor(role, QtGui.QColor(color))
    p.setColor(R.Highlight, accent)
    p.setColor(R.HighlightedText, QtGui.QColor("#ffffff"))
    if hasattr(R, "PlaceholderText"):
        p.setColor(R.PlaceholderText, QtGui.QColor(disabled))
    if hasattr(R, "Accent"):
        p.setColor(R.Accent, accent)
    for role in (R.WindowText, R.Text, R.ButtonText):
        p.setColor(G.Disabled, role, QtGui.QColor(disabled))
    p.setColor(G.Disabled, R.Highlight, QtGui.QColor("#5a5a5a" if dark else "#c0c0c0"))
    return p


def set_override(mode: str) -> None:
    """'system', 'light' or 'dark'."""
    _state["override"] = mode if mode in ("system", "light", "dark") else "system"


def apply(app: QtWidgets.QApplication) -> None:
    if _state["native_palette"] is None:
        _state["native_palette"] = QtGui.QPalette(app.palette())
        _state["native_style"] = app.style().name() if hasattr(app.style(), "name") else ""
        if (_state["native_style"] or "").lower() == "windows":
            # No platform theme at all: Qt's "Windows" fallback looks dated.
            app.setStyle("Fusion")
            _state["native_style"] = "fusion"
    native = _state["native_palette"]
    mode = _state["override"]
    if mode == "system":
        want_dark = system_prefers_dark(app)
    else:
        want_dark = mode == "dark"
    native_dark = is_dark(native)
    accent = portal_accent() if not _is_kde() else None

    if want_dark is None or want_dark == native_dark:
        if mode == "system" and accent is None:
            app.setPalette(native)
            _state["applied"] = "native"
        else:
            pal = QtGui.QPalette(native)
            if accent is not None and (_state["native_style"] or "").lower() in ("fusion", ""):
                pal.setColor(QtGui.QPalette.ColorRole.Highlight, accent)
                pal.setColor(QtGui.QPalette.ColorRole.HighlightedText, QtGui.QColor("#ffffff"))
            app.setPalette(pal)
            _state["applied"] = "native+accent"
        return
    # The platform theme disagrees with the desktop preference: build our own.
    if (app.style().name().lower() if hasattr(app.style(), "name") else "") != "fusion":
        app.setStyle("Fusion")
    native_hl = native.color(QtGui.QPalette.ColorRole.Highlight)
    app.setPalette(make_palette(bool(want_dark), accent or native_hl))
    _state["applied"] = "dark" if want_dark else "light"


class ThemeWatcher(QtCore.QObject):
    """Re-applies the theme when the desktop changes it at runtime."""

    changed = Signal()

    def __init__(self, app: QtWidgets.QApplication):
        super().__init__(app)
        self.app = app
        self._busy = False
        self._timer = QtCore.QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(150)
        self._timer.timeout.connect(self._reapply)
        if QT_VERSION >= (6, 5):
            app.styleHints().colorSchemeChanged.connect(lambda *_: self._timer.start())
        app.installEventFilter(self)

    def eventFilter(self, obj, ev):  # noqa: N802 - Qt API
        if obj is self.app and ev.type() == QtCore.QEvent.Type.ThemeChange and not self._busy:
            self._timer.start()
        return False

    def _reapply(self):
        self._busy = True
        try:
            _state["native_palette"] = None if _state["applied"] == "native" else _state["native_palette"]
            apply(self.app)
            self.changed.emit()
        finally:
            self._busy = False


# -- color helpers for custom painting ------------------------------------------
def mix(a: QtGui.QColor, b: QtGui.QColor, t: float) -> QtGui.QColor:
    return QtGui.QColor.fromRgbF(
        a.redF() * (1 - t) + b.redF() * t,
        a.greenF() * (1 - t) + b.greenF() * t,
        a.blueF() * (1 - t) + b.blueF() * t,
        a.alphaF() * (1 - t) + b.alphaF() * t,
    )


def with_alpha(c: QtGui.QColor, alpha: float) -> QtGui.QColor:
    c = QtGui.QColor(c)
    c.setAlphaF(max(0.0, min(1.0, alpha)))
    return c


def fs_color(hex_color: str, palette: QtGui.QPalette | None = None) -> QtGui.QColor:
    """A filesystem color tuned for the current light/dark palette."""
    c = QtGui.QColor(hex_color)
    if is_dark(palette):
        h, s, l, a = c.getHslF()
        return QtGui.QColor.fromHslF(h, min(1.0, s * 0.85), min(0.72, l + 0.12), a)
    return c


def text_on(bg: QtGui.QColor) -> QtGui.QColor:
    lum = 0.2126 * bg.redF() + 0.7152 * bg.greenF() + 0.0722 * bg.blueF()
    return QtGui.QColor("#000000" if lum > 0.55 else "#ffffff")
