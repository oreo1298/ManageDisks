"""Pending operations.

An operation is a small serialisable object that

* ``apply(disks)`` – validates itself against a *preview* model and mutates
  it, so queued operations can be shown before anything touches a disk;
* ``execute(ctx)`` – performs the real work with external tools.

Partitions are identified by ``(disk, start sector)`` instead of partition
numbers: numbers can shift (MBR logical partitions) while the start of a
partition at a given point in the queue is unambiguous.
"""

from __future__ import annotations

import os
import struct
from dataclasses import asdict, dataclass, field, fields
from typing import ClassVar

from . import filesystems as fsmod
from . import parttypes
from .filesystems import BY_ID, Filesystem, lookup
from .model import Disk, FreeSpace, Partition, estimate_usable, find_disk
from .units import MiB, format_size


class OpError(Exception):
    """An operation is not possible (raised during validation/preview)."""


REGISTRY: dict[str, type["Operation"]] = {}


def signature(fs: Filesystem | None) -> tuple[str, str]:
    """(lsblk fstype, version) a freshly created ``fs`` will report."""
    if fs is None or fs.id == "cleared":
        return "", ""
    if fs.id.startswith("fat"):
        return "vfat", fs.id.upper()
    if fs.id == "linux-swap":
        return "swap", ""
    if fs.id == "luks":
        return "crypto_LUKS", "2"
    return fs.id, ""


def _disk(disks: list[Disk], path: str) -> Disk:
    d = find_disk(disks, path)
    if d is None:
        raise OpError(f"Disk {path} not found")
    return d


def _part(disk: Disk, start: int) -> Partition:
    p = disk.find(start)
    if p is None:
        raise OpError(f"No partition starts at sector {start} on {disk.path}")
    return p


def _require_table(disk: Disk) -> None:
    if not disk.table:
        raise OpError(f"{disk.path} has no partition table. Create one first.")
    if disk.read_only:
        raise OpError(f"{disk.path} is read-only")


def _require_idle(p: Partition, action: str) -> None:
    if p.is_swap_active:
        raise OpError(f"{p.display_name()} is in use as swap. Turn swap off before you {action}.")
    if p.all_mountpoints():
        raise OpError(f"{p.display_name()} is mounted at {', '.join(p.all_mountpoints())}. "
                      f"Unmount it before you {action}.")
    if p.holders:
        names = ", ".join(h.get("name", "?") for h in p.holders)
        raise OpError(f"{p.display_name()} is in use by {names}. Close it before you {action}.")


def _fs_of(p: Partition) -> Filesystem | None:
    return lookup(p.fstype, p.fsversion)


def _check_fs_size(fs: Filesystem | None, size_bytes: int) -> None:
    if fs is None:
        return
    if size_bytes < fs.min_size:
        raise OpError(f"{fs.name} needs at least {format_size(fs.min_size)}")
    if fs.max_size and size_bytes > fs.max_size:
        raise OpError(f"{fs.name} can be at most {format_size(fs.max_size)}")


def _check_label(fs: Filesystem | None, label: str) -> str:
    if not label:
        return ""
    if fs is None or fs.id == "cleared":
        raise OpError("A label needs a filesystem")
    label = fs.normalize_label(label)
    err = fs.validate_label(label)
    if err:
        raise OpError(err)
    return label


def _check_name(disk: Disk, name: str) -> None:
    if name and disk.table != "gpt":
        raise OpError("Partition names are only supported on GPT disks")
    if '"' in name or "\n" in name or "\\" in name:
        raise OpError("Partition names cannot contain quotes, backslashes or newlines")
    if len(name) > 36:
        raise OpError("GPT partition names can be at most 36 characters")


def _region_free(disk: Disk, start: int, size: int, kind: str) -> None:
    """Raise unless [start, start+size) is free and legal for a partition of ``kind``."""
    if size <= 0:
        raise OpError("The partition size must be positive")
    end = start + size - 1
    if start < disk.first_usable or end > disk.last_usable:
        raise OpError(f"The partition must lie between sectors {disk.first_usable} and {disk.last_usable}")
    ext = disk.extended()
    if kind == "logical":
        if ext is None:
            raise OpError("Logical partitions need an extended partition")
        if start <= ext.start or end > ext.end:
            raise OpError("Logical partitions must lie inside the extended partition")
        others = disk.logicals()
    else:
        others = disk.top_level()
        if ext and kind != "logical" and not (end < ext.start or start > ext.end):
            raise OpError("The new partition overlaps the extended partition")
    for p in others:
        gap = 1 if kind == "logical" else 0  # an EBR precedes each logical partition
        if not (end < p.start - gap or start > p.end + gap):
            raise OpError(f"The new partition overlaps {p.display_name()}")


def _ts() -> str:
    import time
    return time.strftime("%Y%m%d-%H%M%S")


@dataclass
class Operation:
    disk: str
    op: ClassVar[str] = ""
    destructive: ClassVar[bool] = False
    secret_fields: ClassVar[tuple[str, ...]] = ()

    def __init_subclass__(cls, **kw):
        super().__init_subclass__(**kw)
        if cls.op:
            REGISTRY[cls.op] = cls

    # -- serialisation ----------------------------------------------------
    def to_dict(self) -> dict:
        d = asdict(self)
        d["op"] = self.op
        return d

    @staticmethod
    def from_dict(d: dict) -> "Operation":
        d = dict(d)
        cls = REGISTRY.get(d.pop("op", ""))
        if cls is None:
            raise OpError("Unknown operation")
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in names})

    # -- interface ----------------------------------------------------------
    def describe(self) -> str:
        raise NotImplementedError

    def disks(self) -> list[str]:
        return [self.disk]

    def apply(self, disks: list[Disk]) -> None:
        raise NotImplementedError

    def execute(self, ctx) -> None:
        raise NotImplementedError

    def warnings(self, disks: list[Disk]) -> list[str]:
        return []


# ---------------------------------------------------------------------------
@dataclass
class CreateTable(Operation):
    table: str = "gpt"
    op: ClassVar[str] = "create_table"
    destructive: ClassVar[bool] = True

    def describe(self):
        return f"Create a new {parttypes.table_label(self.table)} partition table on {self.disk} (erases everything)"

    def apply(self, disks):
        disk = _disk(disks, self.disk)
        if self.table not in ("gpt", "dos"):
            raise OpError("The partition table must be GPT or MBR")
        if disk.read_only:
            raise OpError(f"{disk.path} is read-only")
        if disk.busy:
            raise OpError(f"{disk.path} is in use. Unmount all of its partitions first.")
        if self.table == "dos" and disk.sectors > 0xFFFFFFFF:
            raise OpError(f"MBR cannot address disks larger than {format_size(0xFFFFFFFF * disk.sector_size)}; use GPT")
        disk.partitions = []
        disk.table = self.table
        disk.table_uuid = ""
        disk.fstype = disk.label = disk.fsuuid = ""
        disk.fs_size = disk.fs_used = None
        disk.first_usable, disk.last_usable = estimate_usable(self.table, disk.sectors, disk.sector_size)
        disk.pending = ["table"]

    def execute(self, ctx):
        ctx.step("Removing old signatures")
        ctx.run(["wipefs", "-a", "-f", self.disk])
        ctx.step(f"Writing {parttypes.table_label(self.table)} partition table")
        ctx.sfdisk(self.disk, [], script=f"label: {self.table}\n", wipe="always")


@dataclass
class CreatePartition(Operation):
    start: int = 0
    size: int = 0
    kind: str = "normal"        # normal | primary | extended | logical
    fstype: str = "ext4"        # Filesystem id, "" for none
    label: str = ""
    name: str = ""              # GPT name
    type_id: str = ""           # empty: derive from filesystem
    encrypt: bool = False
    passphrase: str = ""
    op: ClassVar[str] = "create_partition"
    secret_fields: ClassVar[tuple[str, ...]] = ("passphrase",)

    def describe(self):
        what = "extended partition" if self.kind == "extended" else (
            f"{BY_ID[self.fstype].name if self.fstype in BY_ID else 'unformatted'}"
            f"{' (encrypted)' if self.encrypt else ''} {'logical ' if self.kind == 'logical' else ''}partition")
        lbl = f" “{self.label}”" if self.label else ""
        return f"Create {self._size_text()} {what}{lbl} on {self.disk}{self._where()}"

    def _size_text(self):
        return f"{self.size}-sector" if not hasattr(self, "_ss") else format_size(self.size * self._ss)

    def _where(self):
        return f" at sector {self.start}" if not hasattr(self, "_ss") else \
            f" at {format_size(self.start * self._ss)}"

    def apply(self, disks):
        disk = _disk(disks, self.disk)
        self._ss = disk.sector_size
        _require_table(disk)
        kind = self.kind
        if disk.table == "gpt":
            if kind not in ("normal", "primary"):
                raise OpError("GPT disks do not use extended or logical partitions")
            kind = "normal"
            if len(disk.partitions) >= 128:
                raise OpError("A GPT disk can hold at most 128 partitions")
        else:
            if kind == "normal":
                kind = "primary"
            if kind in ("primary", "extended") and disk.primary_count() >= 4:
                raise OpError("An MBR disk can hold at most 4 primary partitions. "
                              "Create an extended partition to hold more (logical) partitions.")
            if kind == "extended" and disk.extended():
                raise OpError("An MBR disk can have only one extended partition")
            if (self.start + self.size) > 0xFFFFFFFF:
                raise OpError("MBR partitions cannot extend beyond 2 TiB (with 512-byte sectors)")
        _region_free(disk, self.start, self.size, kind)
        fs = None
        if kind != "extended" and self.fstype:
            fs = BY_ID.get(self.fstype)
            if fs is None or not fs.implemented("create") or fs.id == "luks":
                raise OpError(f"Cannot create a {self.fstype} filesystem")
            _check_fs_size(fs, self.size * disk.sector_size)
        if self.encrypt:
            if fs is None:
                raise OpError("Choose a filesystem to put inside the encrypted volume")
            if len(self.passphrase) < 1:
                raise OpError("Encryption needs a passphrase")
            _check_fs_size(BY_ID["luks"], self.size * disk.sector_size)
        label = _check_label(fs, self.label) if fs else ""
        _check_name(disk, self.name)
        if kind == "extended":
            type_id = "05"
        elif self.type_id:
            try:
                type_id = parttypes.resolve(self.type_id, disk.table)
            except ValueError as e:
                raise OpError(str(e)) from None
        else:
            type_id = parttypes.default_type_for_fs(self.fstype or "linux", disk.table)
        number = disk.next_number(kind)
        if number == 0:
            raise OpError("No free partition number left on this disk")
        fstype, fsver = signature(fs)
        p = Partition(number=number, start=self.start, size=self.size, kind=kind, type_id=type_id,
                      name=self.name, label=label, pending=["new"])
        if self.encrypt:
            p.fstype, p.fsversion = "crypto_LUKS", "2"
            p.holders = [{"name": "(encrypted)", "type": "crypt", "fstype": fstype, "label": label,
                          "mountpoints": [], "virtual": True}]
            p.label = ""
        else:
            p.fstype, p.fsversion = fstype, fsver
            p.fs_size = self.size * disk.sector_size if fs else None
        disk.partitions.append(p)

    def execute(self, ctx):
        disk = ctx.disk(self.disk)
        table = disk.table
        type_id = ("05" if self.kind == "extended" else
                   parttypes.resolve(self.type_id, table) if self.type_id else
                   parttypes.default_type_for_fs(self.fstype or "linux", table))
        line = f"start={self.start}, size={self.size}, type={type_id}"
        if table == "gpt" and self.name:
            line += f', name="{self.name}"'
        ctx.step("Creating partition")
        ctx.sfdisk(self.disk, ["--append"], script=line + "\n")
        if self.kind == "extended":
            return
        path = ctx.locate(self.disk, self.start)
        ctx.run(["wipefs", "-a", path])
        if self.fstype:
            make_filesystem(ctx, path, self.fstype, self.label, self.encrypt, self.passphrase)


def make_filesystem(ctx, path: str, fstype: str, label: str, encrypt: bool = False,
                    passphrase: str = "") -> None:
    fs = BY_ID[fstype]
    if not encrypt:
        ctx.step(f"Creating {fs.name} filesystem on {path}")
        fs.mkfs(ctx.runner, path, label)
        return
    luks: fsmod.Luks = BY_ID["luks"]  # type: ignore[assignment]
    ctx.step(f"Encrypting {path} (LUKS2)")
    luks.format(ctx.runner, path, passphrase)
    name = f"managedisks-{os.path.basename(path)}"
    luks.open(ctx.runner, path, passphrase, name)
    try:
        ctx.step(f"Creating {fs.name} filesystem inside the encrypted volume")
        fs.mkfs(ctx.runner, f"/dev/mapper/{name}", label)
    finally:
        ctx.run(["cryptsetup", "close", name], check=False)


@dataclass
class DeletePartition(Operation):
    start: int = 0
    op: ClassVar[str] = "delete_partition"
    destructive: ClassVar[bool] = True

    def describe(self):
        return f"Delete partition at sector {self.start} on {self.disk}" + (
            f" ({self._name})" if getattr(self, "_name", "") else "")

    def apply(self, disks):
        disk = _disk(disks, self.disk)
        _require_table(disk)
        p = _part(disk, self.start)
        self._name = p.display_name()
        if p.is_extended and disk.logicals():
            raise OpError("Delete the logical partitions inside the extended partition first")
        _require_idle(p, "delete it")
        disk.partitions.remove(p)
        if p.is_logical:
            for q in disk.logicals():
                if q.number > p.number:
                    q.number -= 1

    def execute(self, ctx):
        num = ctx.number(self.disk, self.start)
        ctx.step(f"Deleting partition {num}")
        ctx.sfdisk(self.disk, ["--delete", self.disk, str(num)], device_arg=False)


@dataclass
class FormatPartition(Operation):
    start: int = 0
    fstype: str = "ext4"
    label: str = ""
    encrypt: bool = False
    passphrase: str = ""
    op: ClassVar[str] = "format"
    destructive: ClassVar[bool] = True
    secret_fields: ClassVar[tuple[str, ...]] = ("passphrase",)

    def describe(self):
        name = BY_ID[self.fstype].name if self.fstype in BY_ID else self.fstype
        enc = " (encrypted)" if self.encrypt else ""
        return f"Format {getattr(self, '_name', f'partition at sector {self.start}')} as {name}{enc}" + (
            f" with label “{self.label}”" if self.label else "")

    def apply(self, disks):
        disk = _disk(disks, self.disk)
        _require_table(disk)
        p = _part(disk, self.start)
        self._name = p.display_name()
        if p.is_extended:
            raise OpError("Extended partitions cannot be formatted")
        _require_idle(p, "format it")
        fs = BY_ID.get(self.fstype)
        if fs is None or fs.id == "luks":
            raise OpError(f"Unknown filesystem {self.fstype}")
        _check_fs_size(fs, p.size * disk.sector_size)
        if self.encrypt:
            if fs.id == "cleared":
                raise OpError("Choose a filesystem to put inside the encrypted volume")
            if not self.passphrase:
                raise OpError("Encryption needs a passphrase")
        label = _check_label(fs, self.label) if fs.id != "cleared" else ""
        fstype, fsver = signature(fs)
        if self.encrypt:
            p.fstype, p.fsversion, p.label = "crypto_LUKS", "2", ""
            p.holders = [{"name": "(encrypted)", "type": "crypt", "fstype": fstype, "label": label,
                          "mountpoints": [], "virtual": True}]
        else:
            p.fstype, p.fsversion, p.label = fstype, fsver, label
            p.holders = []
        p.fsuuid = ""
        p.fs_used = None
        p.fs_size = p.size * disk.sector_size if fstype else None
        new_type = _auto_type(disk, p, self.fstype)
        if new_type:
            p.type_id = new_type
        if "format" not in p.pending:
            p.pending.append("format")

    def execute(self, ctx):
        disk = ctx.disk(self.disk)
        path = ctx.locate(self.disk, self.start)
        ctx.step(f"Removing old signatures from {path}")
        ctx.run(["wipefs", "-a", path])
        if self.fstype != "cleared":
            make_filesystem(ctx, path, self.fstype, self.label, self.encrypt, self.passphrase)
        live = disk.find(self.start)
        if live:
            new_type = _auto_type(disk, live, self.fstype)
            if new_type:
                ctx.step("Updating partition type")
                ctx.sfdisk(self.disk, ["--part-type", self.disk, str(live.number), new_type], device_arg=False)


def _auto_type(disk: Disk, p: Partition, fstype: str) -> str | None:
    """New partition type when reformatting a generic data partition."""
    if fstype in ("", "cleared") or not disk.table:
        return None
    if parttypes.normalize_type(p.type_id, disk.table) not in parttypes.GENERIC_TYPES:
        return None
    want = parttypes.default_type_for_fs(fstype, disk.table)
    return None if want == p.type_id else want


@dataclass
class ResizeMovePartition(Operation):
    start: int = 0
    new_start: int = 0
    new_size: int = 0
    op: ClassVar[str] = "resize"

    def describe(self):
        old = getattr(self, "_old", None)
        name = getattr(self, "_name", f"partition at sector {self.start}")
        ss = getattr(self, "_ss", 512)
        parts = []
        if old and old[1] != self.new_size:
            parts.append(f"{'grow' if self.new_size > old[1] else 'shrink'} "
                         f"{format_size(old[1] * ss)} → {format_size(self.new_size * ss)}")
        if self.new_start != self.start:
            delta = (self.new_start - self.start) * ss
            parts.append(f"move {'right' if delta > 0 else 'left'} by {format_size(abs(delta))}")
        what = " and ".join(parts) or "resize"
        return f"Resize {name}: {what}"

    def apply(self, disks):
        disk = _disk(disks, self.disk)
        _require_table(disk)
        p = _part(disk, self.start)
        self._name = p.display_name()
        self._old = (p.start, p.size)
        self._ss = disk.sector_size
        if self.new_size <= 0:
            raise OpError("The new size must be positive")
        if self.new_start == p.start and self.new_size == p.size:
            raise OpError("The partition already has this size and position")
        lo, hi = disk.neighbours(p)
        new_end = self.new_start + self.new_size - 1
        if self.new_start < lo or new_end > hi:
            raise OpError("Not enough free space next to the partition for this size/position")
        moved = self.new_start != p.start
        grow = self.new_size > p.size
        shrink = self.new_size < p.size
        if p.is_extended:
            lg = disk.logicals()
            if lg and (self.new_start >= lg[0].start or new_end < lg[-1].end):
                raise OpError("The extended partition must keep containing all logical partitions")
            p.start, p.size = self.new_start, self.new_size
            p.pending.append("resized")
            return
        if disk.table == "dos" and new_end > 0xFFFFFFFF:
            raise OpError("MBR partitions cannot extend beyond 2 TiB")
        fs = _fs_of(p)
        if p.fstype and fs is None:
            if shrink or grow:
                raise OpError(f"Resizing {fsmod.display_name(p.fstype)} is not supported "
                              "(the data would not follow). Only moving is possible.")
        if p.fstype == "crypto_LUKS" and (shrink or grow):
            raise OpError("Resizing encrypted (LUKS) partitions is not supported yet")
        if fs and fs.id != "cleared":
            if grow and not fs.implemented("grow"):
                raise OpError(f"{fs.name} filesystems cannot be grown")
            if shrink and not fs.implemented("shrink"):
                raise OpError(f"{fs.name} filesystems cannot be shrunk")
            if grow and not fs.supports("grow"):
                raise OpError(f"Growing {fs.name} needs: {', '.join(fs.missing_tools('grow'))}")
            if shrink and not fs.supports("shrink"):
                raise OpError(f"Shrinking {fs.name} needs: {', '.join(fs.missing_tools('shrink'))}")
            _check_fs_size(fs, self.new_size * disk.sector_size)
            if shrink and p.fs_used is not None and p.fs_used > self.new_size * disk.sector_size:
                raise OpError(f"The filesystem holds {format_size(p.fs_used)} of data; "
                              "the new size is too small")
        if moved and fs and not fs.movable:
            raise OpError(f"{fs.name} partitions cannot be moved")
        if p.busy:
            if moved:
                _require_idle(p, "move it")
            if p.is_swap_active or p.holders:
                _require_idle(p, "resize it")
            if grow and not (fs and fs.grow_online):
                _require_idle(p, "grow it")
            if shrink and not (fs and fs.shrink_online):
                _require_idle(p, "shrink it")
        p.start, p.size = self.new_start, self.new_size
        if p.fs_size is not None:
            p.fs_size = self.new_size * disk.sector_size
        p.pending.append("moved" if moved else "resized")

    def execute(self, ctx):
        disk = ctx.disk(self.disk)
        live = disk.find(self.start)
        if live is None:
            raise OpError(f"Partition at sector {self.start} disappeared")
        num = live.number
        path = ctx.locate(self.disk, self.start)
        ss = disk.sector_size
        old_size = live.size
        fs = _fs_of(live)
        if fs and fs.id == "cleared":
            fs = None
        mnt = next((m for m in live.mountpoints if m and m != "[SWAP]"), None)
        if live.is_extended:
            ctx.step("Resizing extended partition")
            ctx.sfdisk(self.disk, ["-N", str(num)], script=f"start={self.new_start}, size={self.new_size}\n")
            return
        size = old_size
        start = self.start
        if self.new_size < old_size:
            if fs and fs.id != "linux-swap":
                ctx.step(f"Shrinking {fs.name} filesystem to {format_size(self.new_size * ss)}")
                fs.shrink(ctx.runner, path, self.new_size * ss, mnt)
            ctx.step("Shrinking partition")
            ctx.sfdisk(self.disk, ["-N", str(num)], script=f"start={start}, size={self.new_size}\n")
            size = self.new_size
        if self.new_start != start:
            ctx.step(f"Moving partition data ({format_size(size * ss)})")
            ctx.move(self.disk, num, self.new_start, size)
            start = self.new_start
            path = ctx.locate(self.disk, start)
            if live.fstype in ("ntfs", "vfat"):
                ctx.step("Updating boot sector")
                fix_boot_sector(ctx, path, live.fstype, start, ss)
        if self.new_size > size:
            ctx.step("Growing partition")
            ctx.sfdisk(self.disk, ["-N", str(num)], script=f"start={start}, size={self.new_size}\n")
            path = ctx.locate(self.disk, start)
            if fs and fs.id != "linux-swap":
                ctx.step(f"Growing {fs.name} filesystem")
                fs.grow(ctx.runner, path, self.new_size * ss, mnt)
        if fs and fs.id == "linux-swap" and self.new_size != old_size:
            ctx.step("Recreating swap signature")
            fs.grow(ctx.runner, path, self.new_size * ss)


def fix_boot_sector(ctx, path: str, fstype: str, start: int, sector_size: int) -> None:
    """Update the "hidden sectors" field of NTFS/FAT boot sectors after a move,
    so the volume stays bootable (Windows uses it to locate itself)."""
    if ctx.dry_run:
        ctx.log(f"(dry run) would update hidden sectors of {path} to {start}")
        return
    with open(path, "r+b") as f:
        bs = f.read(512)
        if len(bs) < 512 or bs[510:512] != b"\x55\xaa":
            ctx.log("No boot sector signature; leaving it alone")
            return
        bps = struct.unpack_from("<H", bs, 0x0B)[0] or 512
        hidden = start * sector_size // bps
        if hidden > 0xFFFFFFFF:
            ctx.log("Partition start too large for the boot sector field; skipped")
            return
        targets = [0]
        if fstype == "ntfs" and bs[3:11] == b"NTFS    ":
            total = struct.unpack_from("<Q", bs, 0x28)[0]
            targets.append(total * bps)
        elif fstype == "vfat" and bs[0x52:0x5A] == b"FAT32   ":
            backup = struct.unpack_from("<H", bs, 0x32)[0]
            if 0 < backup < 32:
                targets.append(backup * bps)
        elif fstype == "vfat" and bs[0x36:0x3B] in (b"FAT16", b"FAT12"):
            pass
        else:
            ctx.log("Unrecognised boot sector; skipped")
            return
        for off in targets:
            f.seek(off)
            sec = f.read(512)
            if len(sec) == 512 and sec[510:512] == b"\x55\xaa":
                f.seek(off + 0x1C)
                f.write(struct.pack("<I", hidden))
        f.flush()
        os.fsync(f.fileno())
    ctx.log(f"Hidden sectors set to {hidden}")


@dataclass
class SetLabel(Operation):
    start: int = 0
    label: str = ""
    op: ClassVar[str] = "set_label"

    def describe(self):
        target = getattr(self, "_name", f"partition at sector {self.start}")
        return f"Set filesystem label of {target} to “{self.label}”" if self.label else \
            f"Remove filesystem label of {target}"

    def apply(self, disks):
        disk = _disk(disks, self.disk)
        p = _part(disk, self.start)
        self._name = p.display_name()
        fs = _fs_of(p)
        if fs is None or fs.id == "cleared" or not fs.implemented("label"):
            raise OpError("This partition has no filesystem whose label can be changed")
        if not fs.supports("label"):
            raise OpError(f"Changing {fs.name} labels needs: {', '.join(fs.missing_tools('label'))}")
        if p.busy and not fs.label_online:
            _require_idle(p, "change its label")
        self.label = _check_label(fs, self.label) if self.label else ""
        p.label = self.label
        p.pending.append("label")

    def execute(self, ctx):
        disk = ctx.disk(self.disk)
        live = _part(disk, self.start)
        fs = _fs_of(live)
        if fs is None:
            raise OpError("No filesystem found")
        path = ctx.locate(self.disk, self.start)
        mnt = next((m for m in live.mountpoints if m and m != "[SWAP]"), None)
        ctx.step(f"Setting label on {path}")
        fs.set_label(ctx.runner, path, self.label, mnt)


@dataclass
class NewUUID(Operation):
    start: int = 0
    op: ClassVar[str] = "new_uuid"

    def describe(self):
        return f"Give {getattr(self, '_name', f'partition at sector {self.start}')} a new filesystem UUID"

    def apply(self, disks):
        disk = _disk(disks, self.disk)
        p = _part(disk, self.start)
        self._name = p.display_name()
        fs = _fs_of(p)
        if fs is None or not fs.implemented("uuid"):
            raise OpError("The filesystem UUID of this partition cannot be changed")
        if not fs.supports("uuid"):
            raise OpError(f"Changing {fs.name} UUIDs needs: {', '.join(fs.missing_tools('uuid'))}")
        _require_idle(p, "change its UUID")
        p.fsuuid = "(new)"
        p.pending.append("uuid")

    def warnings(self, disks):
        return ["Entries in /etc/fstab or boot loader configuration that use the old UUID must be updated."]

    def execute(self, ctx):
        disk = ctx.disk(self.disk)
        fs = _fs_of(_part(disk, self.start))
        if fs is None:
            raise OpError("No filesystem found")
        path = ctx.locate(self.disk, self.start)
        ctx.step(f"Generating new UUID for {path}")
        fs.set_uuid(ctx.runner, path)


@dataclass
class SetPartitionProps(Operation):
    start: int = 0
    type_id: str | None = None
    name: str | None = None
    attrs: list[str] | None = None
    bootable: bool | None = None
    op: ClassVar[str] = "set_props"

    def describe(self):
        bits = []
        if self.type_id is not None:
            bits.append(f"type “{parttypes.type_name(self.type_id, getattr(self, '_table', 'gpt'))}”")
        if self.name is not None:
            bits.append(f"name “{self.name}”")
        if self.attrs is not None:
            bits.append("attributes " + (", ".join(self.attrs) or "none"))
        if self.bootable is not None:
            bits.append("bootable flag " + ("on" if self.bootable else "off"))
        return f"Set {' and '.join(bits)} on {getattr(self, '_name', f'partition at sector {self.start}')}"

    def apply(self, disks):
        disk = _disk(disks, self.disk)
        _require_table(disk)
        p = _part(disk, self.start)
        self._name = p.display_name()
        self._table = disk.table
        if p.is_extended and self.type_id is not None:
            raise OpError("The type of an extended partition cannot be changed")
        if self.type_id is not None:
            try:
                self.type_id = parttypes.resolve(self.type_id, disk.table)
            except ValueError as e:
                raise OpError(str(e)) from None
            if disk.table == "dos" and self.type_id in parttypes.EXTENDED_MBR_CODES:
                raise OpError("Cannot turn a partition into an extended partition")
            p.type_id = self.type_id
        if self.name is not None:
            _check_name(disk, self.name)
            p.name = self.name
        if self.attrs is not None:
            if disk.table != "gpt":
                raise OpError("Partition attributes are only supported on GPT disks")
            p.attrs = list(self.attrs)
        if self.bootable is not None:
            if disk.table != "dos":
                raise OpError("The bootable flag only exists on MBR disks (use the EFI System type on GPT)")
            if p.is_extended or p.is_logical:
                raise OpError("Only primary partitions can be marked bootable")
            p.bootable = self.bootable
        p.pending.append("props")

    def execute(self, ctx):
        disk = ctx.disk(self.disk)
        live = _part(disk, self.start)
        n = str(live.number)
        if self.type_id is not None:
            ctx.step("Setting partition type")
            ctx.sfdisk(self.disk, ["--part-type", self.disk, n, self.type_id], device_arg=False)
        if self.name is not None:
            ctx.step("Setting partition name")
            ctx.sfdisk(self.disk, ["--part-label", self.disk, n, self.name], device_arg=False)
        if self.attrs is not None:
            ctx.step("Setting partition attributes")
            ctx.sfdisk(self.disk, ["--part-attrs", self.disk, n, ",".join(self.attrs)], device_arg=False)
        if self.bootable is not None:
            ctx.step("Setting bootable flag")
            active = {p.number for p in disk.partitions if p.bootable}
            if self.bootable:
                active.add(live.number)
            else:
                active.discard(live.number)
            args = [str(x) for x in sorted(active)] or ["-"]
            ctx.sfdisk(self.disk, ["--activate", self.disk] + args, device_arg=False)


@dataclass
class CheckFilesystem(Operation):
    start: int = 0
    repair: bool = True
    op: ClassVar[str] = "check"

    def describe(self):
        return f"Check {'and repair ' if self.repair else ''}filesystem on " \
               f"{getattr(self, '_name', f'partition at sector {self.start}')}"

    def apply(self, disks):
        disk = _disk(disks, self.disk)
        p = _part(disk, self.start)
        self._name = p.display_name()
        if p.is_new:
            raise OpError("Apply the pending changes before checking a new partition")
        fs = _fs_of(p)
        if fs is None or not fs.implemented("check"):
            raise OpError("This filesystem cannot be checked")
        if not fs.supports("check"):
            raise OpError(f"Checking {fs.name} needs: {', '.join(fs.missing_tools('check'))}")
        if p.busy and fs.id != "btrfs":
            _require_idle(p, "check it")

    def execute(self, ctx):
        disk = ctx.disk(self.disk)
        live = _part(disk, self.start)
        fs = _fs_of(live)
        if fs is None:
            raise OpError("No filesystem found")
        path = ctx.locate(self.disk, self.start)
        mnt = next((m for m in live.mountpoints if m and m != "[SWAP]"), None)
        ctx.step(f"Checking {fs.name} filesystem on {path}")
        fs.check(ctx.runner, path, self.repair, mnt)


@dataclass
class CopyPartition(Operation):
    start: int = 0              # source partition start on ``disk``
    dest_disk: str = ""
    dest_start: int = 0
    dest_size: int = 0
    new_uuid: bool = True
    op: ClassVar[str] = "copy"

    def disks(self):
        return [self.disk, self.dest_disk]

    def describe(self):
        ss = getattr(self, "_ss", None)
        where = f"at {format_size(self.dest_start * ss)}" if ss else f"at sector {self.dest_start}"
        return f"Copy {getattr(self, '_name', f'partition at sector {self.start}')} to {self.dest_disk} {where}"

    def apply(self, disks):
        src_disk = _disk(disks, self.disk)
        dst = _disk(disks, self.dest_disk or self.disk)
        self.dest_disk = dst.path
        _require_table(dst)
        src = _part(src_disk, self.start)
        self._name = src.display_name()
        self._ss = dst.sector_size
        if src.is_extended:
            raise OpError("Extended partitions cannot be copied")
        _require_idle(src, "copy it")
        if src_disk.sector_size != dst.sector_size:
            raise OpError("Source and destination disks use different sector sizes")
        if self.dest_size < src.size:
            raise OpError(f"The copy needs at least {format_size(src.size * src_disk.sector_size)}")
        kind = "normal" if dst.table == "gpt" else "primary"
        ext = dst.extended()
        if dst.table == "dos" and ext and ext.start < self.dest_start <= ext.end:
            kind = "logical"
        if dst.table == "dos" and kind == "primary" and dst.primary_count() >= 4:
            raise OpError("The destination disk already has 4 primary partitions")
        _region_free(dst, self.dest_start, self.dest_size, kind)
        fs = _fs_of(src)
        if self.dest_size > src.size and fs and fs.id != "cleared" and not fs.supports("grow"):
            raise OpError(f"{fs.name} cannot be grown to fill the larger destination; "
                          "make the destination the same size as the source")
        type_id = src.type_id
        if src_disk.table != dst.table:
            type_id = (parttypes.mbr_to_gpt(type_id) if dst.table == "gpt" else parttypes.gpt_to_mbr(type_id))
        p = Partition(number=dst.next_number(kind), start=self.dest_start, size=self.dest_size, kind=kind,
                      type_id=type_id, name=src.name if dst.table == "gpt" else "", fstype=src.fstype,
                      fsversion=src.fsversion, label=src.label,
                      fsuuid="(new)" if self.new_uuid else src.fsuuid,
                      fs_size=self.dest_size * dst.sector_size if src.fs_size else None,
                      fs_used=src.fs_used, pending=["new", "copy"])
        dst.partitions.append(p)

    def execute(self, ctx):
        src_disk = ctx.disk(self.disk)
        src = _part(src_disk, self.start)
        dst = ctx.disk(self.dest_disk)
        src_path = ctx.locate(self.disk, self.start)
        type_id = src.type_id
        if src_disk.table != dst.table:
            type_id = (parttypes.mbr_to_gpt(type_id) if dst.table == "gpt" else parttypes.gpt_to_mbr(type_id))
        line = f"start={self.dest_start}, size={self.dest_size}, type={type_id}"
        if dst.table == "gpt" and src.name:
            line += f', name="{src.name}"'
        ctx.step("Creating destination partition")
        ctx.sfdisk(self.dest_disk, ["--append"], script=line + "\n")
        dst_path = ctx.locate(self.dest_disk, self.dest_start)
        nbytes = src.size * src_disk.sector_size
        ctx.step(f"Copying {format_size(nbytes)} from {src_path} to {dst_path}")
        ctx.copy(src_path, dst_path, nbytes)
        fs = _fs_of(src)
        if src.fstype in ("ntfs", "vfat"):
            fix_boot_sector(ctx, dst_path, src.fstype, self.dest_start, dst.sector_size)
        if fs and self.new_uuid and fs.supports("uuid"):
            ctx.step("Assigning a new UUID to the copy")
            fs.set_uuid(ctx.runner, dst_path)
        if fs and self.dest_size > src.size and fs.supports("grow"):
            ctx.step(f"Growing the copied {fs.name} filesystem")
            fs.grow(ctx.runner, dst_path, self.dest_size * dst.sector_size)


WIPE_METHODS = {
    "quick": "Quick: remove partition table and filesystem signatures",
    "zero": "Overwrite everything with zeros",
    "random": "Overwrite everything with random data",
    "discard": "Discard all blocks (SSD/NVMe TRIM, very fast)",
}


@dataclass
class Wipe(Operation):
    start: int | None = None     # None: whole disk
    method: str = "quick"
    op: ClassVar[str] = "wipe"
    destructive: ClassVar[bool] = True

    def describe(self):
        what = getattr(self, "_name", None) or (self.disk if self.start is None else f"partition at sector {self.start}")
        return f"Wipe {what} ({WIPE_METHODS.get(self.method, self.method).split(':')[0].lower()})"

    def apply(self, disks):
        disk = _disk(disks, self.disk)
        if self.method not in WIPE_METHODS:
            raise OpError(f"Unknown wipe method {self.method}")
        if disk.read_only:
            raise OpError(f"{disk.path} is read-only")
        if self.start is None:
            if disk.busy:
                raise OpError(f"{disk.path} is in use. Unmount all of its partitions first.")
            self._name = disk.path
            disk.partitions = []
            disk.table = None
            disk.fstype = disk.label = disk.fsuuid = ""
            disk.pending = ["wiped"]
            return
        p = _part(disk, self.start)
        self._name = p.display_name()
        if p.is_extended:
            raise OpError("Extended partitions cannot be wiped; wipe the logical partitions instead")
        _require_idle(p, "wipe it")
        p.fstype = p.fsversion = p.label = p.fsuuid = ""
        p.fs_size = p.fs_used = None
        p.holders = []
        p.pending.append("wiped")

    def execute(self, ctx):
        if self.start is None:
            disk = ctx.disk(self.disk)
            target = self.disk
            nbytes = disk.size
            if self.method == "quick":
                for p in disk.partitions:
                    if not p.is_extended and p.path:
                        ctx.run(["wipefs", "-a", p.path], check=False)
        else:
            target = ctx.locate(self.disk, self.start)
            disk = ctx.disk(self.disk)
            nbytes = _part(disk, self.start).size * disk.sector_size
        ctx.step(f"Wiping {target}")
        if self.method == "quick":
            ctx.run(["wipefs", "-a", "-f", target])
            ctx.zero_range(target, 0, min(MiB, nbytes))
            if self.start is None and nbytes > 2 * MiB:
                ctx.zero_range(target, nbytes - MiB, MiB)
        elif self.method == "discard":
            ctx.run(["blkdiscard", "-f", target])
        else:
            ctx.fill(target, nbytes, random_data=self.method == "random")
        if self.start is None:
            ctx.sync_kernel(self.disk)


@dataclass
class ConvertTable(Operation):
    to: str = "gpt"
    op: ClassVar[str] = "convert"

    def describe(self):
        return f"Convert {self.disk} to {parttypes.table_label(self.to)} (keeps partitions and data)"

    def warnings(self, disks):
        w = ["Converting the partition table can make an installed operating system unbootable. "
             "Make sure you have a backup."]
        if self.to == "gpt":
            w.append("Booting from a GPT disk needs UEFI, or a ‘BIOS boot’ partition for GRUB on BIOS systems.")
        else:
            w.append("MBR disks can only be booted in legacy BIOS mode (or UEFI via an EFI partition).")
        return w

    def apply(self, disks):
        disk = _disk(disks, self.disk)
        if self.to not in ("gpt", "dos"):
            raise OpError("Target must be GPT or MBR")
        if not disk.table:
            raise OpError(f"{disk.path} has no partition table; create one instead")
        if disk.table == self.to:
            raise OpError(f"{disk.path} already uses {parttypes.table_label(self.to)}")
        if disk.busy:
            raise OpError(f"{disk.path} is in use. Unmount all of its partitions first.")
        if any(p.is_new for p in disk.partitions):
            raise OpError("Apply pending changes to this disk before converting it")
        first, last = estimate_usable(self.to, disk.sectors, disk.sector_size)
        parts = [p for p in disk.sorted_partitions() if not p.is_extended]
        if self.to == "gpt":
            min_first = 2 + -(-(128 * 128) // disk.sector_size)
            if parts and parts[0].start < min_first:
                raise OpError("The first partition starts too early to make room for a GPT header")
            if parts and parts[-1].end > last:
                need = (parts[-1].end - last) * disk.sector_size
                raise OpError(f"The last partition reaches the end of the disk; shrink it by at least "
                              f"{format_size(need)} to make room for the backup GPT")
            first = min(first, parts[0].start) if parts else first
        else:
            if len(parts) > 4:
                raise OpError("MBR supports only 4 primary partitions; this disk has "
                              f"{len(parts)}. Delete or merge partitions first.")
            if disk.sectors > 0xFFFFFFFF and parts and parts[-1].end > 0xFFFFFFFF:
                raise OpError("Partitions beyond 2 TiB cannot be described by MBR")
            first = min(first, parts[0].start) if parts else first
        new_parts = []
        for i, p in enumerate(parts, start=1):
            p.number = i
            if self.to == "gpt":
                p.kind = "normal"
                p.type_id = parttypes.mbr_to_gpt(p.type_id)
                p.attrs = ["LegacyBIOSBootable"] if p.bootable else []
                p.bootable = False
            else:
                p.kind = "primary"
                p.bootable = "LegacyBIOSBootable" in p.attrs
                p.type_id = parttypes.gpt_to_mbr(p.type_id)
                p.attrs = []
                p.name = ""
            new_parts.append(p)
        disk.partitions = new_parts
        disk.table = self.to
        disk.first_usable, disk.last_usable = first, last
        disk.pending = ["converted"]

    def execute(self, ctx):
        disk = ctx.disk(self.disk)
        parts = [p for p in disk.sorted_partitions() if not p.is_extended]
        lines = [f"label: {self.to}", ""]
        for p in parts:
            if self.to == "gpt":
                t = parttypes.mbr_to_gpt(p.type_id)
                line = f"start={p.start}, size={p.size}, type={t}"
                if p.bootable:
                    line += ', attrs="LegacyBIOSBootable"'
            else:
                t = parttypes.gpt_to_mbr(p.type_id)
                line = f"start={p.start}, size={p.size}, type={t}"
                if "LegacyBIOSBootable" in p.attrs:
                    line += ", bootable"
            lines.append(line)
        ctx.step(f"Writing {parttypes.table_label(self.to)} partition table")
        ctx.sfdisk(self.disk, [], script="\n".join(lines) + "\n", wipe="auto")


ALL_OPS = list(REGISTRY.values())


def simulate(disks: list[Disk], ops: list[Operation]) -> list[Disk]:
    """Apply ``ops`` to a copy of ``disks`` (raises OpError on the first invalid op)."""
    preview = [d.clone() for d in disks]
    for op in ops:
        op.apply(preview)
    return preview


def free_space_at(disk: Disk, sector: int) -> FreeSpace | None:
    for f in disk.free_spaces():
        if f.start <= sector <= f.end:
            return f
    return None


def redact(d: dict) -> dict:
    """Copy of an operation dict that is safe to log."""
    out = dict(d)
    cls = REGISTRY.get(out.get("op", ""))
    for k in (cls.secret_fields if cls else ()):
        if out.get(k):
            out[k] = "********"
    return out


@dataclass
class Plan:
    """A queue of operations with undo support."""
    ops: list[Operation] = field(default_factory=list)

    def preview(self, disks: list[Disk]) -> list[Disk]:
        return simulate(disks, self.ops)

    def add(self, disks: list[Disk], op: Operation) -> list[Disk]:
        preview = simulate(disks, self.ops)
        op.apply(preview)        # raises OpError if impossible
        self.ops.append(op)
        return preview

    def undo(self) -> Operation | None:
        return self.ops.pop() if self.ops else None

    def clear(self) -> None:
        self.ops.clear()

    def prune(self, disks: list[Disk]) -> list[Operation]:
        """Drop operations that no longer apply (e.g. a disk was unplugged)."""
        kept, dropped = [], []
        preview = [d.clone() for d in disks]
        for op in self.ops:
            try:
                op.apply(preview)
                kept.append(op)
            except OpError:
                dropped.append(op)
        self.ops = kept
        return dropped
