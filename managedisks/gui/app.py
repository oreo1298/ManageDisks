"""GUI entry point."""

from __future__ import annotations

import argparse
import signal
import sys

from .. import APP_ID, APP_NAME, __version__


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="managedisks", description=f"{APP_NAME} – disk and partition manager")
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    parser.add_argument("--unlock", action="store_true", help="ask for administrator rights at startup")
    parser.add_argument("--loop", action="store_true", help="show loop devices")
    parser.add_argument("--scheme", choices=["system", "light", "dark"], help="override the color scheme")
    args, qt_args = parser.parse_known_args(argv if argv is not None else sys.argv[1:])

    from .qt import QtCore, QtGui, QtWidgets

    if hasattr(QtCore.Qt, "HighDpiScaleFactorRoundingPolicy"):
        QtGui.QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
            QtCore.Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    QtGui.QGuiApplication.setDesktopFileName(APP_ID)
    app = QtWidgets.QApplication([sys.argv[0]] + qt_args)
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_NAME)
    app.setApplicationVersion(__version__)
    app.setOrganizationName("ManageDisks")

    from . import icons, theme
    from .main_window import MainWindow

    settings = QtCore.QSettings("ManageDisks", "ManageDisks")
    theme.set_override(args.scheme or settings.value("color_scheme", "system"))
    theme.apply(app)
    watcher = theme.ThemeWatcher(app)
    watcher.changed.connect(icons.clear_cache)
    app.setWindowIcon(icons.app_icon())

    if args.loop:
        settings.setValue("show_loop", True)
    win = MainWindow()
    win.show()
    if args.unlock:
        QtCore.QTimer.singleShot(200, win.unlock_admin)

    # Let Ctrl+C in a terminal close the app.
    signal.signal(signal.SIGINT, lambda *_: app.quit())
    timer = QtCore.QTimer()
    timer.start(500)
    timer.timeout.connect(lambda: None)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
