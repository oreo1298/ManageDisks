"""JSON-lines server run as root (see package docstring)."""

from __future__ import annotations

import json
import os
import sys
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor

from .. import __version__
from ..core import executor, filesystems, scanner, smart, tasks
from ..core.executor import ExecContext
from ..core.operations import Operation, OpError
from ..core.runner import CommandError
from . import PROTOCOL_VERSION


class Server:
    def __init__(self, inp=None, out=None):
        self.inp = inp or sys.stdin
        self.out = out or sys.stdout
        self.lock = threading.Lock()
        self.cancels: dict[int, threading.Event] = {}
        self.pool = ThreadPoolExecutor(max_workers=4)
        # Operations that change disks are serialised.
        self.write_lock = threading.Lock()

    def send(self, obj: dict) -> None:
        line = json.dumps(obj, default=str)
        with self.lock:
            self.out.write(line + "\n")
            self.out.flush()

    def serve(self) -> None:
        self.send({"event": {"type": "ready", "version": __version__, "protocol": PROTOCOL_VERSION,
                             "uid": os.geteuid()}})
        for line in self.inp:
            line = line.strip()
            if not line:
                continue
            try:
                req = json.loads(line)
            except ValueError:
                continue
            rid = req.get("id")
            method = req.get("method", "")
            params = req.get("params") or {}
            if method == "cancel":
                ev = self.cancels.get(params.get("target"))
                if ev:
                    ev.set()
                self.send({"id": rid, "result": True})
                continue
            if method == "quit":
                self.send({"id": rid, "result": True})
                break
            ev = threading.Event()
            self.cancels[rid] = ev
            self.pool.submit(self._handle, rid, method, params, ev)
        # stdin closed: let running jobs finish (aborting a partition move
        # half way would be far worse), then exit.
        self.pool.shutdown(wait=True)

    def _handle(self, rid, method, params, cancel):
        fn = getattr(self, "m_" + method, None)
        try:
            if fn is None:
                raise OpError(f"Unknown method {method}")
            result = fn(params, lambda ev: self.send({"id": rid, "event": ev}), cancel)
            self.send({"id": rid, "result": result})
        except (OpError, CommandError, OSError, ValueError, RuntimeError) as e:
            self.send({"id": rid, "error": str(e)})
        except Exception as e:  # noqa: BLE001
            traceback.print_exc(file=sys.stderr)
            self.send({"id": rid, "error": f"Internal error: {e}"})
        finally:
            self.cancels.pop(rid, None)

    # -- methods --------------------------------------------------------------
    def m_ping(self, p, emit, cancel):
        return {"version": __version__, "uid": os.geteuid()}

    def m_scan(self, p, emit, cancel):
        disks = scanner.scan(include_loop=bool(p.get("include_loop")), deep=True)
        if p.get("usage", True):
            scanner.fill_usage(disks)
        return [d.to_dict() for d in disks]

    def m_usage(self, p, emit, cancel):
        return tasks.fs_usage(p["device"], p.get("fstype", ""), p.get("fsversion", ""))

    def m_min_size(self, p, emit, cancel):
        return tasks.fs_min_size(p["device"], p.get("fstype", ""), p.get("fsversion", ""), p.get("mountpoint"))

    def m_apply(self, p, emit, cancel):
        ops = [Operation.from_dict(d) for d in p.get("ops", [])]
        with self.write_lock:
            ok = executor.execute(ops, emit, cancel, dry_run=bool(p.get("dry_run")))
        return {"ok": ok}

    def m_mount(self, p, emit, cancel):
        return tasks.mount(p["device"], p.get("mountpoint"), p.get("options"), p.get("label", ""))

    def m_unmount(self, p, emit, cancel):
        tasks.unmount(p["device"])
        return True

    def m_swapon(self, p, emit, cancel):
        tasks.swapon(p["device"])
        return True

    def m_swapoff(self, p, emit, cancel):
        tasks.swapoff(p["device"])
        return True

    def m_luks_open(self, p, emit, cancel):
        return tasks.luks_open(p["device"], p["passphrase"])

    def m_luks_close(self, p, emit, cancel):
        tasks.luks_close(p["device"])
        return True

    def m_power_off(self, p, emit, cancel):
        tasks.power_off(p["device"])
        return True

    def m_smart(self, p, emit, cancel):
        return smart.read_raw(tasks.require_block_device(p["device"]))

    def m_smart_test(self, p, emit, cancel):
        return smart.start_self_test(tasks.require_block_device(p["device"]), p.get("kind", "short"))

    def _ctx(self, emit, cancel):
        return ExecContext(emit, cancel)

    def m_surface_test(self, p, emit, cancel):
        return tasks.surface_test(self._ctx(emit, cancel), p["device"])

    def m_restore_image(self, p, emit, cancel):
        with self.write_lock:
            tasks.restore_image(self._ctx(emit, cancel), p["image"], p["device"])
        return True

    def m_create_image(self, p, emit, cancel):
        tasks.create_image(self._ctx(emit, cancel), p["device"], p["image"], bool(p.get("compress")))
        owner = p.get("owner")
        if owner:  # hand the file to the user who asked for it
            try:
                os.chown(p["image"], int(owner[0]), int(owner[1]))
            except OSError:
                pass
        return True

    def m_clone_disk(self, p, emit, cancel):
        with self.write_lock:
            tasks.clone_disk(self._ctx(emit, cancel), p["source"], p["target"], bool(p.get("new_ids", True)))
        return True

    def m_support(self, p, emit, cancel):
        rows = filesystems.support_matrix()
        return [{"id": r.fs.id, "caps": r.caps, "missing": r.missing, "packages": r.packages} for r in rows]


def main() -> int:
    if os.geteuid() != 0:
        print("managedisks-helper must run as root (it is started through pkexec)", file=sys.stderr)
        return 1
    os.umask(0o022)
    # Never let an inherited environment influence a root process.
    for var in ("PYTHONPATH", "PYTHONHOME", "LD_PRELOAD", "LD_LIBRARY_PATH"):
        os.environ.pop(var, None)
    os.chdir("/")
    out = os.fdopen(os.dup(sys.stdout.fileno()), "w", buffering=1, encoding="utf-8")
    # Anything a library prints must not corrupt the protocol stream.
    sys.stdout = sys.stderr
    Server(sys.stdin, out).serve()
    return 0
