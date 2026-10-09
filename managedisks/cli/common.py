"""Shared helpers for the CLI and the interactive shell."""

from __future__ import annotations

import getpass
import json
import os
import shutil
import sys
import threading

from ..core import executor, filesystems, parttypes, scanner
from ..core.model import Disk, FreeSpace, Partition
from ..core.operations import Operation, OpError, Plan
from ..core.units import MiB, SizeError, align_down, align_up, format_size, parse_size


class CliError(Exception):
    pass


def use_color() -> bool:
    return sys.stdout.isatty() and not os.environ.get("NO_COLOR") and os.environ.get("TERM") != "dumb"


def c(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if use_color() else text


def bold(t): return c(t, "1")
def dim(t): return c(t, "2")
def red(t): return c(t, "31")
def green(t): return c(t, "32")
def yellow(t): return c(t, "33")
def blue(t): return c(t, "34")


def table(rows: list[list[str]], header: list[str] | None = None, indent: int = 2) -> str:
    import re
    strip = re.compile(r"\033\[[0-9;]*m")
    allrows = ([header] if header else []) + rows
    if not allrows:
        return ""
    widths = [max(len(strip.sub("", str(r[i]))) for r in allrows) for i in range(len(allrows[0]))]
    lines = []
    for n, r in enumerate(allrows):
        cells = []
        for i, cell in enumerate(r):
            cell = str(cell)
            pad = widths[i] - len(strip.sub("", cell))
            cells.append(cell + " " * pad)
        line = " " * indent + "  ".join(cells).rstrip()
        lines.append(bold(line) if header and n == 0 else line)
    return "\n".join(lines)


def scan(include_loop: bool) -> list[Disk]:
    disks = scanner.scan(include_loop=include_loop)
    if os.geteuid() == 0:
        scanner.fill_usage(disks)
    return disks


def find_device(disks: list[Disk], dev: str) -> tuple[Disk, Partition | None]:
    real = os.path.realpath(dev)
    for d in disks:
        if os.path.realpath(d.path) == real or d.name == dev:
            return d, None
        for p in d.partitions:
            if p.path and (os.path.realpath(p.path) == real or os.path.basename(p.path) == dev):
                return d, p
    raise CliError(f"No such disk or partition: {dev}")


def find_disk_arg(disks, dev) -> Disk:
    d, p = find_device(disks, dev)
    if p is not None:
        raise CliError(f"{dev} is a partition; a whole disk is expected")
    return d


def find_part_arg(disks, dev) -> tuple[Disk, Partition]:
    d, p = find_device(disks, dev)
    if p is None:
        raise CliError(f"{dev} is a disk; a partition is expected")
    return d, p


def size_arg(text: str, disk: Disk, total_bytes: int | None = None) -> int:
    """Size in sectors (rounded down to MiB)."""
    try:
        b = parse_size(text, total=total_bytes, sector_size=disk.sector_size)
    except SizeError as e:
        raise CliError(str(e)) from None
    sectors = b // disk.sector_size
    aligned = align_down(sectors, disk.alignment)
    return aligned if aligned > 0 else sectors


def pick_free(disk: Disk, start_text: str | None, logical: bool) -> FreeSpace:
    frees = [f for f in disk.free_spaces() if f.inside_extended == logical]
    if not frees:
        raise CliError("No unallocated space" + (" inside the extended partition" if logical else "")
                       + f" on {disk.path}")
    if start_text is None:
        return max(frees, key=lambda f: f.size) if not logical else frees[0]
    try:
        sector = parse_size(start_text, sector_size=disk.sector_size) // disk.sector_size
    except SizeError as e:
        raise CliError(str(e)) from None
    for f in frees:
        if f.start <= sector <= f.end:
            return f
    raise CliError(f"Position {start_text} is not inside unallocated space")


def placement(disk: Disk, free: FreeSpace, start_text: str | None, size_text: str | None) -> tuple[int, int]:
    """(start, size) in sectors for a new partition inside ``free``."""
    ustart, usize = disk.usable_extent(free)
    start = ustart
    if start_text is not None:
        sector = parse_size(start_text, sector_size=disk.sector_size) // disk.sector_size
        start = max(ustart, align_up(sector, disk.alignment))
    avail = free.end + 1 - start
    if avail <= 0:
        raise CliError("No room at that position")
    if size_text in (None, "", "max", "100%"):
        size = align_down(avail, disk.alignment) or avail
    else:
        size = size_arg(size_text, disk, total_bytes=avail * disk.sector_size)
    if size > avail:
        raise CliError(f"Only {format_size(avail * disk.sector_size)} available there")
    return start, size


def confirm(question: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        raise CliError("Refusing to make changes without confirmation; pass --yes")
    try:
        ans = input(f"{question} [y/N] ").strip().lower()
    except EOFError:
        return False
    return ans in ("y", "yes")


def ask_passphrase() -> str:
    env = os.environ.get("MANAGEDISKS_PASSPHRASE")
    if env:
        return env
    if not sys.stdin.isatty():
        return sys.stdin.readline().rstrip("\n")
    a = getpass.getpass("Passphrase: ")
    b = getpass.getpass("Repeat passphrase: ")
    if a != b:
        raise CliError("Passphrases do not match")
    if not a:
        raise CliError("Empty passphrase")
    return a


def require_root() -> None:
    if os.geteuid() != 0:
        raise CliError("This command changes disks and must run as root, e.g.: sudo managedisks-cli "
                       + " ".join(sys.argv[1:]))


class Printer:
    """Renders executor events on the terminal."""

    def __init__(self, verbose: bool = False, as_json: bool = False):
        self.verbose = verbose
        self.as_json = as_json
        self.tty = sys.stderr.isatty()
        self._bar = False

    def _clear(self):
        if self._bar:
            sys.stderr.write("\r" + " " * (shutil.get_terminal_size().columns - 1) + "\r")
            self._bar = False

    def __call__(self, ev: dict) -> None:
        if self.as_json:
            print(json.dumps(ev), flush=True)
            return
        t = ev.get("type")
        if t == "op_start":
            self._clear()
            print(bold(f"[{ev['index'] + 1}/{ev['count']}] {ev['text']}"), flush=True)
        elif t == "step":
            self._clear()
            print(f"  {blue('›')} {ev['text']}", flush=True)
        elif t == "log":
            if self.verbose or ev["line"].startswith("$ "):
                self._clear()
                print(dim("    " + ev["line"]), flush=True)
        elif t == "progress" and self.tty:
            frac = ev.get("fraction")
            if frac is None:
                return
            width = max(10, min(40, shutil.get_terminal_size().columns - 30))
            filled = int(frac * width)
            rate = ev.get("rate")
            extra = f"  {format_size(rate)}/s" if rate else ""
            sys.stderr.write(f"\r    [{'#' * filled}{'.' * (width - filled)}] {frac * 100:5.1f}%{extra}   ")
            sys.stderr.flush()
            self._bar = True
        elif t == "op_failed":
            self._clear()
            print(red(f"  ✗ {ev['error']}"), flush=True)
        elif t == "op_done":
            self._clear()
            print(green("  ✓ done"), flush=True)
        elif t == "done":
            self._clear()


def run_ops(ops: list[Operation], *, dry_run: bool, assume_yes: bool, verbose: bool = False,
            as_json: bool = False, disks: list[Disk] | None = None) -> bool:
    if not dry_run:
        require_root()
    destructive = any(o.destructive for o in ops)
    if not as_json:
        print(bold("Pending operations:"))
        for i, o in enumerate(ops, 1):
            print(f"  {i}. {o.describe()}")
        warns = [w for o in ops for w in o.warnings(disks or [])]
        for w in warns:
            print(yellow(f"  ! {w}"))
    if dry_run:
        if not as_json:
            print(dim("Dry run: showing the commands that would run."))
    elif not confirm(red("Apply these changes? Data may be lost.") if destructive else "Apply these changes?",
                     assume_yes):
        print("Aborted.")
        return False
    cancel = threading.Event()
    printer = Printer(verbose or dry_run, as_json)
    try:
        ok = executor.execute(ops, printer, cancel, dry_run=dry_run, disks=disks if dry_run else None)
    except KeyboardInterrupt:
        cancel.set()
        print(red("Interrupted"))
        return False
    if not as_json:
        print(green("All operations completed successfully.") if ok else red("Stopped because of an error."))
    return ok


def plan_one(disks: list[Disk], op: Operation) -> list[Operation]:
    plan = Plan()
    try:
        plan.add(disks, op)
    except OpError as e:
        raise CliError(str(e)) from None
    return plan.ops


def fs_choice(name: str) -> str:
    key = name.lower()
    aliases = {"vfat": "fat32", "fat": "fat32", "swap": "linux-swap", "none": "cleared",
               "raw": "cleared", "ext": "ext4", "msdos": "fat16"}
    key = aliases.get(key, key)
    if key not in filesystems.BY_ID or key == "luks":
        raise CliError(f"Unknown filesystem {name!r}. Choose from: "
                       + ", ".join(f.id for f in filesystems.creatable() if f.id != "luks"))
    return key


def type_choice(value: str, table_kind: str) -> str:
    try:
        return parttypes.resolve(value, table_kind)
    except ValueError as e:
        raise CliError(str(e)) from None


def fmt_part_fs(p: Partition) -> str:
    inner = p.content()
    if p.fstype == "crypto_LUKS":
        return "LUKS" + (f" → {filesystems.display_name(inner)}" if inner and inner != p.fstype else "")
    return filesystems.display_name(p.fstype, p.fsversion) or ("extended" if p.is_extended else "")


def mib(sectors: int, disk: Disk) -> str:
    return format_size(sectors * disk.sector_size)


__all__ = ["CliError", "MiB"]
