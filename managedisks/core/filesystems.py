"""Filesystem support: creation, resizing, checking, labels and usage.

Every filesystem is described by a :class:`Filesystem` subclass that knows
which external tools it needs and how to drive them. Capabilities are only
reported as available when the required tools are installed, and
:func:`missing_packages` tells the user what to install on their distro.
"""

from __future__ import annotations

import contextlib
import os
import re
import secrets
import tempfile
import uuid as uuidlib
from dataclasses import dataclass

from . import distro
from .runner import CommandError, Runner, have, output, run_simple
from .units import GiB, KiB, MiB, TiB

CAPABILITIES = ("create", "grow", "shrink", "move", "copy", "check", "label", "uuid", "usage")
CAPABILITY_NAMES = {
    "create": "Create", "grow": "Grow", "shrink": "Shrink", "move": "Move", "copy": "Copy",
    "check": "Check", "label": "Label", "uuid": "New UUID", "usage": "Read usage",
}

# Which package provides a tool, per distro family ("default" otherwise).
_PKG = {
    "e2fsprogs": {"default": "e2fsprogs", "gentoo": "sys-fs/e2fsprogs"},
    "btrfs": {"default": "btrfs-progs", "suse": "btrfsprogs", "gentoo": "sys-fs/btrfs-progs"},
    "xfs": {"default": "xfsprogs", "gentoo": "sys-fs/xfsprogs"},
    "f2fs": {"default": "f2fs-tools", "gentoo": "sys-fs/f2fs-tools"},
    "dosfs": {"default": "dosfstools", "gentoo": "sys-fs/dosfstools"},
    "exfat": {"default": "exfatprogs", "gentoo": "sys-fs/exfatprogs"},
    "ntfs": {"default": "ntfs-3g", "fedora": "ntfsprogs", "suse": "ntfsprogs", "mageia": "ntfs-3g",
             "alpine": "ntfs-3g-progs", "gentoo": "sys-fs/ntfs3g", "nixos": "ntfs3g"},
    "util-linux": {"default": "util-linux", "gentoo": "sys-apps/util-linux"},
    "sfdisk": {"default": "util-linux", "debian": "fdisk", "alpine": "sfdisk",
               "gentoo": "sys-apps/util-linux"},
    "cryptsetup": {"default": "cryptsetup", "gentoo": "sys-fs/cryptsetup"},
    "smartmontools": {"default": "smartmontools", "gentoo": "sys-apps/smartmontools"},
    "udisks": {"default": "udisks2", "gentoo": "sys-fs/udisks"},
}

TOOL_PACKAGE = {
    **{t: "e2fsprogs" for t in ("mkfs.ext2", "mkfs.ext3", "mkfs.ext4", "e2fsck", "resize2fs",
                                 "tune2fs", "e2label", "dumpe2fs", "badblocks")},
    **{t: "btrfs" for t in ("mkfs.btrfs", "btrfs", "btrfstune")},
    **{t: "xfs" for t in ("mkfs.xfs", "xfs_growfs", "xfs_admin", "xfs_repair", "xfs_db")},
    **{t: "f2fs" for t in ("mkfs.f2fs", "resize.f2fs", "fsck.f2fs", "f2fslabel")},
    **{t: "dosfs" for t in ("mkfs.fat", "fatlabel", "fsck.fat")},
    **{t: "exfat" for t in ("mkfs.exfat", "fsck.exfat", "exfatlabel", "tune.exfat", "dump.exfat")},
    **{t: "ntfs" for t in ("mkfs.ntfs", "ntfsresize", "ntfslabel", "ntfsfix", "ntfsinfo")},
    **{t: "util-linux" for t in ("mkswap", "swaplabel", "swapon", "swapoff", "wipefs", "blkid",
                                  "lsblk", "partx", "blkdiscard", "mount", "umount")},
    "sfdisk": "sfdisk",
    "cryptsetup": "cryptsetup",
    "smartctl": "smartmontools",
    "udisksctl": "udisks",
}


def package_for(tool: str, fam: str | None = None) -> str:
    fam = fam or distro.family() or "default"
    key = TOOL_PACKAGE.get(tool)
    if not key:
        return tool
    names = _PKG[key]
    return names.get(fam, names["default"])


class Unsupported(RuntimeError):
    pass


@contextlib.contextmanager
def temp_mount(run: Runner, dev: str, fstype: str, options: str | None = None):
    """Mount ``dev`` on a private temporary directory for online-only tools."""
    base = "/run/managedisks" if os.access("/run", os.W_OK) else None
    if base:
        os.makedirs(base, mode=0o700, exist_ok=True)
    mnt = tempfile.mkdtemp(prefix="mnt-", dir=base)
    argv = ["mount", "-t", fstype]
    if options:
        argv += ["-o", options]
    try:
        run.run(argv + [dev, mnt])
        try:
            yield mnt
        finally:
            run.run(["umount", mnt], check=False)
    finally:
        with contextlib.suppress(OSError):
            os.rmdir(mnt)


def _int(pattern: str, text: str, default: int | None = None) -> int | None:
    m = re.search(pattern, text, re.MULTILINE)
    return int(m.group(1)) if m else default


class Filesystem:
    id = ""
    name = ""
    description = ""
    lsblk_types: tuple[str, ...] = ()
    # capability -> tools required
    tools: dict[str, tuple[str, ...]] = {}
    grow_online = False      # can grow while mounted
    shrink_online = False    # can shrink while mounted
    label_online = True
    min_size = MiB
    max_size: int | None = None
    max_label = 16
    label_upper = False
    movable = True
    color = "#888888"

    # -- capabilities -----------------------------------------------------
    def supports(self, cap: str) -> bool:
        if cap == "move":
            return self.movable and have("sfdisk")
        if cap == "copy":
            return self.movable
        req = self.tools.get(cap)
        if req is None:
            return False
        return all(have(t) for t in req)

    def implemented(self, cap: str) -> bool:
        if cap in ("move", "copy"):
            return self.movable
        return cap in self.tools

    def missing_tools(self, cap: str) -> list[str]:
        if cap in ("move", "copy"):
            return [] if have("sfdisk") or cap == "copy" else ["sfdisk"]
        return [t for t in self.tools.get(cap, ()) if not have(t)]

    def normalize_label(self, label: str) -> str:
        label = label.strip()
        if self.label_upper:
            label = label.upper()
        return label

    def validate_label(self, label: str) -> str | None:
        if len(label.encode("utf-8")) > self.max_label:
            return f"{self.name} labels can be at most {self.max_label} bytes long"
        if any(c in label for c in "\n\r\t\0"):
            return "The label contains invalid characters"
        return None

    # -- operations (override) --------------------------------------------
    def mkfs(self, run: Runner, dev: str, label: str = "") -> None:
        raise Unsupported(f"Cannot create {self.name} filesystems")

    def set_label(self, run: Runner, dev: str, label: str, mountpoint: str | None = None) -> None:
        raise Unsupported(f"Cannot change the label of {self.name} filesystems")

    def set_uuid(self, run: Runner, dev: str) -> None:
        raise Unsupported(f"Cannot change the UUID of {self.name} filesystems")

    def grow(self, run: Runner, dev: str, size: int, mountpoint: str | None = None) -> None:
        raise Unsupported(f"Cannot grow {self.name} filesystems")

    def shrink(self, run: Runner, dev: str, size: int, mountpoint: str | None = None) -> None:
        raise Unsupported(f"Cannot shrink {self.name} filesystems")

    def check(self, run: Runner, dev: str, repair: bool = True, mountpoint: str | None = None) -> None:
        raise Unsupported(f"Cannot check {self.name} filesystems")

    def usage(self, dev: str) -> tuple[int, int] | None:
        """(size, used) in bytes for an unmounted filesystem, if determinable."""
        return None

    def min_size_for(self, dev: str, mountpoint: str | None = None) -> int | None:
        """Smallest size the filesystem on ``dev`` can be shrunk to."""
        return None


class Ext(Filesystem):
    version = 4
    max_label = 16
    grow_online = True
    color = "#3d7cd6"

    def __init__(self, version: int):
        self.version = version
        self.id = f"ext{version}"
        self.name = f"ext{version}"
        self.lsblk_types = (self.id,)
        self.description = {
            2: "Classic Linux filesystem without journal",
            3: "Journaled Linux filesystem (legacy)",
            4: "Default Linux filesystem: reliable and fast",
        }[version]
        self.color = {2: "#6b8fd4", 3: "#4f7fd8", 4: "#3d7cd6"}[version]
        self.max_size = 16 * TiB if version < 4 else 1024 * 1024 * TiB
        self.tools = {
            "create": (f"mkfs.{self.id}",),
            "grow": ("resize2fs", "e2fsck"),
            "shrink": ("resize2fs", "e2fsck"),
            "check": ("e2fsck",),
            "label": ("e2label",),
            "uuid": ("tune2fs",),
            "usage": ("dumpe2fs",),
        }

    def mkfs(self, run, dev, label=""):
        argv = [f"mkfs.{self.id}", "-F", "-q"]
        if label:
            argv += ["-L", label]
        run.run(argv + [dev])

    def set_label(self, run, dev, label, mountpoint=None):
        run.run(["e2label", dev, label])

    def set_uuid(self, run, dev):
        run.run(["tune2fs", "-U", "random", dev])

    def _fsck(self, run, dev):
        run.run(["e2fsck", "-f", "-y", dev], ok_codes=(0, 1, 2))

    def grow(self, run, dev, size, mountpoint=None):
        if not mountpoint:
            self._fsck(run, dev)
        run.run(["resize2fs", "-p", dev], progress=_resize2fs_progress)

    def shrink(self, run, dev, size, mountpoint=None):
        if mountpoint:
            raise Unsupported("ext filesystems must be unmounted to shrink")
        self._fsck(run, dev)
        run.run(["resize2fs", "-p", dev, f"{size // KiB}K"], progress=_resize2fs_progress)

    def check(self, run, dev, repair=True, mountpoint=None):
        if mountpoint:
            raise Unsupported("Unmount the filesystem before checking it")
        run.run(["e2fsck", "-f", "-y" if repair else "-n", dev], ok_codes=(0, 1, 2))

    def _header(self, dev):
        text = output(["dumpe2fs", "-h", dev])
        bs = _int(r"^Block size:\s+(\d+)", text)
        count = _int(r"^Block count:\s+(\d+)", text)
        free = _int(r"^Free blocks:\s+(\d+)", text)
        return bs, count, free

    def usage(self, dev):
        bs, count, free = self._header(dev)
        if bs and count is not None and free is not None:
            return count * bs, (count - free) * bs
        return None

    def min_size_for(self, dev, mountpoint=None):
        bs, _, _ = self._header(dev)
        text = run_simple(["resize2fs", "-P", dev], check=False).output
        blocks = _int(r"minimum size of the filesystem:\s+(\d+)", text)
        if bs and blocks:
            return blocks * bs
        return None


def _resize2fs_progress(line: str) -> float | None:
    # resize2fs -p prints rows of X characters; not worth parsing precisely.
    return None


class Btrfs(Filesystem):
    id = "btrfs"
    name = "Btrfs"
    description = "Copy-on-write filesystem with snapshots, compression and checksums"
    lsblk_types = ("btrfs",)
    tools = {
        "create": ("mkfs.btrfs",),
        "grow": ("btrfs",),
        "shrink": ("btrfs",),
        "check": ("btrfs",),
        "label": ("btrfs",),
        "uuid": ("btrfstune",),
        "usage": ("btrfs",),
    }
    grow_online = True
    shrink_online = True
    min_size = 256 * MiB
    max_label = 255
    color = "#e0a526"

    def mkfs(self, run, dev, label=""):
        argv = ["mkfs.btrfs", "-f"]
        if label:
            argv += ["-L", label]
        run.run(argv + [dev])

    def set_label(self, run, dev, label, mountpoint=None):
        run.run(["btrfs", "filesystem", "label", mountpoint or dev, label])

    def set_uuid(self, run, dev):
        run.run(["btrfstune", "-f", "-u", dev])

    @staticmethod
    def _devid(mnt: str, dev: str) -> str:
        try:
            text = output(["btrfs", "filesystem", "show", "--raw", mnt])
        except CommandError:
            return "1"
        real = os.path.realpath(dev)
        for line in text.splitlines():
            m = re.search(r"devid\s+(\d+)\s+size.*path\s+(\S+)", line)
            if m and os.path.realpath(m.group(2)) == real:
                return m.group(1)
        return "1"

    def _resize(self, run, dev, spec, mountpoint):
        if mountpoint:
            run.run(["btrfs", "filesystem", "resize", f"{self._devid(mountpoint, dev)}:{spec}", mountpoint])
            return
        with temp_mount(run, dev, "btrfs") as mnt:
            run.run(["btrfs", "filesystem", "resize", f"{self._devid(mnt, dev)}:{spec}", mnt])

    def grow(self, run, dev, size, mountpoint=None):
        self._resize(run, dev, "max", mountpoint)

    def shrink(self, run, dev, size, mountpoint=None):
        self._resize(run, dev, str(size), mountpoint)

    def check(self, run, dev, repair=True, mountpoint=None):
        if mountpoint:
            run.run(["btrfs", "scrub", "start", "-B", mountpoint])
        else:
            # "btrfs check --repair" is documented as dangerous; only a
            # read-only check is offered.
            run.run(["btrfs", "check", "--readonly", "--progress", dev])

    def usage(self, dev):
        text = output(["btrfs", "inspect-internal", "dump-super", dev])
        total = _int(r"^total_bytes\s+(\d+)", text)
        used = _int(r"^bytes_used\s+(\d+)", text)
        if total and used is not None:
            return total, used
        return None

    def min_size_for(self, dev, mountpoint=None):
        def query(mnt):
            text = output(["btrfs", "inspect-internal", "min-dev-size", "--id", self._devid(mnt, dev), mnt])
            return _int(r"^(\d+)\s+bytes", text)
        if mountpoint:
            return query(mountpoint)
        quiet = Runner()
        with temp_mount(quiet, dev, "btrfs", "ro") as mnt:
            return query(mnt)


class Xfs(Filesystem):
    id = "xfs"
    name = "XFS"
    description = "High-performance journaled filesystem (cannot shrink)"
    lsblk_types = ("xfs",)
    tools = {
        "create": ("mkfs.xfs",),
        "grow": ("xfs_growfs",),
        "check": ("xfs_repair",),
        "label": ("xfs_admin",),
        "uuid": ("xfs_admin",),
        "usage": ("xfs_db",),
    }
    grow_online = True
    label_online = False
    min_size = 300 * MiB
    max_label = 12
    color = "#c4553b"

    def mkfs(self, run, dev, label=""):
        argv = ["mkfs.xfs", "-f"]
        if label:
            argv += ["-L", label]
        run.run(argv + [dev])

    def set_label(self, run, dev, label, mountpoint=None):
        run.run(["xfs_admin", "-L", label or "--", dev])

    def set_uuid(self, run, dev):
        run.run(["xfs_admin", "-U", "generate", dev])

    def grow(self, run, dev, size, mountpoint=None):
        if mountpoint:
            run.run(["xfs_growfs", mountpoint])
            return
        with temp_mount(run, dev, "xfs") as mnt:
            run.run(["xfs_growfs", mnt])

    def check(self, run, dev, repair=True, mountpoint=None):
        if mountpoint:
            raise Unsupported("Unmount the filesystem before checking it")
        run.run(["xfs_repair"] + ([] if repair else ["-n"]) + [dev])

    def usage(self, dev):
        text = output(["xfs_db", "-r", "-c", "sb 0", "-c", "print dblocks fdblocks blocksize", dev])
        dblocks = _int(r"^dblocks = (\d+)", text)
        fdblocks = _int(r"^fdblocks = (\d+)", text)
        bs = _int(r"^blocksize = (\d+)", text)
        if dblocks and fdblocks is not None and bs:
            return dblocks * bs, (dblocks - fdblocks) * bs
        return None


class F2fs(Filesystem):
    id = "f2fs"
    name = "F2FS"
    description = "Flash-friendly filesystem for SSDs, SD cards and USB sticks"
    lsblk_types = ("f2fs",)
    tools = {
        "create": ("mkfs.f2fs",),
        "grow": ("resize.f2fs",),
        "check": ("fsck.f2fs",),
        "label": ("f2fslabel",),
    }
    label_online = False
    min_size = 64 * MiB
    max_label = 512
    color = "#2ba39a"

    def mkfs(self, run, dev, label=""):
        argv = ["mkfs.f2fs", "-f"]
        if label:
            argv += ["-l", label]
        run.run(argv + [dev])

    def set_label(self, run, dev, label, mountpoint=None):
        run.run(["f2fslabel", dev, label])

    def grow(self, run, dev, size, mountpoint=None):
        if mountpoint:
            raise Unsupported("F2FS must be unmounted to grow")
        run.run(["resize.f2fs", dev])

    def check(self, run, dev, repair=True, mountpoint=None):
        if mountpoint:
            raise Unsupported("Unmount the filesystem before checking it")
        run.run(["fsck.f2fs", "-f", "-y" if repair else "-n", dev], ok_codes=(0, 1))


class Fat(Filesystem):
    label_upper = True
    max_label = 11
    lsblk_types = ("vfat",)
    tools = {
        "create": ("mkfs.fat",),
        "check": ("fsck.fat",),
        "label": ("fatlabel",),
        "uuid": ("fatlabel",),
        "usage": ("fsck.fat",),
    }

    def __init__(self, bits: int):
        self.bits = bits
        self.id = f"fat{bits}"
        self.name = f"FAT{bits}"
        if bits == 32:
            self.description = "Universal compatibility (EFI system partitions, old devices); 4 GiB file limit"
            self.min_size = 33 * MiB
            self.max_size = 2 * TiB
            self.color = "#3fa34d"
        else:
            self.description = "Legacy FAT for very small volumes (max 4 GiB)"
            self.min_size = 1 * MiB
            self.max_size = 4 * GiB
            self.color = "#6cbf5b"

    def validate_label(self, label):
        err = super().validate_label(label)
        if err:
            return err
        bad = set('"*+,./:;<=>?[\\]|')
        if any(c in bad for c in label):
            return "FAT labels cannot contain any of: " + "".join(sorted(bad))
        return None

    def mkfs(self, run, dev, label=""):
        argv = ["mkfs.fat", "-F", str(self.bits)]
        if label:
            argv += ["-n", self.normalize_label(label)]
        run.run(argv + [dev])

    def set_label(self, run, dev, label, mountpoint=None):
        if label:
            run.run(["fatlabel", dev, self.normalize_label(label)])
        else:
            run.run(["fatlabel", "-r", dev])

    def set_uuid(self, run, dev):
        run.run(["fatlabel", "-i", dev, secrets.token_hex(4)])

    def check(self, run, dev, repair=True, mountpoint=None):
        if mountpoint:
            raise Unsupported("Unmount the filesystem before checking it")
        run.run(["fsck.fat", "-a" if repair else "-n", "-w", "-v", dev], ok_codes=(0, 1))

    def usage(self, dev):
        text = run_simple(["fsck.fat", "-n", "-v", dev], check=False).output
        csize = _int(r"(\d+) bytes per cluster", text)
        m = re.search(r"(\d+)/(\d+) clusters", text)
        if csize and m:
            used, total = int(m.group(1)), int(m.group(2))
            return total * csize, used * csize
        return None


class Exfat(Filesystem):
    id = "exfat"
    name = "exFAT"
    description = "Cross-platform (Windows, macOS, Linux) for USB drives and SD cards"
    lsblk_types = ("exfat",)
    tools = {
        "create": ("mkfs.exfat",),
        "check": ("fsck.exfat",),
        "label": ("exfatlabel",),
        "uuid": ("tune.exfat",),
    }
    label_online = False
    max_label = 22   # 11 UTF-16 characters
    color = "#55b046"

    def mkfs(self, run, dev, label=""):
        argv = ["mkfs.exfat"]
        if label:
            argv += ["-L", label]
        run.run(argv + [dev])

    def set_label(self, run, dev, label, mountpoint=None):
        run.run(["exfatlabel", dev, label])

    def set_uuid(self, run, dev):
        run.run(["tune.exfat", "-I", "0x" + secrets.token_hex(4), dev])

    def check(self, run, dev, repair=True, mountpoint=None):
        if mountpoint:
            raise Unsupported("Unmount the filesystem before checking it")
        run.run(["fsck.exfat", "-y" if repair else "-n", dev], ok_codes=(0, 1))


class Ntfs(Filesystem):
    id = "ntfs"
    name = "NTFS"
    description = "Windows filesystem"
    lsblk_types = ("ntfs",)
    tools = {
        "create": ("mkfs.ntfs",),
        "grow": ("ntfsresize",),
        "shrink": ("ntfsresize",),
        "check": ("ntfsfix",),
        "label": ("ntfslabel",),
        "uuid": ("ntfslabel",),
        "usage": ("ntfsresize",),
    }
    label_online = False
    max_label = 128
    color = "#3e9fd6"

    def mkfs(self, run, dev, label=""):
        argv = ["mkfs.ntfs", "-Q", "-F"]
        if label:
            argv += ["-L", label]
        run.run(argv + [dev])

    def set_label(self, run, dev, label, mountpoint=None):
        run.run(["ntfslabel", "--force", dev, label])

    def set_uuid(self, run, dev):
        run.run(["ntfslabel", "--new-serial", dev])

    def grow(self, run, dev, size, mountpoint=None):
        run.run(["ntfsresize", "-f", "-f", "-P", "-n", dev])
        run.run(["ntfsresize", "-f", "-f", "-P", dev])

    def shrink(self, run, dev, size, mountpoint=None):
        run.run(["ntfsresize", "-f", "-f", "-P", "-n", "-s", str(size), dev])
        run.run(["ntfsresize", "-f", "-f", "-P", "-s", str(size), dev])

    def check(self, run, dev, repair=True, mountpoint=None):
        if mountpoint:
            raise Unsupported("Unmount the filesystem before checking it")
        run.run(["ntfsfix"] + ([] if repair else ["-n"]) + [dev])

    def _info(self, dev):
        return run_simple(["ntfsresize", "-i", "-f", "-P", dev], check=False).output

    def usage(self, dev):
        text = self._info(dev)
        size = _int(r"Current volume size:\s+(\d+) bytes", text)
        used_mb = re.search(r"Space in use\s*:\s*(\d+) MB", text)
        if size and used_mb:
            return size, int(used_mb.group(1)) * 1000 * 1000
        return None

    def min_size_for(self, dev, mountpoint=None):
        return _int(r"You might resize at (\d+) bytes", self._info(dev))


class Swap(Filesystem):
    id = "linux-swap"
    name = "Linux swap"
    description = "Swap space for memory paging and hibernation"
    lsblk_types = ("swap",)
    tools = {
        "create": ("mkswap",),
        "grow": ("mkswap",),
        "shrink": ("mkswap",),
        "label": ("swaplabel",),
        "uuid": ("swaplabel",),
    }
    label_online = False
    min_size = 64 * KiB
    max_label = 16
    color = "#c8417a"

    def mkfs(self, run, dev, label="", uuid: str | None = None):
        argv = ["mkswap", "-f"]
        if label:
            argv += ["-L", label]
        if uuid:
            argv += ["-U", uuid]
        run.run(argv + [dev])

    def _recreate(self, run, dev):
        info = probe(dev)
        self.mkfs(run, dev, info.get("LABEL", ""), info.get("UUID") or None)

    def grow(self, run, dev, size, mountpoint=None):
        self._recreate(run, dev)

    def shrink(self, run, dev, size, mountpoint=None):
        # Recreated after the partition has been shrunk (see operations).
        pass

    def set_label(self, run, dev, label, mountpoint=None):
        run.run(["swaplabel", "-L", label, dev])

    def set_uuid(self, run, dev):
        run.run(["swaplabel", "-U", str(uuidlib.uuid4()), dev])


class Luks(Filesystem):
    id = "luks"
    name = "LUKS encryption"
    description = "Encrypted container (LUKS2) holding another filesystem"
    lsblk_types = ("crypto_LUKS",)
    tools = {
        "create": ("cryptsetup",),
        "label": ("cryptsetup",),
        "uuid": ("cryptsetup",),
    }
    label_online = True
    min_size = 32 * MiB
    max_label = 47
    movable = True
    color = "#7a5ac8"

    def format(self, run, dev, passphrase, label=""):
        argv = ["cryptsetup", "luksFormat", "--batch-mode", "--type", "luks2", "--key-file=-"]
        if label:
            argv += ["--label", label]
        run.run(argv + [dev], input=passphrase, secret_input=True)

    def open(self, run, dev, passphrase, name):
        run.run(["cryptsetup", "open", "--key-file=-", dev, name], input=passphrase,
                secret_input=True)

    def close(self, run, name):
        run.run(["cryptsetup", "close", name])

    def set_label(self, run, dev, label, mountpoint=None):
        run.run(["cryptsetup", "config", "--label", label, dev])

    def set_uuid(self, run, dev):
        run.run(["cryptsetup", "-q", "luksUUID", "--uuid", str(uuidlib.uuid4()), dev])


class Cleared(Filesystem):
    """Pseudo filesystem: wipe signatures and leave the partition empty."""
    id = "cleared"
    name = "Unformatted"
    description = "Remove existing signatures and leave the partition empty"
    tools = {"create": ("wipefs",)}
    min_size = 512
    color = "#9a9a9a"

    def mkfs(self, run, dev, label=""):
        run.run(["wipefs", "-a", dev])


FILESYSTEMS: list[Filesystem] = [
    Ext(4), Btrfs(), Xfs(), F2fs(), Ext(3), Ext(2), Fat(32), Fat(16), Exfat(), Ntfs(),
    Swap(), Luks(), Cleared(),
]

BY_ID = {fs.id: fs for fs in FILESYSTEMS}

# Signatures we display but cannot manage.
FOREIGN_NAMES = {
    "LVM2_member": "LVM physical volume",
    "linux_raid_member": "Linux RAID member",
    "zfs_member": "ZFS member",
    "bcachefs": "bcachefs",
    "hfsplus": "HFS+",
    "hfs": "HFS",
    "apfs": "APFS",
    "udf": "UDF",
    "iso9660": "ISO 9660",
    "squashfs": "SquashFS",
    "erofs": "EROFS",
    "jfs": "JFS",
    "reiserfs": "ReiserFS",
    "nilfs2": "NILFS2",
    "BitLocker": "BitLocker",
    "ufs": "UFS",
    "minix": "Minix",
    "ceph": "Ceph",
    "VMFS": "VMFS",
}

FOREIGN_COLORS = {
    "LVM2_member": "#b3762c", "linux_raid_member": "#8c6d4b", "zfs_member": "#5b6abf",
    "BitLocker": "#5562a6",
}


def lookup(fstype: str, fsversion: str = "") -> Filesystem | None:
    """Map an lsblk/blkid TYPE (+VERSION) to a Filesystem."""
    if not fstype:
        return None
    if fstype == "vfat":
        v = (fsversion or "").upper()
        return BY_ID["fat16"] if v in ("FAT16", "FAT12") else BY_ID["fat32"]
    if fstype in BY_ID:
        return BY_ID[fstype]
    for fs in FILESYSTEMS:
        if fstype in fs.lsblk_types:
            return fs
    return None


def display_name(fstype: str, fsversion: str = "") -> str:
    if not fstype:
        return ""
    fs = lookup(fstype, fsversion)
    if fs:
        return fs.name
    return FOREIGN_NAMES.get(fstype, fstype)


def color_for(fstype: str, fsversion: str = "") -> str:
    fs = lookup(fstype, fsversion)
    if fs:
        return fs.color
    return FOREIGN_COLORS.get(fstype, "#8e8e8e")


def creatable() -> list[Filesystem]:
    return [fs for fs in FILESYSTEMS if fs.implemented("create")]


def probe(dev: str) -> dict[str, str]:
    """Low level blkid probe (needs read access to ``dev``)."""
    try:
        text = output(["blkid", "-p", "-o", "export", dev])
    except CommandError:
        return {}
    info = {}
    for line in text.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            info[k] = re.sub(r"\\(.)", r"\1", v)  # values are shell-escaped
    return info


@dataclass
class SupportRow:
    fs: Filesystem
    caps: dict[str, bool | None]   # None = not implemented for this fs
    missing: list[str]             # missing tools
    packages: list[str]            # packages to install


def support_matrix() -> list[SupportRow]:
    rows = []
    for fs in FILESYSTEMS:
        if fs.id == "cleared":
            continue
        caps: dict[str, bool | None] = {}
        missing: set[str] = set()
        for cap in CAPABILITIES:
            if not fs.implemented(cap):
                caps[cap] = None
                continue
            caps[cap] = fs.supports(cap)
            missing.update(fs.missing_tools(cap))
        pkgs = sorted({package_for(t) for t in missing})
        rows.append(SupportRow(fs, caps, sorted(missing), pkgs))
    return rows


def missing_packages() -> list[str]:
    pkgs: set[str] = set()
    for row in support_matrix():
        pkgs.update(row.packages)
    return sorted(pkgs)
