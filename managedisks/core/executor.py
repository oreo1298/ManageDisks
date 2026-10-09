"""Execute a queue of operations (must run as root)."""

from __future__ import annotations

import functools
import os
import random
import re
import tempfile
import threading
import time
import traceback
from typing import Callable

from . import scanner
from .model import Disk, find_disk
from .operations import Operation, OpError, redact, simulate
from .runner import Cancelled, CommandError, Runner, have, run_simple

Event = dict
Emit = Callable[[Event], None]

CHUNK = 4 * 1024 * 1024
LOG_DIR = "/var/log/managedisks"


@functools.lru_cache(maxsize=1)
def sfdisk_supports_lock() -> bool:
    try:
        return "--lock" in run_simple(["sfdisk", "--help"], check=False).output
    except CommandError:
        return False


_MOVE_RE = re.compile(r"Moved\s+(\d+)\s+from\s+(\d+)\s+sectors")


def _move_progress(line: str) -> float | None:
    m = _MOVE_RE.search(line)
    if m and int(m.group(2)):
        return int(m.group(1)) / int(m.group(2))
    return None


class ExecContext:
    def __init__(self, emit: Emit, cancel: threading.Event | None = None, dry_run: bool = False):
        self.emit = emit
        self.cancel = cancel or threading.Event()
        self.dry_run = dry_run
        self.runner = Runner(on_line=self.log, on_progress=self.progress, cancel=self.cancel, dry_run=dry_run)
        self.preview: list[Disk] | None = None   # dry runs: state before the current op
        self.post: list[Disk] | None = None      # dry runs: state after the current op
        self._last_progress = 0.0

    # -- events -------------------------------------------------------------
    def log(self, line: str) -> None:
        self.emit({"type": "log", "line": line})

    def step(self, text: str) -> None:
        self.emit({"type": "step", "text": text})
        self.log("» " + text)
        self.progress(None)

    def progress(self, fraction: float | None) -> None:
        now = time.monotonic()
        if fraction is not None and 0 < fraction < 1 and now - self._last_progress < 0.15:
            return
        self._last_progress = now
        self.emit({"type": "progress", "fraction": fraction})

    def run(self, argv, **kw):
        return self.runner.run(argv, **kw)

    # -- disk state ---------------------------------------------------------
    def disk(self, path: str) -> Disk:
        if self.dry_run and self.preview is not None:
            d = find_disk(self.preview, path)
            if d is None:
                raise OpError(f"Disk {path} not found")
            return d.clone()
        for d in scanner.scan(include_loop=True, deep=True):
            if d.path == path:
                return d
        raise OpError(f"Disk {path} not found")

    def number(self, disk_path: str, start: int) -> int:
        d = self.disk(disk_path)
        p = d.find(start)
        if p is None and self.dry_run and self.post is not None:
            post = find_disk(self.post, disk_path)
            p = post.find(start) if post else None
        if p is None:
            raise OpError(f"No partition starts at sector {start} on {disk_path}")
        return p.number

    def locate(self, disk_path: str, start: int) -> str:
        """Device node of the partition starting at ``start`` (waits for udev)."""
        num = self.number(disk_path, start)
        if self.dry_run:
            return scanner.partition_path(disk_path, num) if num else f"{disk_path}<new>"
        path = scanner.partition_path(disk_path, num)
        for _ in range(100):
            if os.path.exists(path):
                return path
            time.sleep(0.1)
            path = scanner.partition_path(disk_path, num)
        raise OpError(f"Device node for partition {num} of {disk_path} did not appear")

    def settle(self) -> None:
        if self.dry_run:
            return
        if have("udevadm"):
            run_simple(["udevadm", "settle", "--timeout=15"], check=False, timeout=30)

    def sync_kernel(self, disk_path: str) -> None:
        """Bring the kernel's partition list in line with the on-disk table.

        Uses partx (BLKPG ioctls) per partition, which also works while other
        partitions of the disk are mounted, unlike re-reading the whole table.
        """
        if self.dry_run:
            return
        pt = scanner.sfdisk_dump(disk_path)
        want: dict[int, tuple[int, int, bool]] = {}
        if pt:
            ss = int(pt.get("sectorsize", 512))
            for idx, e in enumerate(pt.get("partitions", [])):
                num = scanner._num_from_node(e.get("node", "")) or idx + 1
                ext = str(e.get("type", "")).lower() in ("5", "f", "85", "05", "0f")
                want[num] = (int(e["start"]) * ss // 512, int(e["size"]) * ss // 512, ext)
        have_k = scanner.kernel_partitions(disk_path)
        quiet = dict(check=False, quiet=False)
        # 1. remove partitions that vanished or moved
        for num in sorted(have_k, reverse=True):
            if num not in want or have_k[num][0] != want[num][0]:
                self.run(["partx", "-d", "--nr", str(num), disk_path], **quiet)
        have_k = scanner.kernel_partitions(disk_path)
        # 2. resize changed ones
        for num, (start, size, ext) in want.items():
            if num in have_k and not ext and have_k[num][1] != size:
                self.run(["partx", "-u", "--nr", str(num), disk_path], **quiet)
        # 3. add new ones
        for num in sorted(want):
            if num not in have_k:
                self.run(["partx", "-a", "--nr", str(num), disk_path], **quiet)
        self.settle()
        have_k = scanner.kernel_partitions(disk_path)
        bad = [n for n, (s, z, ext) in want.items()
               if n not in have_k or have_k[n][0] != s or (not ext and have_k[n][1] != z)]
        if bad:
            # Last resort: ask the kernel to re-read the whole table.
            self.run(["partx", "-u", disk_path], **quiet)
            self.settle()
            have_k = scanner.kernel_partitions(disk_path)
            bad = [n for n, (s, z, ext) in want.items()
                   if n not in have_k or have_k[n][0] != s or (not ext and have_k[n][1] != z)]
            if bad:
                raise OpError(
                    "The partition table was written, but the kernel could not be updated for "
                    f"partition(s) {', '.join(map(str, bad))} (probably because they are in use). "
                    "Reboot to make the changes visible.")

    def sfdisk(self, disk_path: str, args: list[str], script: str | None = None,
               device_arg: bool = True, wipe: str | None = None) -> None:
        argv = ["sfdisk", "--no-reread", "--no-tell-kernel"]
        if sfdisk_supports_lock():
            argv.append("--lock")
        if wipe:
            argv += ["--wipe", wipe]
        argv += args
        if device_arg:
            argv.append(disk_path)
        if script is not None:
            for line in script.strip().splitlines():
                self.log("  | " + line)
        self.run(argv, input=script)
        self.sync_kernel(disk_path)

    def move(self, disk_path: str, number: int, new_start: int, size: int) -> None:
        logdir = LOG_DIR if os.access(os.path.dirname(LOG_DIR), os.W_OK) else tempfile.gettempdir()
        os.makedirs(logdir, exist_ok=True)
        logfile = os.path.join(logdir, f"move-{os.path.basename(disk_path)}-{number}-{int(time.time())}")
        argv = ["sfdisk", "--no-reread", "--no-tell-kernel", "-N", str(number),
                f"--move-data={logfile}", disk_path]
        script = f"start={new_start}, size={size}\n"
        self.log("  | " + script.strip())
        # Interrupting a data move would corrupt the partition (runs are not
        # cancellable by default).
        self.run(argv, input=script, pty=True, progress=_move_progress)
        self.sync_kernel(disk_path)

    # -- raw data -------------------------------------------------------------
    def _io_loop(self, nbytes: int, write_chunk: Callable[[int, int], None], cancellable: bool = True):
        done = 0
        last = 0.0
        started = time.monotonic()
        while done < nbytes:
            if cancellable and self.cancel.is_set():
                raise Cancelled("Operation cancelled")
            n = min(CHUNK, nbytes - done)
            write_chunk(done, n)
            done += n
            now = time.monotonic()
            if now - last > 0.25:
                last = now
                rate = done / max(0.001, now - started)
                self.emit({"type": "progress", "fraction": done / nbytes, "rate": rate})
        self.emit({"type": "progress", "fraction": 1.0})

    def copy(self, src: str, dst: str, nbytes: int, *, cancellable: bool = True) -> None:
        self.log(f"$ copy {src} -> {dst} ({nbytes} bytes)")
        if self.dry_run:
            return
        buf = bytearray(CHUNK)
        view = memoryview(buf)
        with open(src, "rb", buffering=0) as fi, open(dst, "r+b", buffering=0) as fo:
            def chunk(offset, n):
                got = 0
                while got < n:
                    r = fi.readinto(view[got:n])
                    if not r:
                        raise OpError(f"Unexpected end of {src} after {offset + got} bytes")
                    got += r
                written = 0
                while written < n:
                    written += fo.write(view[written:n])
            self._io_loop(nbytes, chunk, cancellable)
            os.fsync(fo.fileno())

    def fill(self, target: str, nbytes: int, random_data: bool = False) -> None:
        rnd = random_data
        self.log(f"$ overwrite {target} with {'random data' if rnd else 'zeros'} ({nbytes} bytes)")
        if self.dry_run:
            return
        zeros = bytes(CHUNK)
        rng = _Rng()
        with open(target, "r+b", buffering=0) as fo:
            def chunk(offset, n):
                data = rng.bytes(n) if rnd else zeros[:n]
                written = 0
                while written < n:
                    written += fo.write(data[written:])
            self._io_loop(nbytes, chunk)
            os.fsync(fo.fileno())

    def zero_range(self, target: str, offset: int, length: int) -> None:
        self.log(f"$ zero {length} bytes of {target} at offset {offset}")
        if self.dry_run or length <= 0:
            return
        with open(target, "r+b", buffering=0) as fo:
            fo.seek(offset)
            fo.write(bytes(length))
            os.fsync(fo.fileno())


class _Rng:
    """Fast non-cryptographic random data for wiping (os.urandom is slow)."""

    def __init__(self):
        self._r = random.Random(os.urandom(16))

    def bytes(self, n: int) -> bytes:
        return self._r.randbytes(n)


def _audit_log(text: str) -> None:
    try:
        os.makedirs(LOG_DIR, mode=0o755, exist_ok=True)
        with open(os.path.join(LOG_DIR, "operations.log"), "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + text + "\n")
    except OSError:
        pass


def execute(ops: list[Operation], emit: Emit, cancel: threading.Event | None = None,
            dry_run: bool = False, disks: list[Disk] | None = None) -> bool:
    """Validate the whole queue against the current disks, then run it.

    Returns True if every operation succeeded.
    """
    ctx = ExecContext(emit, cancel, dry_run)
    count = len(ops)
    try:
        current = disks if disks is not None else scanner.scan(include_loop=True, deep=not dry_run)
        simulate(current, ops)  # also fills in display names for describe()
    except OpError as e:
        emit({"type": "done", "ok": False, "completed": 0,
              "error": f"The pending operations are no longer valid: {e}"})
        return False
    except Exception as e:  # noqa: BLE001
        emit({"type": "done", "ok": False, "completed": 0, "error": str(e)})
        return False

    preview = [d.clone() for d in current]
    for i, op in enumerate(ops):
        desc = op.describe()
        emit({"type": "op_start", "index": i, "count": count, "text": desc})
        if not dry_run:
            _audit_log(f"START {desc} {redact(op.to_dict())}")
        if dry_run:
            ctx.preview = preview
            ctx.post = [d.clone() for d in preview]
            op.apply(ctx.post)
        try:
            op.execute(ctx)
            ctx.runner.run(["sync"], check=False, quiet=True, mutating=False)
            if dry_run:
                preview = ctx.post
        except Cancelled:
            emit({"type": "op_failed", "index": i, "error": "Cancelled"})
            emit({"type": "done", "ok": False, "completed": i, "error": "Cancelled by user"})
            if not dry_run:
                _audit_log(f"CANCELLED {desc}")
            return False
        except (OpError, CommandError, OSError) as e:
            emit({"type": "op_failed", "index": i, "error": str(e)})
            emit({"type": "done", "ok": False, "completed": i, "error": str(e)})
            if not dry_run:
                _audit_log(f"FAILED {desc}: {e}")
            return False
        except Exception as e:  # noqa: BLE001
            ctx.log(traceback.format_exc())
            emit({"type": "op_failed", "index": i, "error": f"Internal error: {e}"})
            emit({"type": "done", "ok": False, "completed": i, "error": f"Internal error: {e}"})
            return False
        emit({"type": "op_done", "index": i})
        if not dry_run:
            _audit_log(f"DONE {desc}")
    emit({"type": "done", "ok": True, "completed": count, "error": None})
    return True
