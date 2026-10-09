"""Immediate (non-queued) actions: mounting, encryption, imaging, cloning,
surface tests. All functions here expect to run as root."""

from __future__ import annotations

import bz2
import contextlib
import gzip
import lzma
import mmap
import os
import re
import stat
import time
import uuid as uuidlib

from . import scanner
from .executor import CHUNK, ExecContext
from .filesystems import BY_ID, lookup, probe
from .operations import OpError
from .runner import CommandError, have, output, run_simple
from .units import format_size


def require_block_device(path: str) -> str:
    real = os.path.realpath(path)
    try:
        st = os.stat(real)
    except OSError:
        raise OpError(f"{path} does not exist") from None
    if not stat.S_ISBLK(st.st_mode):
        raise OpError(f"{path} is not a block device")
    return path


def device_size(path: str) -> int:
    fd = os.open(path, os.O_RDONLY)
    try:
        return os.lseek(fd, 0, os.SEEK_END)
    finally:
        os.close(fd)


# -- filesystem details -------------------------------------------------------
def fs_usage(dev: str, fstype: str, fsversion: str = "") -> dict:
    require_block_device(dev)
    fs = lookup(fstype, fsversion)
    if fs is None or not fs.supports("usage"):
        return {}
    res = fs.usage(dev)
    return {"size": res[0], "used": res[1]} if res else {}


def fs_min_size(dev: str, fstype: str, fsversion: str = "", mountpoint: str | None = None) -> int | None:
    require_block_device(dev)
    fs = lookup(fstype, fsversion)
    if fs is None:
        return None
    try:
        return fs.min_size_for(dev, mountpoint)
    except (CommandError, OSError):
        return None


# -- mounting -------------------------------------------------------------------
_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def default_mountpoint(dev: str, label: str = "") -> str:
    name = _SAFE.sub("_", label).strip("_") or os.path.basename(dev)
    base = os.path.join("/mnt", name)
    path, i = base, 1
    while os.path.ismount(path) or (os.path.exists(path) and os.listdir(path)):
        i += 1
        path = f"{base}-{i}"
    return path


MOUNT_HINTS = {
    "apfs": "Linux has no built-in APFS driver. To read this volume install the linux-apfs-rw kernel "
            "module (read-only by default) or apfs-fuse.",
    "hfsplus": "Make sure the hfsplus kernel module is available (modprobe hfsplus).",
}


def _mount_options(mnt: str) -> list[str]:
    try:
        with open("/proc/self/mountinfo", encoding="utf-8") as f:
            for line in f:
                fields = line.split()
                if len(fields) > 5 and fields[4].encode().decode("unicode_escape") == mnt:
                    return fields[5].split(",")
    except OSError:
        pass
    return []


def mount(dev: str, mountpoint: str | None = None, options: str | None = None, label: str = "") -> str:
    """Mount ``dev`` and return the mount point (plus a note if it ended up read-only)."""
    require_block_device(dev)
    mnt = mountpoint or default_mountpoint(dev, label)
    created = not os.path.exists(mnt)
    os.makedirs(mnt, exist_ok=True)
    argv = ["mount"]
    if options:
        argv += ["-o", options]
    fstype = probe(dev).get("TYPE", "")
    try:
        run_simple(argv + [dev, mnt])
    except CommandError as e:
        if created:
            with contextlib.suppress(OSError):
                os.rmdir(mnt)
        hint = MOUNT_HINTS.get(fstype)
        raise OpError(f"{e}\n\n{hint}" if hint else str(e)) from None
    if fstype == "hfsplus" and "ro" in _mount_options(mnt) and "ro" not in (options or "").split(","):
        return (f"{mnt} (read-only: Linux cannot write to journaled HFS+ volumes; turn journaling off "
                "on a Mac to write from Linux)")
    return mnt


def unmount(dev: str) -> None:
    require_block_device(dev)
    mps = scanner.mounted_sources().get(os.path.realpath(dev), [])
    if not mps:
        return
    for mp in sorted(mps, key=len, reverse=True):
        run_simple(["umount", mp])
        if mp.startswith("/mnt/") and os.path.isdir(mp):
            try:
                os.rmdir(mp)
            except OSError:
                pass


def swapon(dev: str) -> None:
    run_simple(["swapon", require_block_device(dev)])


def swapoff(dev: str) -> None:
    run_simple(["swapoff", require_block_device(dev)])


def luks_open(dev: str, passphrase: str, name: str | None = None) -> str:
    require_block_device(dev)
    if not name:
        u = output(["cryptsetup", "luksUUID", dev]).strip()
        name = f"luks-{u}" if u else f"luks-{os.path.basename(dev)}"
    run_simple(["cryptsetup", "open", "--key-file=-", dev, name], input=passphrase)
    return f"/dev/mapper/{name}"


def luks_close(dev: str) -> None:
    require_block_device(dev)
    for node in scanner.lsblk_json([dev]):
        for child in node.get("children") or []:
            if child.get("type") == "crypt":
                run_simple(["cryptsetup", "close", child.get("name")])


def power_off(disk: str) -> None:
    """Safely detach a removable drive (udisks2 when available)."""
    require_block_device(disk)
    if have("udisksctl"):
        run_simple(["udisksctl", "power-off", "--no-user-interaction", "-b", disk])
    else:
        run_simple(["sync"])
        base = os.path.basename(os.path.realpath(disk))
        with open(f"/sys/block/{base}/device/delete", "w") as f:
            f.write("1")


# -- long running tasks -------------------------------------------------------
def _open_reader(path: str):
    lower = path.lower()
    if lower.endswith(".gz"):
        return gzip.open(path, "rb")
    if lower.endswith(".xz"):
        return lzma.open(path, "rb")
    if lower.endswith(".bz2"):
        return bz2.open(path, "rb")
    if lower.endswith(".zst"):
        try:
            from compression import zstd  # type: ignore[import-not-found]  # Python 3.14+
            return zstd.open(path, "rb")
        except ImportError:
            raise OpError("Reading .zst images needs Python 3.14 or newer; decompress it first") from None
    return open(path, "rb", buffering=0)


def _uncompressed_size(path: str) -> int | None:
    lower = path.lower()
    if lower.endswith((".gz", ".xz", ".bz2", ".zst")):
        return None
    return os.path.getsize(path)


def restore_image(ctx: ExecContext, image: str, target: str) -> None:
    """Write an image file (ISO, .img, .img.xz, ...) to a disk or partition."""
    require_block_device(target)
    if not os.path.isfile(image):
        raise OpError(f"{image} is not a file")
    total = device_size(target)
    size = _uncompressed_size(image)
    if size is not None and size > total:
        raise OpError(f"The image ({format_size(size)}) is larger than {target} ({format_size(total)})")
    ctx.step(f"Writing {os.path.basename(image)} to {target}")
    ctx.log(f"$ write {image} -> {target}")
    written = 0
    started = time.monotonic()
    last = 0.0
    with _open_reader(image) as fi, open(target, "r+b", buffering=0) as fo:
        while True:
            if ctx.cancel.is_set():
                raise OpError("Cancelled")
            data = fi.read(CHUNK)
            if not data:
                break
            if written + len(data) > total:
                raise OpError("The image is larger than the target device")
            view = memoryview(data)
            off = 0
            while off < len(data):
                off += fo.write(view[off:])
            written += len(data)
            now = time.monotonic()
            if now - last > 0.25:
                last = now
                frac = written / size if size else None
                ctx.emit({"type": "progress", "fraction": frac, "rate": written / max(0.001, now - started),
                          "done": written})
        ctx.step("Flushing data to the device")
        os.fsync(fo.fileno())
    ctx.emit({"type": "progress", "fraction": 1.0})
    ctx.log(f"Wrote {format_size(written)}")
    _reread(ctx, target)


def create_image(ctx: ExecContext, source: str, image: str, compress: bool = False) -> None:
    require_block_device(source)
    total = device_size(source)
    ctx.step(f"Reading {source} into {image}")
    ctx.log(f"$ read {source} -> {image}")
    opener = (lambda p: gzip.open(p, "wb", compresslevel=3)) if compress else (lambda p: open(p, "wb"))
    done = 0
    started = time.monotonic()
    last = 0.0
    with open(source, "rb", buffering=0) as fi, opener(image) as fo:
        while done < total:
            if ctx.cancel.is_set():
                raise OpError("Cancelled")
            data = fi.read(min(CHUNK, total - done))
            if not data:
                break
            fo.write(data)
            done += len(data)
            now = time.monotonic()
            if now - last > 0.25:
                last = now
                ctx.emit({"type": "progress", "fraction": done / total,
                          "rate": done / max(0.001, now - started)})
    ctx.emit({"type": "progress", "fraction": 1.0})
    ctx.log(f"Saved {format_size(done)} to {image}")


def clone_disk(ctx: ExecContext, source: str, target: str, new_ids: bool = True) -> None:
    """Sector-by-sector copy of a whole disk onto another (larger or equal) disk."""
    require_block_device(source)
    require_block_device(target)
    if os.path.realpath(source) == os.path.realpath(target):
        raise OpError("Source and target are the same device")
    src_size, dst_size = device_size(source), device_size(target)
    if dst_size < src_size:
        raise OpError(f"The target ({format_size(dst_size)}) is smaller than the source ({format_size(src_size)})")
    ctx.step(f"Cloning {source} to {target} ({format_size(src_size)})")
    ctx.copy(source, target, src_size)
    _reread(ctx, target)
    pt = scanner.sfdisk_dump(target)
    if pt and pt.get("label") == "gpt":
        if dst_size > src_size:
            ctx.step("Moving the backup GPT header to the end of the target disk")
            ctx.run(["sfdisk", "--no-reread", "--relocate", "gpt-bak-std", target], check=False)
        if new_ids:
            ctx.step("Giving the clone new disk and partition GUIDs")
            ctx.run(["sfdisk", "--no-reread", "--disk-id", target, str(uuidlib.uuid4()).upper()])
            for idx, e in enumerate(pt.get("partitions", [])):
                num = scanner._num_from_node(e.get("node", "")) or idx + 1
                ctx.run(["sfdisk", "--no-reread", "--part-uuid", target, str(num), str(uuidlib.uuid4()).upper()])
    elif pt and pt.get("label") == "dos" and new_ids:
        ctx.step("Giving the clone a new disk identifier")
        ctx.run(["sfdisk", "--no-reread", "--disk-id", target, "0x" + os.urandom(4).hex()])
    _reread(ctx, target)
    ctx.log("Note: filesystem UUIDs are identical on both disks. Disconnect one of them, or "
            "give the clone's partitions new UUIDs, before booting with both attached.")


def _reread(ctx: ExecContext, disk: str) -> None:
    base = os.path.basename(os.path.realpath(disk))
    if os.path.exists(f"/sys/block/{base}"):
        ctx.sync_kernel(disk)


def surface_test(ctx: ExecContext, dev: str) -> list[tuple[int, int]]:
    """Read every sector of ``dev`` (non destructive). Returns bad byte ranges."""
    require_block_device(dev)
    total = device_size(dev)
    bad: list[tuple[int, int]] = []
    ctx.step(f"Reading {format_size(total)} from {dev}")
    flags = os.O_RDONLY | getattr(os, "O_DIRECT", 0)
    try:
        fd = os.open(dev, flags)
    except OSError:
        fd = os.open(dev, os.O_RDONLY)
    buf = mmap.mmap(-1, CHUNK)  # page aligned, as O_DIRECT requires
    sector = 4096
    started = time.monotonic()
    last = 0.0
    done = 0
    try:
        while done < total:
            if ctx.cancel.is_set():
                ctx.log("Cancelled")
                break
            n = min(CHUNK, total - done)
            try:
                if n == CHUNK:
                    os.preadv(fd, [buf], done)
                else:
                    os.pread(fd, n, done)
            except OSError:
                # Narrow the error down to individual blocks.
                off = done
                while off < done + n:
                    try:
                        os.pread(fd, min(sector, done + n - off), off)
                    except OSError:
                        if bad and bad[-1][0] + bad[-1][1] == off:
                            bad[-1] = (bad[-1][0], bad[-1][1] + sector)
                        else:
                            bad.append((off, sector))
                        ctx.log(f"Read error at byte {off} (sector {off // 512})")
                    off += sector
            done += n
            now = time.monotonic()
            if now - last > 0.3:
                last = now
                ctx.emit({"type": "progress", "fraction": done / total,
                          "rate": done / max(0.001, now - started), "bad": len(bad)})
    finally:
        os.close(fd)
        buf.close()
    ctx.emit({"type": "progress", "fraction": 1.0, "bad": len(bad)})
    ctx.log(f"Surface test finished: {len(bad)} bad region(s)")
    return bad


def all_supported_filesystems() -> list[str]:
    return [fs.id for fs in BY_ID.values() if fs.supports("create")]

