"""An interactive console that feels like Windows' DISKPART.

    MANAGEDISKS> list disk
    MANAGEDISKS> select disk 1
    MANAGEDISKS> create partition primary size=20480
    MANAGEDISKS> format fs=ext4 label="data" quick
    MANAGEDISKS> assign

Sizes follow diskpart: SIZE/DESIRED/MINIMUM are in MB (MiB) and OFFSET in
KB unless a unit is given (SIZE=20G works too).
"""

from __future__ import annotations

import os
import shlex
import sys

from .. import __version__
from ..core import filesystems, parttypes, tasks
from ..core.model import Disk, Partition
from ..core.operations import (CheckFilesystem, ConvertTable, CreatePartition, CreateTable, DeletePartition,
                               FormatPartition, NewUUID, ResizeMovePartition, SetLabel, SetPartitionProps, Wipe)
from ..core.units import KiB, MiB, SizeError, align_down, align_up, format_size, parse_size
from .common import (CliError, bold, dim, fmt_part_fs, green, plan_one, red, run_ops, scan, table,
                     yellow)

HELP = {
    "list": "LIST DISK | LIST PARTITION | LIST VOLUME — show objects; * marks the selection",
    "select": "SELECT DISK n | SELECT PARTITION n | SELECT VOLUME n — choose the object to work on "
              "(n can also be a device such as /dev/sdb)",
    "detail": "DETAIL DISK | DETAIL PARTITION — properties of the selected object",
    "create": "CREATE PARTITION PRIMARY|EXTENDED|LOGICAL|EFI|MSR [SIZE=n] [OFFSET=n] [FS=fs] [LABEL=x] "
              "— new partition (SIZE in MB, OFFSET in KB; default: largest free region)",
    "delete": "DELETE PARTITION — delete the selected partition",
    "format": "FORMAT [FS=ext4] [LABEL=\"name\"] [QUICK] — format the selected partition",
    "extend": "EXTEND [SIZE=n] — grow the selected partition into the free space after it (MB)",
    "shrink": "SHRINK DESIRED=n | SHRINK QUERYMAX — shrink the selected partition by n MB",
    "clean": "CLEAN [ALL] — remove all partitions/signatures (ALL overwrites the whole disk with zeros)",
    "convert": "CONVERT GPT | CONVERT MBR — create a table on an empty disk or convert keeping data",
    "assign": "ASSIGN [MOUNT=path] — mount the selected partition (default under /mnt)",
    "remove": "REMOVE — unmount the selected partition",
    "active": "ACTIVE — mark the selected MBR partition bootable",
    "inactive": "INACTIVE — clear the MBR bootable flag",
    "set": "SET ID=type — set partition type (linux, esp, swap, msdata, 83, ef, a GPT GUID, ...)",
    "gpt": "GPT ATTRIBUTES=0x8000000000000001 — set GPT attribute bits",
    "label": "LABEL \"text\" — set the filesystem label of the selected partition",
    "uuid": "UUID — give the filesystem of the selected partition a new UUID",
    "check": "CHECK [SCAN] — check/repair the filesystem (SCAN: report only)",
    "rescan": "RESCAN — reread disks",
    "exit": "EXIT — leave",
}

ALIASES = {"sel": "select", "del": "delete", "lis": "list", "det": "detail", "cre": "create",
           "quit": "exit", "chkdsk": "check", "?": "help"}


def _kv(args: list[str]) -> tuple[dict[str, str], set[str]]:
    kv, flags = {}, set()
    for a in args:
        if "=" in a:
            k, v = a.split("=", 1)
            kv[k.lower()] = v
        else:
            flags.add(a.lower())
    return kv, flags


class DiskShell:
    def __init__(self, include_loop: bool = False, dry_run: bool = False, assume_yes: bool = False):
        self.include_loop = include_loop
        self.dry_run = dry_run
        self.assume_yes = assume_yes
        self.disks: list[Disk] = []
        self.sel_disk: str | None = None
        self.sel_part: int | None = None   # start sector
        self.rescan()

    # -- state ------------------------------------------------------------------
    def rescan(self):
        self.disks = scan(self.include_loop or os.environ.get("MANAGEDISKS_SHOW_LOOP") == "1")

    def disk(self) -> Disk:
        for d in self.disks:
            if d.path == self.sel_disk:
                return d
        raise CliError("There is no disk selected. Use SELECT DISK.")

    def part(self) -> Partition:
        d = self.disk()
        p = d.find(self.sel_part) if self.sel_part is not None else None
        if p is None:
            raise CliError("There is no partition selected. Use SELECT PARTITION.")
        return p

    def volumes(self) -> list[tuple[Disk, Partition]]:
        return [(d, p) for d in self.disks for p in d.sorted_partitions() if p.fstype]

    # -- loop -----------------------------------------------------------------
    def run(self):
        print(bold(f"ManageDisks shell {__version__}") + dim("  — type HELP for commands, EXIT to leave"))
        if os.geteuid() != 0 and not self.dry_run:
            print(yellow("Not running as root: you can look around, but changes need sudo."))
        while True:
            try:
                line = input("\nMANAGEDISKS> ")
            except (EOFError, KeyboardInterrupt):
                print()
                return
            try:
                if self.execute(line) is False:
                    return
            except CliError as e:
                print(red(str(e)))
            except (OSError, RuntimeError, ValueError) as e:
                print(red(f"Error: {e}"))

    def execute(self, line: str):
        line = line.strip()
        if not line or line.startswith(("#", "rem ", "REM ")):
            return None
        try:
            words = shlex.split(line)
        except ValueError as e:
            raise CliError(str(e)) from None
        verb = words[0].lower()
        verb = ALIASES.get(verb, verb)
        if verb == "exit":
            return False
        fn = getattr(self, "do_" + verb, None)
        if fn is None:
            raise CliError(f"Unknown command {words[0]!r}. Type HELP.")
        return fn(words[1:])

    def _apply(self, op, keep_selection=True):
        ops = plan_one(self.disks, op)
        ok = run_ops(ops, dry_run=self.dry_run, assume_yes=self.assume_yes, disks=self.disks)
        if not self.dry_run:
            self.rescan()
        return ok

    # -- commands --------------------------------------------------------------
    def do_help(self, args):
        if args:
            key = ALIASES.get(args[0].lower(), args[0].lower())
            print(HELP.get(key, f"No help for {args[0]}"))
            return
        for k in sorted(HELP):
            print(f"  {bold(k.upper()):<20} {HELP[k].split(' — ')[-1]}")

    def do_rescan(self, args):
        self.rescan()
        print("Disks rescanned.")

    def do_list(self, args):
        what = (args[0].lower() if args else "")
        if what.startswith("dis"):
            rows = []
            for i, d in enumerate(self.disks):
                mark = "*" if d.path == self.sel_disk else " "
                rows.append([mark, f"Disk {i}", d.path, format_size(d.size), format_size(d.free_bytes()),
                             parttypes.table_label(d.table), d.title()])
            print(table(rows, [" ", "Disk ###", "Device", "Size", "Free", "Table", "Model"], indent=1))
        elif what.startswith("par"):
            d = self.disk()
            rows = []
            for p in d.sorted_partitions():
                mark = "*" if p.start == self.sel_part else " "
                kind = {"normal": "", "primary": "Primary", "extended": "Extended", "logical": "Logical"}[p.kind]
                tname = parttypes.type_name(p.type_id, d.table)
                rows.append([mark, f"Partition {p.number}", (kind + " " + tname).strip(),
                             format_size(p.size * d.sector_size), format_size(p.start * d.sector_size),
                             p.path])
            if not rows:
                print("There are no partitions on this disk to show.")
                return
            print(table(rows, [" ", "Partition ###", "Type", "Size", "Offset", "Device"], indent=1))
        elif what.startswith("vol"):
            rows = []
            for i, (d, p) in enumerate(self.volumes()):
                mark = "*" if (d.path == self.sel_disk and p.start == self.sel_part) else " "
                rows.append([mark, f"Volume {i}", p.path, p.label, fmt_part_fs(p),
                             format_size(p.size * d.sector_size), ", ".join(p.all_mountpoints())])
            print(table(rows, [" ", "Volume ###", "Device", "Label", "Fs", "Size", "Mounted"], indent=1))
        else:
            raise CliError("Usage: LIST DISK | LIST PARTITION | LIST VOLUME")

    def do_select(self, args):
        if len(args) < 2:
            raise CliError("Usage: SELECT DISK n | SELECT PARTITION n | SELECT VOLUME n")
        what, ref = args[0].lower(), args[1]
        if what.startswith("dis"):
            if ref.startswith("/dev/"):
                match = [d for d in self.disks if d.path == ref]
            else:
                idx = int(ref) if ref.isdigit() else -1
                match = [self.disks[idx]] if 0 <= idx < len(self.disks) else []
            if not match:
                raise CliError("The disk you specified is not valid.")
            self.sel_disk, self.sel_part = match[0].path, None
            print(f"Disk {self.disks.index(match[0])} ({match[0].path}) is now the selected disk.")
        elif what.startswith("par"):
            d = self.disk()
            p = None
            if ref.startswith("/dev/"):
                p = next((q for q in d.partitions if q.path == ref), None)
            elif ref.isdigit():
                p = d.by_number(int(ref))
            if p is None:
                raise CliError("The partition you specified is not valid.")
            self.sel_part = p.start
            print(f"Partition {p.number} ({p.path}) is now the selected partition.")
        elif what.startswith("vol"):
            vols = self.volumes()
            idx = int(ref) if ref.isdigit() else -1
            if not 0 <= idx < len(vols):
                raise CliError("The volume you specified is not valid.")
            d, p = vols[idx]
            self.sel_disk, self.sel_part = d.path, p.start
            print(f"Volume {idx} ({p.path}) is the selected volume.")
        else:
            raise CliError("Usage: SELECT DISK n | SELECT PARTITION n | SELECT VOLUME n")

    def do_detail(self, args):
        what = args[0].lower() if args else ""
        from .main import cmd_info

        class A:
            json = False
        a = A()
        if what.startswith("dis"):
            a.device = self.disk().path
        elif what.startswith(("par", "vol")):
            a.device = self.part().path
        else:
            raise CliError("Usage: DETAIL DISK | DETAIL PARTITION")
        cmd_info(a)

    def _size(self, text: str, default_unit: int, d: Disk) -> int:
        try:
            return parse_size(text, default_unit=default_unit, sector_size=d.sector_size)
        except SizeError as e:
            raise CliError(str(e)) from None

    def do_create(self, args):
        if len(args) < 2 or not args[0].lower().startswith("par"):
            raise CliError(HELP["create"])
        d = self.disk()
        if not d.table:
            raise CliError("The disk has no partition table. Use CONVERT GPT or CONVERT MBR first.")
        kind_word = args[1].lower()
        kv, flags = _kv(args[2:])
        kind = {"primary": "normal", "extended": "extended", "logical": "logical", "efi": "normal",
                "msr": "normal"}.get(kind_word)
        if kind is None:
            raise CliError(HELP["create"])
        logical = kind == "logical"
        frees = [f for f in d.free_spaces() if f.inside_extended == logical]
        if not frees:
            raise CliError("There is not enough usable free space on the disk.")
        start_req = None
        if "offset" in kv:
            start_req = self._size(kv["offset"], KiB, d) // d.sector_size
            free = next((f for f in frees if f.start <= start_req <= f.end), None)
            if free is None:
                raise CliError("The offset is not inside unallocated space.")
        else:
            free = max(frees, key=lambda f: f.size)
        ustart, usize = d.usable_extent(free)
        start = max(ustart, align_up(start_req, d.alignment)) if start_req is not None else ustart
        avail = free.end + 1 - start
        defaults = {"efi": 512 * MiB, "msr": 16 * MiB}
        if "size" in kv:
            size = self._size(kv["size"], MiB, d) // d.sector_size
        elif kind_word in defaults:
            size = defaults[kind_word] // d.sector_size
        else:
            size = align_down(avail, d.alignment) or avail
        if size > avail:
            raise CliError(f"There is not enough space: only {format_size(avail * d.sector_size)} available.")
        fs = kv.get("fs", "")
        type_id = kv.get("id", "")
        if kind_word == "efi":
            fs, type_id = fs or "fat32", "esp"
            if d.table != "gpt":
                type_id = "ef"
        elif kind_word == "msr":
            fs, type_id = "", "msr"
        from .common import fs_choice
        fstype = fs_choice(fs) if fs else ""
        if fstype == "cleared":
            fstype = ""
        op = CreatePartition(d.path, start=start, size=size, kind=kind, fstype=fstype if kind != "extended" else "",
                             label=kv.get("label", ""), type_id=type_id)
        if self._apply(op):
            self.sel_part = start
            print(green("ManageDisks succeeded in creating the specified partition."))

    def do_delete(self, args):
        if not args or not args[0].lower().startswith("par"):
            raise CliError("Usage: DELETE PARTITION")
        p = self.part()
        if self._apply(DeletePartition(self.disk().path, start=p.start)):
            self.sel_part = None
            print(green("ManageDisks successfully deleted the selected partition."))

    def do_format(self, args):
        kv, flags = _kv(args)
        p = self.part()
        from .common import fs_choice
        fs = fs_choice(kv.get("fs", "ext4"))
        if self._apply(FormatPartition(self.disk().path, start=p.start, fstype=fs, label=kv.get("label", ""))):
            print(green("ManageDisks successfully formatted the volume."))

    def do_extend(self, args):
        kv, _ = _kv(args)
        d, p = self.disk(), self.part()
        lo, hi = d.neighbours(p)
        if "size" in kv:
            add = self._size(kv["size"], MiB, d) // d.sector_size
            new_size = p.size + add
        else:
            new_size = align_down(hi - p.start + 1, d.alignment) or (hi - p.start + 1)
        if new_size <= p.size:
            raise CliError("There is no free space after the partition to extend into.")
        if self._apply(ResizeMovePartition(d.path, start=p.start, new_start=p.start, new_size=new_size)):
            print(green("ManageDisks successfully extended the volume."))

    def do_shrink(self, args):
        kv, flags = _kv(args)
        d, p = self.disk(), self.part()
        if "querymax" in flags:
            fs = filesystems.lookup(p.fstype, p.fsversion)
            if fs is None or not fs.implemented("shrink"):
                print("The volume cannot be shrunk (filesystem does not support it).")
                return
            minimum = None
            if os.geteuid() == 0 and p.path:
                minimum = tasks.fs_min_size(p.path, p.fstype, p.fsversion,
                                            next(iter(p.all_mountpoints()), None))
            if minimum is None and p.fs_used is not None:
                minimum = p.fs_used
            if minimum is None:
                print("Cannot determine the minimum size (run as root).")
                return
            print(f"The maximum number of reclaimable bytes is: "
                  f"{format_size(max(0, p.size * d.sector_size - minimum))}")
            return
        if "desired" not in kv:
            raise CliError(HELP["shrink"])
        by = self._size(kv["desired"], MiB, d) // d.sector_size
        new_size = align_down(p.size - by, d.alignment)
        if new_size <= 0:
            raise CliError("The desired amount is larger than the partition.")
        if self._apply(ResizeMovePartition(d.path, start=p.start, new_start=p.start, new_size=new_size)):
            print(green("ManageDisks successfully shrunk the volume."))

    def do_clean(self, args):
        d = self.disk()
        method = "zero" if args and args[0].lower() == "all" else "quick"
        if self._apply(Wipe(d.path, method=method)):
            self.sel_part = None
            print(green("ManageDisks succeeded in cleaning the disk."))

    def do_convert(self, args):
        if not args or args[0].lower() not in ("gpt", "mbr"):
            raise CliError(HELP["convert"])
        d = self.disk()
        to = "gpt" if args[0].lower() == "gpt" else "dos"
        op = CreateTable(d.path, to) if not d.partitions else ConvertTable(d.path, to=to)
        if self._apply(op):
            print(green(f"ManageDisks successfully converted the selected disk to {args[0].upper()} format."))

    def do_assign(self, args):
        kv, _ = _kv(args)
        p = self.part()
        if self.dry_run:
            print(f"(dry run) would mount {p.path}")
            return
        if os.geteuid() != 0:
            raise CliError("Mounting needs root.")
        mnt = tasks.mount(p.path, kv.get("mount"), label=p.label)
        self.rescan()
        print(green(f"Mounted {p.path} at {mnt}."))

    def do_remove(self, args):
        p = self.part()
        if self.dry_run:
            print(f"(dry run) would unmount {p.path}")
            return
        if os.geteuid() != 0:
            raise CliError("Unmounting needs root.")
        tasks.unmount(p.path)
        self.rescan()
        print(green(f"Unmounted {p.path}."))

    def _props(self, **kw):
        d, p = self.disk(), self.part()
        return self._apply(SetPartitionProps(d.path, start=p.start, **kw))

    def do_active(self, args):
        if self._props(bootable=True):
            print(green("ManageDisks marked the current partition as active."))

    def do_inactive(self, args):
        if self._props(bootable=False):
            print(green("ManageDisks marked the current partition as inactive."))

    def do_set(self, args):
        kv, _ = _kv(args)
        if "id" not in kv:
            raise CliError(HELP["set"])
        if self._props(type_id=kv["id"]):
            print(green("ManageDisks successfully set the partition ID."))

    def do_gpt(self, args):
        kv, _ = _kv(args)
        if "attributes" not in kv:
            raise CliError(HELP["gpt"])
        try:
            value = int(kv["attributes"], 0)
        except ValueError:
            raise CliError("ATTRIBUTES must be a number such as 0x8000000000000000") from None
        names = {0: "RequiredPartition", 1: "NoBlockIOProtocol", 2: "LegacyBIOSBootable"}
        attrs = [names.get(b, f"GUID:{b}") for b in range(64) if value & (1 << b) and (b < 3 or b >= 48)]
        if self._props(attrs=attrs):
            print(green("ManageDisks successfully assigned the attributes to the selected GPT partition."))

    def do_label(self, args):
        p = self.part()
        if self._apply(SetLabel(self.disk().path, start=p.start, label=" ".join(args))):
            print(green("Label changed."))

    def do_uuid(self, args):
        p = self.part()
        if self._apply(NewUUID(self.disk().path, start=p.start)):
            print(green("New UUID assigned."))

    def do_check(self, args):
        p = self.part()
        repair = not (args and args[0].lower() == "scan")
        self._apply(CheckFilesystem(self.disk().path, start=p.start, repair=repair))


def main():  # pragma: no cover
    DiskShell().run()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
