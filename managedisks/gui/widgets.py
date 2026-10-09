"""Custom widgets: the graphical disk map, usage bars, size editors and the
drag-to-resize partition editor. Everything paints with palette colors so it
follows the light/dark system theme."""

from __future__ import annotations

from ..core import filesystems
from ..core.model import Disk, FreeSpace, Partition
from ..core.units import GiB, MiB, TiB, format_size
from . import icons, theme
from .qt import QtCore, QtGui, QtWidgets, Signal

Qt = QtCore.Qt
QPalette = QtGui.QPalette

EXTENDED_COLOR = "#8a6fbf"
UNALLOCATED_COLOR = "#9a9a9a"


def seg_key(item) -> tuple[str, int] | None:
    if item is None:
        return None
    return ("p", item.start) if isinstance(item, Partition) else ("f", item.start)


def part_color(p: Partition, pal: QPalette) -> QtGui.QColor:
    if p.is_extended:
        return theme.fs_color(EXTENDED_COLOR, pal)
    inner = p.content()
    if p.fstype == "crypto_LUKS":
        return theme.fs_color(filesystems.BY_ID["luks"].color, pal)
    if not p.fstype and not inner:
        return theme.fs_color("#a0a0a0", pal)
    return theme.fs_color(filesystems.color_for(p.fstype, p.fsversion), pal)


def part_title(p: Partition) -> str:
    if p.is_new or not p.path:
        return "New partition"
    return p.path.rsplit("/", 1)[-1]


def part_fs_text(p: Partition) -> str:
    if p.is_extended:
        return "Extended"
    if p.fstype == "crypto_LUKS":
        inner = next((h.get("fstype") for h in p.holders if h.get("fstype")), "")
        return "LUKS" + (f" · {filesystems.display_name(inner)}" if inner else " (locked)")
    return filesystems.display_name(p.fstype, p.fsversion) or "Unformatted"


def usage_of(p: Partition) -> tuple[int | None, int | None]:
    for h in p.holders:
        if h.get("fs_used") is not None and h.get("fs_size"):
            return h["fs_used"], h["fs_size"]
    return p.fs_used, p.fs_size


def paint_usage_bar(painter: QtGui.QPainter, rect: QtCore.QRectF, ratio: float | None,
                    color: QtGui.QColor, pal: QPalette, warn_full: bool = True) -> None:
    painter.save()
    painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    track = theme.mix(pal.color(QPalette.ColorRole.Base), pal.color(QPalette.ColorRole.Text), 0.12)
    r = rect.height() / 2
    painter.setBrush(track)
    painter.drawRoundedRect(rect, r, r)
    if ratio is not None and ratio > 0:
        fill = QtCore.QRectF(rect)
        fill.setWidth(max(rect.height(), rect.width() * min(1.0, ratio)))
        if warn_full and ratio > 0.9:
            color = theme.fs_color("#d64545", pal)
        painter.setBrush(color)
        painter.drawRoundedRect(fill, r, r)
    painter.restore()


class DiskMap(QtWidgets.QWidget):
    """Proportional, clickable picture of a disk's layout."""

    selected = Signal(object)            # Partition | FreeSpace | None
    activated = Signal(object)
    contextRequested = Signal(object, QtCore.QPoint)

    MIN_W = 46

    def __init__(self, parent=None):
        super().__init__(parent)
        self.disk: Disk | None = None
        self._rects: list[tuple[QtCore.QRectF, object]] = []
        self._selected: tuple[str, int] | None = None
        self._hover: tuple[str, int] | None = None
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Fixed)
        self.setAccessibleName("Disk layout")

    def sizeHint(self):
        fm = self.fontMetrics()
        return QtCore.QSize(400, max(84, fm.height() * 3 + 34))

    def minimumSizeHint(self):
        return QtCore.QSize(200, self.sizeHint().height())

    def set_disk(self, disk: Disk | None, key: tuple[str, int] | None = None):
        self.disk = disk
        self._selected = key
        self._hover = None
        self._relayout()
        self.update()

    def select_key(self, key):
        if key != self._selected:
            self._selected = key
            self.update()

    def current(self):
        for _, item in self._rects:
            if seg_key(item) == self._selected:
                return item
        return None

    # -- layout -----------------------------------------------------------------
    @staticmethod
    def _widths(sizes: list[int], width: float, minw: float) -> list[float]:
        total = sum(sizes) or 1
        if not sizes:
            return []
        minw = min(minw, width / len(sizes))
        widths = [max(minw, width * s / total) for s in sizes]
        for _ in range(8):
            excess = sum(widths) - width
            if excess <= 0.25:
                break
            flex = [i for i, w in enumerate(widths) if w > minw + 0.01]
            room = sum(widths[i] - minw for i in flex)
            if room <= 0:
                break
            for i in flex:
                widths[i] -= excess * (widths[i] - minw) / room
        return widths

    def _relayout(self):
        self._rects = []
        d = self.disk
        if d is None:
            return
        area = QtCore.QRectF(self.rect()).adjusted(1.5, 1.5, -1.5, -1.5)
        if not d.table:
            self._rects.append((area, None))
            return
        segs = d.segments()
        if not segs:
            return
        gap = 3.0
        widths = self._widths([s.size for s in segs], area.width() - gap * (len(segs) - 1), self.MIN_W)
        x = area.x()
        for seg, w in zip(segs, widths):
            r = QtCore.QRectF(x, area.y(), w, area.height())
            self._rects.append((r, seg))
            if isinstance(seg, Partition) and seg.is_extended:
                inner = r.adjusted(4, 18, -4, -4)
                kids = d.segments_in_extended()
                if kids and inner.width() > 10:
                    kw = self._widths([k.size for k in kids], inner.width() - gap * (len(kids) - 1),
                                      min(self.MIN_W, inner.width() / len(kids)))
                    kx = inner.x()
                    for k, ww in zip(kids, kw):
                        self._rects.append((QtCore.QRectF(kx, inner.y(), ww, inner.height()), k))
                        kx += ww + gap
            x += w + gap

    def resizeEvent(self, e):
        self._relayout()
        super().resizeEvent(e)

    # -- painting ---------------------------------------------------------------
    def paintEvent(self, e):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        pal = self.palette()
        d = self.disk
        if d is None:
            return
        if not d.table:
            r = self._rects[0][0] if self._rects else QtCore.QRectF(self.rect())
            self._paint_whole(p, r, d, pal)
            return
        for r, item in self._rects:
            if isinstance(item, Partition):
                self._paint_part(p, r, item, pal)
            elif isinstance(item, FreeSpace):
                self._paint_free(p, r, item, pal)
        if self.hasFocus():
            cur = next((r for r, it in self._rects if seg_key(it) == self._selected), None)
            if cur is not None:
                pen = QtGui.QPen(pal.color(QPalette.ColorRole.Highlight), 1, Qt.PenStyle.DotLine)
                p.setPen(pen)
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawRoundedRect(cur.adjusted(-1, -1, 1, 1), 8, 8)

    def _frame(self, p, r, item, base, accent, dashed=False):
        key = seg_key(item)
        sel = key is not None and key == self._selected
        hov = key is not None and key == self._hover
        hl = self.palette().color(QPalette.ColorRole.Highlight)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(base)
        p.drawRoundedRect(r, 7, 7)
        if sel:
            p.setBrush(theme.with_alpha(hl, 0.14))
            p.drawRoundedRect(r, 7, 7)
        elif hov:
            p.setBrush(theme.with_alpha(hl, 0.07))
            p.drawRoundedRect(r, 7, 7)
        if sel:
            pen = QtGui.QPen(hl, 2)
        elif hov:
            pen = QtGui.QPen(theme.with_alpha(hl, 0.6), 1.2)
        else:
            pen = QtGui.QPen(theme.mix(base, self.palette().color(QPalette.ColorRole.Text), 0.22), 1)
        if dashed:
            pen.setStyle(Qt.PenStyle.DashLine)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(r.adjusted(0.5, 0.5, -0.5, -0.5), 7, 7)

    def _texts(self, p, r, lines, color, top_pad=10):
        if r.width() < 30:
            return
        fm = self.fontMetrics()
        y = r.y() + top_pad
        for i, (text, bold) in enumerate(lines):
            if not text:
                continue
            if y + fm.height() > r.bottom() - 2:
                break
            f = QtGui.QFont(self.font())
            if bold:
                f.setWeight(QtGui.QFont.Weight.DemiBold)
            p.setFont(f)
            m = QtGui.QFontMetrics(f)
            el = m.elidedText(text, Qt.TextElideMode.ElideRight, int(r.width() - 14))
            c = QtGui.QColor(color)
            if i > 0:
                c.setAlphaF(0.75)
            p.setPen(c)
            p.drawText(QtCore.QPointF(r.x() + 7, y + m.ascent()), el)
            y += m.height() + 1

    def _paint_part(self, p, r, part: Partition, pal):
        d = self.disk
        base = pal.color(QPalette.ColorRole.Base)
        color = part_color(part, pal)
        txt = pal.color(QPalette.ColorRole.Text)
        if part.is_extended:
            tinted = theme.mix(base, color, 0.08)
            self._frame(p, r, part, tinted, color, dashed=part.is_new)
            p.save()
            f = QtGui.QFont(self.font())
            f.setPointSizeF(max(6.5, f.pointSizeF() * 0.85))
            p.setFont(f)
            p.setPen(theme.with_alpha(txt, 0.7))
            p.drawText(QtCore.QRectF(r.x() + 7, r.y() + 2, r.width() - 14, 15),
                       int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                       QtGui.QFontMetrics(f).elidedText(f"Extended · {format_size(part.size * d.sector_size)}",
                                                        Qt.TextElideMode.ElideRight, int(r.width() - 14)))
            p.restore()
            return
        self._frame(p, r, part, base, color, dashed=part.is_new)
        # color band + usage fill
        p.save()
        clip = QtGui.QPainterPath()
        clip.addRoundedRect(r.adjusted(1, 1, -1, -1), 6, 6)
        p.setClipPath(clip)
        p.setPen(Qt.PenStyle.NoPen)
        used, size = usage_of(part)
        if used is not None and size:
            ratio = max(0.0, min(1.0, used / size))
            fill = QtCore.QRectF(r.x(), r.y() + 5, r.width() * ratio, r.height() - 5)
            p.setBrush(theme.with_alpha(color, 0.22 if not theme.is_dark(pal) else 0.30))
            p.drawRect(fill)
        p.setBrush(color)
        p.drawRect(QtCore.QRectF(r.x(), r.y(), r.width(), 5))
        p.restore()
        size_text = format_size(part.size * d.sector_size)
        title = part.label or part.name or part_title(part)
        sub = f"{part_fs_text(part)} · {size_text}"
        third = ", ".join(part.all_mountpoints()) or ("swap (active)" if part.is_swap_active else "")
        if part.label or part.name:
            third = (part_title(part) + ("  " + third if third else ""))
        self._texts(p, r, [(title, True), (sub, False), (third, False)], txt, top_pad=11)
        if part.pending and not part.is_new and r.width() > 24:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(pal.color(QPalette.ColorRole.Highlight))
            p.drawEllipse(QtCore.QPointF(r.right() - 9, r.y() + 13), 3.5, 3.5)
        if (part.is_mounted or part.is_swap_active or part.holders and part.fstype == "crypto_LUKS") \
                and r.width() > 40:
            ic = icons.icon("lock" if part.fstype == "crypto_LUKS" and not part.is_mounted else "mount",
                            prefer_builtin=True)
            ic.paint(p, QtCore.QRect(int(r.right() - 22), int(r.bottom() - 21), 15, 15))

    def _paint_free(self, p, r, free: FreeSpace, pal):
        base = pal.color(QPalette.ColorRole.Window)
        txt = pal.color(QPalette.ColorRole.Text)
        self._frame(p, r, free, theme.mix(base, pal.color(QPalette.ColorRole.Base), 0.4), None, dashed=False)
        p.save()
        clip = QtGui.QPainterPath()
        clip.addRoundedRect(r.adjusted(1, 1, -1, -1), 6, 6)
        p.setClipPath(clip)
        brush = QtGui.QBrush(theme.with_alpha(txt, 0.09), Qt.BrushStyle.BDiagPattern)
        p.fillRect(r, brush)
        p.restore()
        self._texts(p, r, [("Unallocated", True), (format_size(free.size * self.disk.sector_size), False)],
                    theme.with_alpha(txt, 0.85))

    def _paint_whole(self, p, r, d: Disk, pal):
        base = pal.color(QPalette.ColorRole.Base)
        txt = pal.color(QPalette.ColorRole.Text)
        self._frame(p, r, None, base, None)
        if d.fstype:
            color = theme.fs_color(filesystems.color_for(d.fstype), pal)
            p.save()
            clip = QtGui.QPainterPath()
            clip.addRoundedRect(r.adjusted(1, 1, -1, -1), 6, 6)
            p.setClipPath(clip)
            p.fillRect(QtCore.QRectF(r.x(), r.y(), r.width(), 5), color)
            p.restore()
            self._texts(p, r, [(d.label or d.name, True),
                               (f"{filesystems.display_name(d.fstype)} on the whole disk · {format_size(d.size)}",
                                False),
                               (", ".join(d.mountpoints), False)], txt, 11)
        else:
            brush = QtGui.QBrush(theme.with_alpha(txt, 0.08), Qt.BrushStyle.BDiagPattern)
            p.fillRect(r.adjusted(2, 2, -2, -2), brush)
            self._texts(p, r, [("No partition table", True),
                               (f"{format_size(d.size)} · create a partition table to start", False)], txt)

    # -- interaction --------------------------------------------------------------
    def _item_at(self, pos):
        found = None
        for r, item in self._rects:
            if item is not None and r.contains(QtCore.QPointF(pos)):
                found = item  # children are listed after their extended parent
        return found

    def mouseMoveEvent(self, e):
        key = seg_key(self._item_at(e.position().toPoint() if hasattr(e, "position") else e.pos()))
        if key != self._hover:
            self._hover = key
            self.update()
            item = self._item_at(e.position().toPoint() if hasattr(e, "position") else e.pos())
            self.setToolTip(self._tooltip(item) if item is not None else "")

    def leaveEvent(self, e):
        self._hover = None
        self.update()

    def _tooltip(self, item):
        d = self.disk
        if isinstance(item, Partition):
            lines = [f"<b>{part_title(item)}</b>", part_fs_text(item),
                     format_size(item.size * d.sector_size)]
            used, size = usage_of(item)
            if used is not None and size:
                lines.append(f"{format_size(used)} used ({used * 100 // size}%)")
            if item.label:
                lines.append(f"Label: {item.label}")
            if item.all_mountpoints():
                lines.append("Mounted at " + ", ".join(item.all_mountpoints()))
            return "<br>".join(lines)
        if isinstance(item, FreeSpace):
            return f"<b>Unallocated</b><br>{format_size(item.size * d.sector_size)}"
        return ""

    def mousePressEvent(self, e):
        pos = e.position().toPoint() if hasattr(e, "position") else e.pos()
        item = self._item_at(pos)
        self._selected = seg_key(item)
        self.update()
        self.selected.emit(item)
        if e.button() == Qt.MouseButton.RightButton:
            self.contextRequested.emit(item, self.mapToGlobal(pos))

    def mouseDoubleClickEvent(self, e):
        pos = e.position().toPoint() if hasattr(e, "position") else e.pos()
        item = self._item_at(pos)
        if item is not None:
            self.activated.emit(item)

    def keyPressEvent(self, e):
        items = [it for _, it in self._rects if it is not None]
        items.sort(key=lambda it: (it.start, 0 if isinstance(it, Partition) and it.is_extended else 1))
        if not items:
            return super().keyPressEvent(e)
        keys = [seg_key(it) for it in items]
        idx = keys.index(self._selected) if self._selected in keys else -1
        if e.key() in (Qt.Key.Key_Right, Qt.Key.Key_Down):
            idx = min(len(items) - 1, idx + 1)
        elif e.key() in (Qt.Key.Key_Left, Qt.Key.Key_Up):
            idx = max(0, idx - 1)
        elif e.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and idx >= 0:
            self.activated.emit(items[idx])
            return
        elif e.key() == Qt.Key.Key_Menu and idx >= 0:
            r = next(r for r, it in self._rects if it is items[idx])
            self.contextRequested.emit(items[idx], self.mapToGlobal(r.center().toPoint()))
            return
        else:
            return super().keyPressEvent(e)
        self._selected = keys[idx]
        self.update()
        self.selected.emit(items[idx])


class UsageDelegate(QtWidgets.QStyledItemDelegate):
    """Item delegate drawing a usage bar plus text (role UserRole+1 holds (used, size, color))."""

    ROLE = Qt.ItemDataRole.UserRole + 1

    def paint(self, painter, option, index):
        data = index.data(self.ROLE)
        opt = QtWidgets.QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        text = opt.text
        opt.text = ""
        style = opt.widget.style() if opt.widget else QtWidgets.QApplication.style()
        style.drawControl(QtWidgets.QStyle.ControlElement.CE_ItemViewItem, opt, painter, opt.widget)
        if not data:
            painter.save()
            painter.setPen(opt.palette.color(QPalette.ColorRole.Text))
            painter.drawText(opt.rect.adjusted(6, 0, -6, 0),
                             int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), text)
            painter.restore()
            return
        used, size, color = data
        r = QtCore.QRectF(opt.rect).adjusted(6, 0, -6, 0)
        bar_w = min(70.0, r.width() * 0.45)
        bar = QtCore.QRectF(r.x(), r.center().y() - 3, bar_w, 6)
        paint_usage_bar(painter, bar, used / size if size else None, QtGui.QColor(color), opt.palette)
        painter.save()
        selected = bool(opt.state & QtWidgets.QStyle.StateFlag.State_Selected)
        painter.setPen(opt.palette.color(QPalette.ColorRole.HighlightedText if selected else QPalette.ColorRole.Text))
        tr = r.adjusted(bar_w + 8, 0, 0, 0)
        painter.drawText(tr, int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                         opt.fontMetrics.elidedText(text, Qt.TextElideMode.ElideRight, int(tr.width())))
        painter.restore()

    def sizeHint(self, option, index):
        s = super().sizeHint(option, index)
        return QtCore.QSize(s.width() + 80, s.height())


class SizeEdit(QtWidgets.QWidget):
    """Spin box with a unit selector; works in bytes."""

    valueChanged = Signal(int)

    UNITS = [("MiB", MiB), ("GiB", GiB), ("TiB", TiB)]

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QtWidgets.QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        self.spin = QtWidgets.QDoubleSpinBox()
        self.spin.setKeyboardTracking(False)
        self.spin.setAccelerated(True)
        self.spin.setMinimumWidth(110)
        self.unit = QtWidgets.QComboBox()
        for name, _ in self.UNITS:
            self.unit.addItem(name)
        lay.addWidget(self.spin, 1)
        lay.addWidget(self.unit)
        self._min = 0
        self._max = 0
        self._bytes = 0
        self._block = False
        self.spin.valueChanged.connect(self._spin_changed)
        self.unit.currentIndexChanged.connect(self._unit_changed)

    def _factor(self) -> int:
        return self.UNITS[self.unit.currentIndex()][1]

    def _sync(self):
        self._block = True
        f = self._factor()
        decimals = 0 if f == MiB else 2
        self.spin.setDecimals(decimals)
        self.spin.setRange(self._min / f, self._max / f)
        self.spin.setSingleStep(1 if f == MiB else 0.25)
        self.spin.setValue(self._bytes / f)
        self._block = False

    def setRange(self, lo: int, hi: int):
        self._min, self._max = max(0, lo), max(lo, hi)
        if self.unit.currentIndex() == 0 and hi >= 4 * GiB:
            self.unit.setCurrentIndex(2 if hi >= 4 * TiB else 1)
        self._bytes = min(max(self._bytes, self._min), self._max)
        self._sync()

    def setValue(self, b: int):
        self._bytes = min(max(int(b), self._min), self._max)
        self._sync()

    def value(self) -> int:
        return self._bytes

    def _spin_changed(self, v):
        if self._block:
            return
        b = int(round(v * self._factor()))
        if abs(b - self._max) < self._factor() / 200:
            b = self._max
        if abs(b - self._min) < self._factor() / 200:
            b = self._min
        self._bytes = min(max(b, self._min), self._max)
        self.valueChanged.emit(self._bytes)

    def _unit_changed(self, _):
        self._sync()


class PartitionResizer(QtWidgets.QWidget):
    """Visual editor: drag the edges to resize, the middle to move."""

    changed = Signal(int, int)  # start, size (sectors)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.lo = 0
        self.hi = 0
        self.start = 0
        self.size = 0
        self.min_size = 1
        self.max_size = None
        self.align = 2048
        self.can_move = True
        self.can_resize_left = True
        self.used = 0          # sectors that must stay (shown hatched)
        self.sector_size = 512
        self.color = QtGui.QColor("#3d7cd6")
        self._drag = None
        self.setMouseTracking(True)
        self.setMinimumHeight(64)
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Fixed)

    def sizeHint(self):
        return QtCore.QSize(480, 64)

    def configure(self, lo, hi, start, size, *, min_size=1, max_size=None, align=2048, can_move=True,
                  can_resize_left=True, used=0, color="#3d7cd6", sector_size=512):
        self.lo, self.hi = lo, hi
        self.start, self.size = start, size
        self.min_size = max(1, min_size)
        self.max_size = max_size
        self.align = max(1, align)
        self.can_move = can_move
        self.can_resize_left = can_resize_left
        self.used = used
        self.color = QtGui.QColor(color)
        self.sector_size = sector_size
        self.update()

    def set_geometry(self, start, size, emit=False):
        self.start, self.size = start, size
        self.update()
        if emit:
            self.changed.emit(start, size)

    def _track(self) -> QtCore.QRectF:
        return QtCore.QRectF(self.rect()).adjusted(10, 12, -10, -12)

    def _x(self, sector) -> float:
        t = self._track()
        span = max(1, self.hi - self.lo + 1)
        return t.x() + (sector - self.lo) / span * t.width()

    def _sector(self, x) -> int:
        t = self._track()
        span = max(1, self.hi - self.lo + 1)
        return int(self.lo + (x - t.x()) / max(1.0, t.width()) * span)

    def _block_rect(self) -> QtCore.QRectF:
        t = self._track()
        x1, x2 = self._x(self.start), self._x(self.start + self.size)
        if x2 - x1 < 6:
            x2 = x1 + 6
        return QtCore.QRectF(x1, t.y(), x2 - x1, t.height())

    def paintEvent(self, e):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        pal = self.palette()
        t = self._track()
        txt = pal.color(QPalette.ColorRole.Text)
        p.setPen(QtGui.QPen(theme.with_alpha(txt, 0.25), 1))
        p.setBrush(theme.mix(pal.color(QPalette.ColorRole.Window), pal.color(QPalette.ColorRole.Base), 0.5))
        p.drawRoundedRect(t, 6, 6)
        p.fillRect(t.adjusted(2, 2, -2, -2), QtGui.QBrush(theme.with_alpha(txt, 0.07), Qt.BrushStyle.BDiagPattern))
        b = self._block_rect()
        color = theme.fs_color(self.color.name(), pal)
        p.setPen(QtGui.QPen(color.darker(120), 1.2))
        p.setBrush(theme.with_alpha(color, 0.28))
        p.drawRoundedRect(b, 6, 6)
        if self.used:
            u = QtCore.QRectF(b.x(), b.y(), min(b.width(), self._x(self.start + self.used) - b.x()), b.height())
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(theme.with_alpha(color, 0.55))
            p.drawRoundedRect(u, 6, 6)
        hl = pal.color(QPalette.ColorRole.Highlight)
        for x, enabled in ((b.left(), self.can_resize_left), (b.right(), True)):
            if not enabled:
                continue
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(hl)
            p.drawRoundedRect(QtCore.QRectF(x - 3, b.center().y() - 12, 6, 24), 3, 3)
        p.setPen(theme.text_on(theme.mix(pal.color(QPalette.ColorRole.Base), color, 0.28)))
        sz = format_size(self.size * self.sector_size)
        p.drawText(b, int(Qt.AlignmentFlag.AlignCenter),
                   self.fontMetrics().elidedText(sz, Qt.TextElideMode.ElideRight, int(b.width() - 6)))

    def _hit(self, x):
        b = self._block_rect()
        if self.can_resize_left and abs(x - b.left()) <= 7:
            return "left"
        if abs(x - b.right()) <= 7:
            return "right"
        if b.left() < x < b.right() and self.can_move:
            return "move"
        return None

    def mousePressEvent(self, e):
        x = e.position().x() if hasattr(e, "position") else e.pos().x()
        h = self._hit(x)
        if h:
            self._drag = (h, x, self.start, self.size)

    def mouseMoveEvent(self, e):
        x = e.position().x() if hasattr(e, "position") else e.pos().x()
        if not self._drag:
            h = self._hit(x)
            self.setCursor(Qt.CursorShape.SizeHorCursor if h in ("left", "right") else
                           Qt.CursorShape.OpenHandCursor if h == "move" else Qt.CursorShape.ArrowCursor)
            return
        kind, x0, s0, n0 = self._drag
        delta = self._sector(x) - self._sector(x0)
        a = self.align
        max_size = self.max_size or (self.hi - self.lo + 1)
        if kind == "move":
            ns = s0 + delta
            ns = max(self.lo, min(ns, self.hi + 1 - n0))
            ns = ns - (ns - 0) % a if ns % a else ns
            ns = max(self.lo, ns)
            if ns + n0 - 1 > self.hi:
                ns = self.hi + 1 - n0
            self.set_geometry(ns, n0, emit=True)
        elif kind == "right":
            n = n0 + delta
            n = (n // a) * a if n > a else n
            end_cap = self.hi - s0 + 1
            n = max(self.min_size, min(n, end_cap, max_size))
            self.set_geometry(s0, n, emit=True)
        elif kind == "left":
            end = s0 + n0
            ns = s0 + delta
            ns = (ns // a) * a if ns % a else ns
            ns = max(self.lo, min(ns, end - self.min_size))
            n = end - ns
            if n > max_size:
                ns, n = end - max_size, max_size
            self.set_geometry(ns, n, emit=True)

    def mouseReleaseEvent(self, e):
        self._drag = None


class Banner(QtWidgets.QFrame):
    """Inline message bar with an optional action button."""

    def __init__(self, text="", icon_name="info", button_text=None, parent=None):
        super().__init__(parent)
        self.setObjectName("banner")
        lay = QtWidgets.QHBoxLayout(self)
        lay.setContentsMargins(12, 8, 8, 8)
        lay.setSpacing(10)
        self.icon = QtWidgets.QLabel()
        self.icon.setPixmap(icons.icon(icon_name, prefer_builtin=True).pixmap(18, 18))
        self.label = QtWidgets.QLabel(text)
        self.label.setWordWrap(True)
        self.label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(self.icon)
        lay.addWidget(self.label, 1)
        self.button = None
        if button_text:
            self.button = QtWidgets.QPushButton(button_text)
            lay.addWidget(self.button)
        self.kind = "info"

    def set_kind(self, kind: str):
        self.kind = kind
        name = {"info": "info", "warning": "warning", "error": "error"}.get(kind, "info")
        self.icon.setPixmap(icons.icon(name, prefer_builtin=True).pixmap(18, 18))
        self.update()

    def paintEvent(self, e):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        pal = self.palette()
        base = pal.color(QPalette.ColorRole.Window)
        tint = {"info": pal.color(QPalette.ColorRole.Highlight), "warning": QtGui.QColor("#e5a50a"),
                "error": QtGui.QColor("#e01b24")}.get(self.kind, pal.color(QPalette.ColorRole.Highlight))
        p.setPen(QtGui.QPen(theme.with_alpha(tint, 0.45), 1))
        p.setBrush(theme.mix(base, tint, 0.13))
        p.drawRoundedRect(QtCore.QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 8, 8)


class Chip(QtWidgets.QLabel):
    """Small rounded tag such as 'GPT' or 'Removable'."""

    def __init__(self, text="", color: str | None = None, parent=None):
        super().__init__(text, parent)
        self._color = color
        f = self.font()
        f.setPointSizeF(max(7.0, f.pointSizeF() * 0.88))
        f.setWeight(QtGui.QFont.Weight.DemiBold)
        self.setFont(f)
        self.setContentsMargins(8, 2, 8, 2)

    def set_color(self, color: str | None):
        self._color = color
        self.update()

    def paintEvent(self, e):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        pal = self.palette()
        base = pal.color(QPalette.ColorRole.Window)
        c = QtGui.QColor(self._color) if self._color else pal.color(QPalette.ColorRole.Text)
        if self._color:
            c = theme.fs_color(self._color, pal)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(theme.mix(base, c, 0.16))
        r = QtCore.QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p.drawRoundedRect(r, r.height() / 2, r.height() / 2)
        p.setPen(theme.mix(pal.color(QPalette.ColorRole.WindowText), c, 0.35 if self._color else 0.0))
        p.drawText(self.rect(), int(Qt.AlignmentFlag.AlignCenter), self.text())

    def sizeHint(self):
        fm = self.fontMetrics()
        return QtCore.QSize(fm.horizontalAdvance(self.text()) + 18, fm.height() + 6)

    def minimumSizeHint(self):
        return self.sizeHint()


def color_swatch(color: QtGui.QColor, size: int = 12) -> QtGui.QIcon:
    pm = QtGui.QPixmap(size * 2, size * 2)
    pm.setDevicePixelRatio(2.0)
    pm.fill(Qt.GlobalColor.transparent)
    p = QtGui.QPainter(pm)
    p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
    p.setPen(QtGui.QPen(color.darker(130), 1))
    p.setBrush(color)
    p.drawRoundedRect(QtCore.QRectF(1, 1, size - 2, size - 2), 3, 3)
    p.end()
    return QtGui.QIcon(pm)
