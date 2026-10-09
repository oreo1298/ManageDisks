"""In-memory description of disks and partitions.

All partition positions are expressed in *logical sectors* of the disk (the
same unit sfdisk uses), never in the 512-byte units used by sysfs.
"""

from __future__ import annotations

import copy
from dataclasses import asdict, dataclass, field
from typing import Iterator

from . import parttypes
from .units import MiB, align_down, align_up

# Gaps smaller than this are not shown as usable free space.
MIN_FREE_BYTES = MiB


@dataclass
class Partition:
    number: int
    start: int                     # first sector
    size: int                      # sectors
    path: str = ""                 # /dev/sda1, empty for not-yet-created partitions
    kind: str = "normal"           # normal | primary | extended | logical
    type_id: str = ""              # GPT GUID or MBR code
    name: str = ""                 # GPT partition name (PARTLABEL)
    uuid: str = ""                 # PARTUUID
    attrs: list[str] = field(default_factory=list)   # GPT attribute names
    bootable: bool = False         # MBR active flag
    fstype: str = ""               # filesystem / content signature (ext4, crypto_LUKS, ...)
    fsversion: str = ""
    label: str = ""
    fsuuid: str = ""
    mountpoints: list[str] = field(default_factory=list)
    fs_size: int | None = None     # bytes
    fs_used: int | None = None     # bytes
    holders: list[dict] = field(default_factory=list)  # e.g. opened LUKS / LVM devices
    read_only: bool = False
    pending: list[str] = field(default_factory=list)   # preview markers: new, resized, ...

    # -- geometry ---------------------------------------------------------
    @property
    def end(self) -> int:
        """Last sector (inclusive)."""
        return self.start + self.size - 1

    def size_bytes(self, sector_size: int) -> int:
        return self.size * sector_size

    # -- state ------------------------------------------------------------
    @property
    def is_extended(self) -> bool:
        return self.kind == "extended"

    @property
    def is_logical(self) -> bool:
        return self.kind == "logical"

    @property
    def is_swap_active(self) -> bool:
        return "[SWAP]" in self.mountpoints

    @property
    def is_mounted(self) -> bool:
        return any(m and m != "[SWAP]" for m in self.mountpoints)

    @property
    def busy(self) -> bool:
        """True if the kernel is using the partition (mounted, swap, holders)."""
        return bool(self.mountpoints) or bool(self.holders)

    @property
    def is_new(self) -> bool:
        return "new" in self.pending

    def display_name(self) -> str:
        if self.is_new or not self.path:
            return f"New partition #{self.number}" if self.number else "New partition"
        return self.path

    def content(self) -> str:
        """The thing actually holding data (inner fs of an opened LUKS volume, ...)."""
        for h in self.holders:
            if h.get("fstype"):
                return h["fstype"]
        return self.fstype

    def all_mountpoints(self) -> list[str]:
        mps = [m for m in self.mountpoints if m]
        for h in self.holders:
            mps.extend(m for m in h.get("mountpoints", []) if m)
        return mps

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Partition":
        return cls(**d)


@dataclass
class FreeSpace:
    start: int
    size: int
    inside_extended: bool = False

    @property
    def end(self) -> int:
        return self.start + self.size - 1

    def size_bytes(self, sector_size: int) -> int:
        return self.size * sector_size


@dataclass
class Disk:
    path: str
    name: str
    size: int                      # bytes
    sector_size: int = 512         # logical
    physical_sector_size: int = 512
    model: str = ""
    vendor: str = ""
    serial: str = ""
    transport: str = ""            # nvme, sata, usb, mmc, virtio, loop, ...
    rotational: bool = False
    removable: bool = False
    read_only: bool = False
    table: str | None = None       # "gpt", "dos" or None
    table_uuid: str = ""
    first_usable: int = 0          # first usable sector for partitions
    last_usable: int = 0           # last usable sector (inclusive)
    geometry_exact: bool = False   # first/last usable come from sfdisk (not estimated)
    partitions: list[Partition] = field(default_factory=list)
    # Filesystem written directly on the disk (no partition table).
    fstype: str = ""
    label: str = ""
    fsuuid: str = ""
    mountpoints: list[str] = field(default_factory=list)
    fs_size: int | None = None
    fs_used: int | None = None
    holders: list[dict] = field(default_factory=list)
    backing_file: str = ""         # loop devices
    kind: str = "disk"             # disk | loop | raid
    pending: list[str] = field(default_factory=list)

    # -- basics -----------------------------------------------------------
    @property
    def sectors(self) -> int:
        return self.size // self.sector_size

    @property
    def alignment(self) -> int:
        """Default partition alignment in sectors (1 MiB)."""
        return max(1, MiB // self.sector_size)

    @property
    def max_primary(self) -> int:
        return 4 if self.table == "dos" else 128

    @property
    def busy(self) -> bool:
        return (bool(self.mountpoints) or bool(self.holders)
                or any(p.busy for p in self.partitions))

    def media_kind(self) -> str:
        """Coarse device category used for icons."""
        if self.kind == "loop":
            return "loop"
        if self.transport == "usb":
            return "usb"
        if self.transport == "mmc":
            return "sdcard"
        if self.transport == "nvme":
            return "nvme"
        if not self.rotational:
            return "ssd"
        return "hdd"

    def title(self) -> str:
        vendor = self.vendor.strip()
        if vendor.lower().startswith("0x") or vendor.upper() == "ATA":
            vendor = ""  # PCI ids (virtio) and the generic libata vendor are noise
        m = " ".join(x for x in (vendor, self.model.strip()) if x)
        if m:
            return m
        if self.kind == "loop":
            return f"Loop device {self.name}"
        return self.name

    # -- partitions -------------------------------------------------------
    def find(self, start: int) -> Partition | None:
        for p in self.partitions:
            if p.start == start:
                return p
        return None

    def by_number(self, number: int) -> Partition | None:
        for p in self.partitions:
            if p.number == number:
                return p
        return None

    def extended(self) -> Partition | None:
        for p in self.partitions:
            if p.is_extended:
                return p
        return None

    def top_level(self) -> list[Partition]:
        return sorted((p for p in self.partitions if not p.is_logical), key=lambda p: p.start)

    def logicals(self) -> list[Partition]:
        return sorted((p for p in self.partitions if p.is_logical), key=lambda p: p.start)

    def primary_count(self) -> int:
        return sum(1 for p in self.partitions if not p.is_logical)

    def sorted_partitions(self) -> list[Partition]:
        return sorted(self.partitions, key=lambda p: (p.start, 0 if p.is_extended else 1))

    def used_numbers(self) -> set[int]:
        return {p.number for p in self.partitions}

    def next_number(self, kind: str = "normal") -> int:
        used = self.used_numbers()
        if kind == "logical":
            return max([4] + [p.number for p in self.partitions if p.is_logical]) + 1
        limit = 4 if self.table == "dos" else 128
        for n in range(1, limit + 1):
            if n not in used:
                return n
        return 0

    # -- free space -------------------------------------------------------
    def _gaps(self, lo: int, hi: int, parts: list[Partition], inside_ext: bool) -> list[FreeSpace]:
        """Free regions between ``parts`` within [lo, hi]."""
        min_sectors = max(1, MIN_FREE_BYTES // self.sector_size)
        out: list[FreeSpace] = []
        cur = lo
        for p in sorted(parts, key=lambda x: x.start):
            if p.start > cur:
                out.append(FreeSpace(cur, p.start - cur, inside_ext))
            cur = max(cur, p.end + 1)
        if hi >= cur:
            out.append(FreeSpace(cur, hi - cur + 1, inside_ext))
        return [f for f in out if self._usable_extent(f)[1] >= min_sectors]

    def _usable_extent(self, f: FreeSpace) -> tuple[int, int]:
        """Aligned (start, size) a new partition could occupy inside ``f``."""
        a = self.alignment
        start = f.start + (1 if f.inside_extended else 0)  # room for the EBR
        start = align_up(start, a)
        end_excl = f.end + 1
        # The size is rounded down to whole MiB so the following partition
        # (if any) stays aligned too.
        size = end_excl - start
        if size <= 0:
            return start, 0
        aligned = align_down(size, a)
        return start, aligned if aligned > 0 else size

    def usable_extent(self, f: FreeSpace) -> tuple[int, int]:
        return self._usable_extent(f)

    def free_spaces(self) -> list[FreeSpace]:
        if not self.table:
            return []
        lo, hi = self.first_usable, self.last_usable
        out = self._gaps(lo, hi, self.top_level(), False)
        ext = self.extended()
        if ext:
            out.extend(self._gaps(ext.start, ext.end, self.logicals(), True))
        return sorted(out, key=lambda f: f.start)

    def segments(self) -> list[Partition | FreeSpace]:
        """Top-level layout in disk order (extended partitions included, logical not)."""
        items: list[Partition | FreeSpace] = list(self.top_level())
        items += [f for f in self.free_spaces() if not f.inside_extended]
        return sorted(items, key=lambda x: x.start)

    def segments_in_extended(self) -> list[Partition | FreeSpace]:
        items: list[Partition | FreeSpace] = list(self.logicals())
        items += [f for f in self.free_spaces() if f.inside_extended]
        return sorted(items, key=lambda x: x.start)

    def free_bytes(self) -> int:
        return sum(f.size for f in self.free_spaces()) * self.sector_size

    def neighbours(self, part: Partition) -> tuple[int, int]:
        """Lowest start / highest end sector ``part`` may grow to without overlapping."""
        if part.is_logical:
            ext = self.extended()
            lo, hi = (ext.start, ext.end) if ext else (self.first_usable, self.last_usable)
            siblings = [p for p in self.logicals() if p is not part]
            lo_min = lo + 1
        else:
            lo, hi = self.first_usable, self.last_usable
            siblings = [p for p in self.top_level() if p is not part]
            lo_min = lo
        if part.is_extended:
            # An extended partition must keep containing all logical partitions.
            pass
        left = lo_min
        right = hi
        for s in siblings:
            if s.end < part.start:
                left = max(left, s.end + 1 + (1 if part.is_logical else 0))
            elif s.start > part.end:
                right = min(right, s.start - 1)
        return left, right

    # -- usage ------------------------------------------------------------
    def used_ratio(self) -> float | None:
        allocated = sum(p.size for p in self.partitions if not p.is_extended)
        if not self.sectors:
            return None
        return min(1.0, allocated / self.sectors)

    # -- serialisation ----------------------------------------------------
    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Disk":
        d = dict(d)
        d["partitions"] = [Partition.from_dict(p) for p in d.get("partitions", [])]
        return cls(**d)

    def clone(self) -> "Disk":
        return copy.deepcopy(self)


def find_disk(disks: list[Disk], path: str) -> Disk | None:
    for d in disks:
        if d.path == path:
            return d
    return None


def iter_partitions(disks: list[Disk]) -> Iterator[tuple[Disk, Partition]]:
    for d in disks:
        for p in d.sorted_partitions():
            yield d, p


def estimate_usable(table: str | None, sectors: int, sector_size: int) -> tuple[int, int]:
    """Best guess of the usable sector range when sfdisk could not be run."""
    align = max(1, MiB // sector_size)
    if table == "gpt":
        entries = -(-(128 * 128) // sector_size)
        return align, sectors - 1 - entries - 1
    if table == "dos":
        return align, sectors - 1
    return 0, max(0, sectors - 1)


def table_name(disk: Disk) -> str:
    return parttypes.table_label(disk.table)
