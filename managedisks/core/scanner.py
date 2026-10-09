"""Discover disks and partitions.

Works unprivileged (lsblk + sysfs, the data udev already exposes) and adds
exact partition-table geometry and low-level probing when it can read the
devices (root, or membership of the ``disk`` group).
"""

from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor

from . import parttypes
from .model import Disk, Partition, estimate_usable
from .runner import CommandError, have, output, run_simple

SYS_BLOCK = "/sys/class/block"

_GPT_ATTR_BITS = {0: "RequiredPartition", 1: "NoBlockIOProtocol", 2: "LegacyBIOSBootable"}


def _read(path: str, default: str = "") -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read().strip()
    except OSError:
        return default


def _read_int(path: str, default: int = 0) -> int:
    try:
        return int(_read(path, str(default)))
    except ValueError:
        return default


def lsblk_json(devices: list[str] | None = None) -> list[dict]:
    argv = ["lsblk", "-J", "-b", "-O"]
    if devices:
        argv += devices
    try:
        text = output(argv, timeout=30)
    except CommandError:
        # Very old lsblk without -O / some columns.
        cols = ("NAME,KNAME,PATH,MAJ:MIN,FSTYPE,FSVER,LABEL,UUID,PTUUID,PTTYPE,PARTTYPE,PARTLABEL,"
                "PARTUUID,PARTFLAGS,RO,RM,HOTPLUG,MODEL,SERIAL,SIZE,TYPE,PKNAME,TRAN,ROTA,VENDOR,"
                "MOUNTPOINT,FSSIZE,FSUSED,LOG-SEC,PHY-SEC")
        text = output(["lsblk", "-J", "-b", "-o", cols] + (devices or []), timeout=30)
    return json.loads(text).get("blockdevices", [])


def _mountpoints(node: dict) -> list[str]:
    mps = node.get("mountpoints")
    if mps is None:
        mp = node.get("mountpoint")
        mps = [mp] if mp else []
    return [m for m in mps if m]


def _int_or_none(v) -> int | None:
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _gpt_attrs_from_flags(flags: str | None) -> list[str]:
    if not flags:
        return []
    try:
        value = int(flags, 16)
    except ValueError:
        return []
    out = [name for bit, name in _GPT_ATTR_BITS.items() if value & (1 << bit)]
    for bit in range(48, 64):
        if value & (1 << bit):
            out.append(f"GUID:{bit}")
    return out


def sfdisk_dump(dev: str) -> dict | None:
    """Partition table as reported by ``sfdisk --json`` (None if unreadable)."""
    if not have("sfdisk"):
        return None
    try:
        text = output(["sfdisk", "--json", dev], timeout=30)
    except CommandError:
        return None
    if not text.strip():
        return None
    try:
        return json.loads(text).get("partitiontable")
    except ValueError:
        return None


def can_read(dev: str) -> bool:
    return os.access(dev, os.R_OK)


def _holders(node: dict) -> list[dict]:
    """Devices stacked on top of a partition/disk (dm-crypt, LVM LVs, md arrays)."""
    out = []
    for child in node.get("children") or []:
        if child.get("type") in ("part",):
            continue
        out.append({
            "name": child.get("name", ""),
            "path": child.get("path") or "/dev/" + child.get("name", ""),
            "type": child.get("type", ""),
            "fstype": child.get("fstype") or "",
            "label": child.get("label") or "",
            "mountpoints": _mountpoints(child) + [m for gc in child.get("children") or []
                                                   for m in _mountpoints(gc)],
            "fs_size": _int_or_none(child.get("fssize")),
            "fs_used": _int_or_none(child.get("fsused")),
        })
    return out


def _partition_from_node(node: dict, disk: Disk) -> Partition:
    name = node.get("kname") or node.get("name")
    sys = os.path.join(SYS_BLOCK, name)
    factor = 512 // disk.sector_size if disk.sector_size < 512 else 1
    start512 = _read_int(os.path.join(sys, "start"))
    size512 = _read_int(os.path.join(sys, "size"))
    ss = disk.sector_size
    start = start512 * 512 // ss if ss >= 512 else start512 * factor
    size = size512 * 512 // ss if ss >= 512 else size512 * factor
    number = _read_int(os.path.join(sys, "partition")) or _int_or_none(node.get("partn")) or 0
    table = disk.table
    type_id = parttypes.normalize_type(node.get("parttype") or "", table)
    p = Partition(
        number=number,
        start=start,
        size=size,
        path=node.get("path") or f"/dev/{name}",
        kind="normal" if table == "gpt" else "primary",
        type_id=type_id,
        name=node.get("partlabel") or "",
        uuid=(node.get("partuuid") or "").upper() if table == "gpt" else (node.get("partuuid") or ""),
        fstype=node.get("fstype") or "",
        fsversion=node.get("fsver") or "",
        label=node.get("label") or "",
        fsuuid=node.get("uuid") or "",
        mountpoints=_mountpoints(node),
        fs_size=_int_or_none(node.get("fssize")),
        fs_used=_int_or_none(node.get("fsused")),
        holders=_holders(node),
        read_only=bool(node.get("ro")),
    )
    if table == "gpt":
        p.attrs = _gpt_attrs_from_flags(node.get("partflags"))
    elif table == "dos":
        flags = node.get("partflags") or ""
        p.bootable = flags.lower() in ("0x80",)
        if type_id in parttypes.EXTENDED_MBR_CODES:
            p.kind = "extended"
        elif number > 4:
            p.kind = "logical"
    return p


def _apply_sfdisk(disk: Disk, pt: dict) -> None:
    """Refine a disk with exact data from ``sfdisk --json``."""
    label = pt.get("label")
    disk.table = {"gpt": "gpt", "dos": "dos"}.get(label, disk.table)
    disk.table_uuid = pt.get("id", disk.table_uuid) or disk.table_uuid
    if "firstlba" in pt and "lastlba" in pt:
        disk.first_usable = int(pt["firstlba"])
        disk.last_usable = int(pt["lastlba"])
        disk.geometry_exact = True
    elif disk.table == "dos":
        disk.first_usable, disk.last_usable = 1, disk.sectors - 1
        disk.geometry_exact = True
    by_start = {p.start: p for p in disk.partitions}
    for idx, e in enumerate(pt.get("partitions", [])):
        start = int(e["start"])
        size = int(e["size"])
        node = e.get("node", "")
        num = _num_from_node(node) or idx + 1
        p = by_start.get(start)
        if p is None:
            # Present in the table but not known to the kernel (yet).
            p = Partition(number=num, start=start, size=size, path=node)
            disk.partitions.append(p)
        p.number = num
        p.size = size  # sysfs reports extended partitions as 1 KiB
        p.type_id = parttypes.normalize_type(e.get("type", ""), disk.table)
        if disk.table == "gpt":
            p.kind = "normal"
            p.name = e.get("name", "") or ""
            p.uuid = (e.get("uuid", "") or "").upper()
            attrs = e.get("attrs", "") or ""
            p.attrs = [a for a in attrs.replace(",", " ").split() if a]
        else:
            p.bootable = bool(e.get("bootable", False))
            if p.type_id in parttypes.EXTENDED_MBR_CODES:
                p.kind = "extended"
            elif num > 4:
                p.kind = "logical"
            else:
                p.kind = "primary"
    known = {int(e["start"]) for e in pt.get("partitions", [])}
    disk.partitions = [p for p in disk.partitions if p.start in known]


def _num_from_node(node: str) -> int:
    digits = ""
    for ch in reversed(node):
        if ch.isdigit():
            digits = ch + digits
        else:
            break
    return int(digits) if digits else 0


def _probe_missing(disk: Disk) -> None:
    """Fill fs info via blkid when udev did not provide it (no udevd, containers)."""
    from .filesystems import probe

    def fill(target, path):
        info = probe(path)
        if not info:
            return
        target.fstype = target.fstype or info.get("TYPE", "")
        target.fsversion = getattr(target, "fsversion", "") or info.get("VERSION", "")
        target.label = target.label or info.get("LABEL", "")
        target.fsuuid = target.fsuuid or info.get("UUID", "")

    for p in disk.partitions:
        if not p.fstype and p.path and not p.is_extended and os.path.exists(p.path):
            fill(p, p.path)
    if not disk.table and not disk.partitions and not disk.fstype:
        info = probe(disk.path)
        if info.get("PTTYPE"):
            disk.table = {"gpt": "gpt", "dos": "dos"}.get(info["PTTYPE"])
        elif info.get("TYPE"):
            disk.fstype = info.get("TYPE", "")
            disk.label = info.get("LABEL", "")
            disk.fsuuid = info.get("UUID", "")


def _disk_from_node(node: dict) -> Disk:
    name = node.get("kname") or node.get("name")
    sys = os.path.join(SYS_BLOCK, name)
    lsec = _int_or_none(node.get("log-sec")) or _read_int(os.path.join(sys, "queue/logical_block_size"), 512)
    psec = _int_or_none(node.get("phy-sec")) or _read_int(os.path.join(sys, "queue/physical_block_size"), lsec)
    size = _int_or_none(node.get("size")) or _read_int(os.path.join(sys, "size")) * 512
    tran = node.get("tran") or ""
    kind = "disk"
    backing = ""
    if node.get("type") == "loop":
        kind = "loop"
        tran = "loop"
        backing = _read(os.path.join(sys, "loop/backing_file"))
    elif node.get("type", "").startswith("raid"):
        kind = "raid"
    if not tran:
        if name.startswith("nvme"):
            tran = "nvme"
        elif name.startswith("mmcblk"):
            tran = "mmc"
        elif name.startswith("vd"):
            tran = "virtio"
    pttype = node.get("pttype")
    disk = Disk(
        path=node.get("path") or f"/dev/{name}",
        name=name,
        size=size,
        sector_size=lsec,
        physical_sector_size=psec,
        model=(node.get("model") or "").strip(),
        vendor=(node.get("vendor") or "").strip(),
        serial=(node.get("serial") or "").strip(),
        transport=tran,
        rotational=bool(node.get("rota")),
        removable=bool(node.get("rm")) or bool(node.get("hotplug")),
        read_only=bool(node.get("ro")),
        table={"gpt": "gpt", "dos": "dos"}.get(pttype or "", None),
        table_uuid=node.get("ptuuid") or "",
        backing_file=backing,
        kind=kind,
    )
    disk.first_usable, disk.last_usable = estimate_usable(disk.table, disk.sectors, disk.sector_size)
    for child in node.get("children") or []:
        if child.get("type") == "part":
            disk.partitions.append(_partition_from_node(child, disk))
    if not disk.partitions and not disk.table:
        disk.fstype = node.get("fstype") or ""
        disk.label = node.get("label") or ""
        disk.fsuuid = node.get("uuid") or ""
        disk.mountpoints = _mountpoints(node)
        disk.fs_size = _int_or_none(node.get("fssize"))
        disk.fs_used = _int_or_none(node.get("fsused"))
        disk.holders = _holders(node)
    # A partition table may exist although udev did not report one (no udevd).
    if disk.partitions and not disk.table:
        disk.table = "gpt" if any(len(p.type_id) == 36 for p in disk.partitions) else "dos"
        disk.first_usable, disk.last_usable = estimate_usable(disk.table, disk.sectors, disk.sector_size)
    return disk


def _wanted(node: dict, include_loop: bool) -> bool:
    t = node.get("type", "")
    name = node.get("name", "")
    if t == "disk":
        return not name.startswith(("zram", "ram"))
    if t == "loop":
        if not include_loop:
            return False
        size = _int_or_none(node.get("size")) or 0
        if size == 0:
            return False
        # Skip read-only image mounts (snaps, live-ISO squashfs).
        if node.get("fstype") in ("squashfs", "erofs") and not node.get("children"):
            return False
        return True
    if t.startswith("raid") and node.get("pkname") is None:
        return False
    return False


def scan(include_loop: bool = False, deep: bool | None = None) -> list[Disk]:
    """Return all disks. ``deep`` forces/disables sfdisk & blkid probing
    (default: whenever the devices are readable)."""
    nodes = [n for n in lsblk_json() if _wanted(n, include_loop)]
    disks = [_disk_from_node(n) for n in nodes]

    def refine(disk: Disk) -> Disk:
        readable = can_read(disk.path) if deep is None else deep
        if readable:
            if disk.table or disk.partitions or not disk.fstype:
                pt = sfdisk_dump(disk.path)
                if pt:
                    _apply_sfdisk(disk, pt)
            _probe_missing(disk)
        for p in disk.partitions:
            if disk.table == "dos" and p.is_extended and not disk.geometry_exact:
                # Unprivileged: extended size unknown (kernel reports 1 KiB);
                # assume it spans its logical partitions.
                logical_end = max((q.end for q in disk.partitions if q.is_logical), default=p.end)
                p.size = max(p.size, logical_end - p.start + 1)
        return disk

    if len(disks) > 1:
        with ThreadPoolExecutor(max_workers=min(8, len(disks))) as ex:
            disks = list(ex.map(refine, disks))
    else:
        disks = [refine(d) for d in disks]
    return sorted(disks, key=_disk_sort_key)


def _disk_sort_key(d: Disk):
    order = {"nvme": 0, "sata": 1, "ata": 1, "sas": 1, "scsi": 1, "virtio": 2, "mmc": 3, "usb": 4, "loop": 6}
    return (order.get(d.transport, 5), d.name)


def scan_disk(path: str, include_loop: bool = True) -> Disk | None:
    for d in scan(include_loop=include_loop):
        if d.path == path:
            return d
    return None


def fill_usage(disks: list[Disk]) -> None:
    """Compute usage of unmounted filesystems (needs root)."""
    from .filesystems import lookup

    jobs = []
    for d in disks:
        for p in d.partitions:
            if p.fstype and not p.is_mounted and p.fs_used is None and p.path:
                fs = lookup(p.fstype, p.fsversion)
                if fs and fs.supports("usage"):
                    jobs.append((p, fs))

    def work(job):
        p, fs = job
        try:
            res = fs.usage(p.path)
        except Exception:  # noqa: BLE001 - usage is best effort
            res = None
        if res:
            p.fs_size, p.fs_used = res

    if jobs:
        with ThreadPoolExecutor(max_workers=min(8, len(jobs))) as ex:
            list(ex.map(work, jobs))


def partition_path(disk_path: str, number: int) -> str:
    """Conventional device node for partition ``number`` of ``disk_path``."""
    base = os.path.basename(disk_path)
    sysdir = os.path.join(SYS_BLOCK, base)
    try:
        for entry in os.listdir(sysdir):
            if entry.startswith(base) and _read_int(os.path.join(sysdir, entry, "partition")) == number:
                return "/dev/" + entry
    except OSError:
        pass
    sep = "p" if base[-1:].isdigit() else ""
    return f"{disk_path}{sep}{number}"


def kernel_partitions(disk_path: str) -> dict[int, tuple[int, int]]:
    """{number: (start, size)} as the kernel currently sees them, in 512-byte units."""
    base = os.path.basename(os.path.realpath(disk_path))
    sysdir = os.path.join(SYS_BLOCK, base)
    out = {}
    try:
        entries = os.listdir(sysdir)
    except OSError:
        return out
    for entry in entries:
        pdir = os.path.join(sysdir, entry)
        if entry.startswith(base) and os.path.exists(os.path.join(pdir, "partition")):
            out[_read_int(os.path.join(pdir, "partition"))] = (
                _read_int(os.path.join(pdir, "start")), _read_int(os.path.join(pdir, "size")))
    return out


def mounted_sources() -> dict[str, list[str]]:
    """{device: [mountpoints]} from /proc/self/mountinfo (fast, no lsblk)."""
    out: dict[str, list[str]] = {}
    try:
        with open("/proc/self/mountinfo", encoding="utf-8") as f:
            for line in f:
                parts = line.split(" - ")
                if len(parts) != 2:
                    continue
                left, right = parts[0].split(), parts[1].split()
                if len(right) >= 2 and left[4:5]:
                    out.setdefault(os.path.realpath(right[1]), []).append(
                        left[4].encode().decode("unicode_escape"))
    except OSError:
        pass
    return out


def swaps() -> set[str]:
    out = set()
    try:
        with open("/proc/swaps", encoding="utf-8") as f:
            next(f, None)
            for line in f:
                out.add(os.path.realpath(line.split()[0]))
    except OSError:
        pass
    return out


def is_rotational(dev: str) -> bool:
    base = os.path.basename(os.path.realpath(dev))
    return _read(os.path.join(SYS_BLOCK, base, "queue/rotational"), "1") == "1"


def run_lsblk_tree(dev: str) -> str:
    return run_simple(["lsblk", dev], check=False).output
