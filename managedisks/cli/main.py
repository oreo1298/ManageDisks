"""``managedisks-cli``: scriptable disk management."""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading

from .. import __version__
from ..core import distro, executor, filesystems, parttypes, smart, tasks
from ..core.operations import (WIPE_METHODS, CheckFilesystem, ConvertTable, CopyPartition, CreatePartition,
                               CreateTable, DeletePartition, FormatPartition, NewUUID, ResizeMovePartition,
                               SetLabel, SetPartitionProps, Wipe)
from ..core.units import SizeError, align_down, format_size, parse_size
from .common import (CliError, Printer, ask_passphrase, bold, confirm, dim, find_disk_arg, find_part_arg,
                     fmt_part_fs, fs_choice, green, mib, pick_free, placement, plan_one, red, require_root,
                     run_ops, scan, table, type_choice, yellow)


# -- read-only commands ------------------------------------------------------
def print_disk(d, show_free=True) -> None:
    flags = []
    if d.removable:
        flags.append("removable")
    if d.read_only:
        flags.append("read-only")
    head = f"{bold(d.path)}  {d.title()}  {format_size(d.size)}  {parttypes.table_label(d.table)}"
    if d.transport:
        head += f"  {d.transport.upper()}"
    if flags:
        head += "  " + yellow(", ".join(flags))
    print(head)
    rows = []
    items = list(d.sorted_partitions()) + (d.free_spaces() if show_free else [])
    items.sort(key=lambda x: (x.start, 0 if getattr(x, "is_extended", False) else 1))
    for it in items:
        if hasattr(it, "number"):
            p = it
            used = ""
            if p.fs_used is not None and p.fs_size:
                used = f"{format_size(p.fs_used)} ({p.fs_used * 100 // p.fs_size}%)"
            name = ("  " if p.is_logical else "") + (p.path or "(new)")
            rows.append([str(p.number), name, mib(p.start, d), mib(p.size, d), fmt_part_fs(p),
                         p.label or p.name, used, ", ".join(p.all_mountpoints()) or
                         ("swap" if p.is_swap_active else "")])
        else:
            f = it
            rows.append(["", dim(("  " if f.inside_extended else "") + "unallocated"), dim(mib(f.start, d)),
                         dim(mib(f.size, d)), "", "", "", ""])
    if d.fstype and not d.partitions:
        rows.append(["", d.path, "0 B", format_size(d.size), filesystems.display_name(d.fstype),
                     d.label, "", ", ".join(d.mountpoints)])
    if rows:
        print(table(rows, ["#", "Device", "Start", "Size", "Filesystem", "Label/Name", "Used", "Mounted"]))
    elif not d.table:
        print(dim("  (empty disk, no partition table)"))
    print()


def cmd_list(a):
    disks = scan(a.all)
    if a.json:
        print(json.dumps([d.to_dict() for d in disks], indent=2))
        return 0
    if not disks:
        print("No disks found." + ("" if a.all else " (Use --all to include loop devices.)"))
    for d in disks:
        print_disk(d)
    if os.geteuid() != 0:
        print(dim("Tip: run as root to see exact geometry and usage of unmounted filesystems."))
    return 0


def cmd_info(a):
    disks = scan(True)
    from .common import find_device
    d, p = find_device(disks, a.device)
    if a.json:
        print(json.dumps((p or d).to_dict(), indent=2))
        return 0
    if p is None:
        rows = [["Path", d.path], ["Model", d.title()], ["Serial", d.serial or "—"],
                ["Size", f"{format_size(d.size)} ({d.size} bytes, {d.sectors} sectors)"],
                ["Sector size", f"{d.sector_size} logical / {d.physical_sector_size} physical"],
                ["Transport", d.transport or "—"], ["Type", "rotational" if d.rotational else "solid state"],
                ["Partition table", parttypes.table_label(d.table)], ["Table id", d.table_uuid or "—"],
                ["Usable sectors", f"{d.first_usable} – {d.last_usable}"
                 + ("" if d.geometry_exact else " (estimated)")],
                ["Unallocated", format_size(d.free_bytes())]]
        if d.backing_file:
            rows.append(["Backing file", d.backing_file])
        print(table(rows, indent=0))
        print()
        print_disk(d)
    else:
        rows = [["Path", p.path], ["Disk", d.path], ["Number", str(p.number)], ["Kind", p.kind],
                ["Start", f"{p.start} ({mib(p.start, d)})"], ["End", f"{p.end}"],
                ["Size", f"{mib(p.size, d)} ({p.size} sectors)"],
                ["Type", f"{parttypes.type_name(p.type_id, d.table)} ({p.type_id})"],
                ["Partition UUID", p.uuid or "—"], ["Name", p.name or "—"],
                ["Attributes", ", ".join(p.attrs) or ("bootable" if p.bootable else "—")],
                ["Filesystem", fmt_part_fs(p) or "—"], ["Version", p.fsversion or "—"],
                ["Label", p.label or "—"], ["UUID", p.fsuuid or "—"],
                ["Mounted at", ", ".join(p.all_mountpoints()) or "—"]]
        if p.fs_size:
            rows.append(["Usage", f"{format_size(p.fs_used)} of {format_size(p.fs_size)}"
                         if p.fs_used is not None else format_size(p.fs_size)])
        aligned = p.start % d.alignment == 0
        rows.append(["Aligned (1 MiB)", "yes" if aligned else yellow("no")])
        print(table(rows, indent=0))
    return 0


def cmd_support(a):
    rows = []
    for r in filesystems.support_matrix():
        cells = [r.fs.name]
        for cap in filesystems.CAPABILITIES:
            v = r.caps[cap]
            cells.append(dim("—") if v is None else (green("yes") if v else red("no")))
        cells.append(", ".join(r.packages))
        rows.append(cells)
    print(table(rows, ["Filesystem"] + [filesystems.CAPABILITY_NAMES[c] for c in filesystems.CAPABILITIES]
                + ["Missing package"]))
    pkgs = filesystems.missing_packages()
    print()
    print(f"Distribution: {distro.pretty_name()}")
    if pkgs:
        print("Install the missing tools with:\n  " + bold(distro.install_command(pkgs)))
    else:
        print(green("Every supported filesystem has all of its tools installed."))
    return 0


# -- operations ----------------------------------------------------------------
def _ops_disks(a):
    return scan(True)


def cmd_table(a):
    disks = _ops_disks(a)
    d = find_disk_arg(disks, a.disk)
    kind = {"gpt": "gpt", "mbr": "dos", "dos": "dos", "msdos": "dos"}[a.type.lower()]
    return 0 if run_ops(plan_one(disks, CreateTable(d.path, kind)), dry_run=a.dry_run, assume_yes=a.yes,
                        verbose=a.verbose, disks=disks) else 1


def cmd_create(a):
    disks = _ops_disks(a)
    d = find_disk_arg(disks, a.disk)
    kind = "extended" if a.extended else "logical" if a.logical else "normal"
    free = pick_free(d, a.start, kind == "logical")
    start, size = placement(d, free, a.start, a.size)
    fs = "" if kind == "extended" else fs_choice(a.fs)
    if fs == "cleared":
        fs = ""
    passphrase = ask_passphrase() if a.encrypt else ""
    op = CreatePartition(d.path, start=start, size=size, kind=kind, fstype=fs, label=a.label or "",
                         name=a.name or "", type_id=type_choice(a.type, d.table) if a.type and d.table else "",
                         encrypt=a.encrypt, passphrase=passphrase)
    return 0 if run_ops(plan_one(disks, op), dry_run=a.dry_run, assume_yes=a.yes, verbose=a.verbose,
                        disks=disks) else 1


def cmd_delete(a):
    disks = _ops_disks(a)
    d, p = find_part_arg(disks, a.partition)
    return 0 if run_ops(plan_one(disks, DeletePartition(d.path, start=p.start)), dry_run=a.dry_run,
                        assume_yes=a.yes, verbose=a.verbose, disks=disks) else 1


def cmd_format(a):
    disks = _ops_disks(a)
    d, p = find_part_arg(disks, a.partition)
    passphrase = ask_passphrase() if a.encrypt else ""
    op = FormatPartition(d.path, start=p.start, fstype=fs_choice(a.fs), label=a.label or "",
                         encrypt=a.encrypt, passphrase=passphrase)
    return 0 if run_ops(plan_one(disks, op), dry_run=a.dry_run, assume_yes=a.yes, verbose=a.verbose,
                        disks=disks) else 1


def cmd_resize(a):
    disks = _ops_disks(a)
    d, p = find_part_arg(disks, a.partition)
    lo, hi = d.neighbours(p)
    new_start = p.start
    if a.move_by:
        text = a.move_by
        sign = -1 if text.startswith("-") else 1
        try:
            delta = parse_size(text.lstrip("+-"), sector_size=d.sector_size) // d.sector_size
        except SizeError as e:
            raise CliError(str(e)) from None
        new_start = p.start + sign * delta
    elif a.start:
        try:
            new_start = parse_size(a.start, sector_size=d.sector_size) // d.sector_size
        except SizeError as e:
            raise CliError(str(e)) from None
    new_start = new_start - new_start % d.alignment if new_start % d.alignment else new_start
    size = p.size
    if a.size:
        text = a.size
        if text == "max":
            size = hi - new_start + 1
            size = align_down(size, d.alignment) or size
        else:
            rel = text[0] if text[0] in "+-" else ""
            try:
                b = parse_size(text.lstrip("+-"), sector_size=d.sector_size)
            except SizeError as e:
                raise CliError(str(e)) from None
            sectors = b // d.sector_size
            size = p.size + sectors if rel == "+" else p.size - sectors if rel == "-" else sectors
            if not rel:
                size = align_down(size, d.alignment) or size
    op = ResizeMovePartition(d.path, start=p.start, new_start=new_start, new_size=size)
    return 0 if run_ops(plan_one(disks, op), dry_run=a.dry_run, assume_yes=a.yes, verbose=a.verbose,
                        disks=disks) else 1


def cmd_label(a):
    disks = _ops_disks(a)
    d, p = find_part_arg(disks, a.partition)
    return 0 if run_ops(plan_one(disks, SetLabel(d.path, start=p.start, label=a.label)), dry_run=a.dry_run,
                        assume_yes=True, verbose=a.verbose, disks=disks) else 1


def cmd_set(a):
    disks = _ops_disks(a)
    d, p = find_part_arg(disks, a.partition)
    attrs = None
    if a.attr:
        attrs = list(p.attrs)
        for spec in a.attr:
            name, _, state = spec.partition("=")
            if state.lower() in ("off", "0", "no", "false"):
                attrs = [x for x in attrs if x != name]
            elif name not in attrs:
                attrs.append(name)
    boot = None if a.boot is None else a.boot == "on"
    op = SetPartitionProps(d.path, start=p.start,
                           type_id=type_choice(a.type, d.table) if a.type else None,
                           name=a.name, attrs=attrs, bootable=boot)
    if op.type_id is None and op.name is None and op.attrs is None and op.bootable is None:
        raise CliError("Nothing to change: use --type, --name, --attr or --boot")
    return 0 if run_ops(plan_one(disks, op), dry_run=a.dry_run, assume_yes=True, verbose=a.verbose,
                        disks=disks) else 1


def cmd_uuid(a):
    disks = _ops_disks(a)
    d, p = find_part_arg(disks, a.partition)
    return 0 if run_ops(plan_one(disks, NewUUID(d.path, start=p.start)), dry_run=a.dry_run,
                        assume_yes=a.yes, verbose=a.verbose, disks=disks) else 1


def cmd_check(a):
    disks = _ops_disks(a)
    d, p = find_part_arg(disks, a.partition)
    return 0 if run_ops(plan_one(disks, CheckFilesystem(d.path, start=p.start, repair=not a.no_repair)),
                        dry_run=a.dry_run, assume_yes=True, verbose=True, disks=disks) else 1


def cmd_copy(a):
    disks = _ops_disks(a)
    sd, sp = find_part_arg(disks, a.partition)
    dd = find_disk_arg(disks, a.disk)
    free = pick_free(dd, a.start, False)
    start, avail = placement(dd, free, a.start, None)
    size = sp.size if not a.size else placement(dd, free, a.start, a.size)[1]
    if size > avail:
        raise CliError("Not enough unallocated space at the destination")
    op = CopyPartition(sd.path, start=sp.start, dest_disk=dd.path, dest_start=start, dest_size=size,
                       new_uuid=not a.keep_uuid)
    return 0 if run_ops(plan_one(disks, op), dry_run=a.dry_run, assume_yes=a.yes, verbose=a.verbose,
                        disks=disks) else 1


def cmd_wipe(a):
    disks = _ops_disks(a)
    from .common import find_device
    d, p = find_device(disks, a.device)
    op = Wipe(d.path, start=p.start if p else None, method=a.method)
    return 0 if run_ops(plan_one(disks, op), dry_run=a.dry_run, assume_yes=a.yes, verbose=a.verbose,
                        disks=disks) else 1


def cmd_convert(a):
    disks = _ops_disks(a)
    d = find_disk_arg(disks, a.disk)
    to = {"gpt": "gpt", "mbr": "dos", "dos": "dos"}[a.to.lower()]
    return 0 if run_ops(plan_one(disks, ConvertTable(d.path, to=to)), dry_run=a.dry_run, assume_yes=a.yes,
                        verbose=a.verbose, disks=disks) else 1


# -- immediate actions -----------------------------------------------------------
def cmd_mount(a):
    require_root()
    print(tasks.mount(a.device, a.mountpoint, a.options))
    return 0


def cmd_unmount(a):
    require_root()
    tasks.unmount(a.device)
    return 0


def cmd_open(a):
    require_root()
    print(tasks.luks_open(a.device, ask_passphrase()))
    return 0


def cmd_close(a):
    require_root()
    tasks.luks_close(a.device)
    return 0


def cmd_smart(a):
    require_root()
    if a.test:
        print(smart.start_self_test(a.disk, a.test))
        return 0
    data = smart.read_raw(a.disk)
    if a.json:
        print(json.dumps(data, indent=2))
        return 0
    r = smart.parse(data)
    if not r.available:
        print(yellow(r.message))
        return 1
    verdict = {"good": green("GOOD"), "warning": yellow("WARNING"), "bad": red("FAILING"),
               "unknown": dim("UNKNOWN")}[r.verdict]
    rows = [["Overall health", verdict], ["Model", r.model], ["Serial", r.serial], ["Firmware", r.firmware]]
    if r.temperature is not None:
        rows.append(["Temperature", f"{r.temperature} °C"])
    if r.power_on_hours is not None:
        rows.append(["Powered on", f"{r.power_on_hours} hours"])
    if r.power_cycles is not None:
        rows.append(["Power cycles", str(r.power_cycles)])
    if r.percentage_used is not None:
        rows.append(["Wear (percentage used)", f"{r.percentage_used}%"])
    if r.available_spare is not None:
        rows.append(["Available spare", f"{r.available_spare}%"])
    if r.data_written is not None:
        rows.append(["Data written", format_size(r.data_written)])
    if r.media_errors is not None:
        rows.append(["Media errors", str(r.media_errors)])
    print(table(rows, indent=0))
    if r.attributes:
        print()
        rows = []
        for at in r.attributes:
            flag = red("FAILING") if at.failing else yellow("check") if at.warning else ""
            rows.append([str(at.id), at.name, str(at.value), str(at.worst), str(at.threshold), at.raw, flag])
        print(table(rows, ["ID", "Attribute", "Value", "Worst", "Thresh", "Raw", ""], indent=0))
    return 0


def _task(fn, *args):
    require_root()
    cancel = threading.Event()
    ctx = executor.ExecContext(Printer(verbose=True), cancel)
    try:
        return fn(ctx, *args)
    except KeyboardInterrupt:
        cancel.set()
        print(red("\nInterrupted"))
        return None


def cmd_surface(a):
    bad = _task(tasks.surface_test, a.device)
    if bad is None:
        return 1
    if bad:
        print(red(f"{len(bad)} unreadable region(s):"))
        for off, n in bad:
            print(f"  byte {off} (+{n})")
        return 2
    print(green("No read errors found."))
    return 0


def cmd_image(a):
    if a.action == "create":
        _task(tasks.create_image, a.device, a.file, a.gzip)
    else:
        if not confirm(red(f"Overwrite everything on {a.device} with {a.file}?"), a.yes):
            return 1
        _task(tasks.restore_image, a.file, a.device)
    return 0


def cmd_clone(a):
    if not confirm(red(f"Overwrite everything on {a.target} with a copy of {a.source}?"), a.yes):
        return 1
    _task(tasks.clone_disk, a.source, a.target, not a.keep_ids)
    return 0


def cmd_shell(a):
    from .shell import DiskShell
    DiskShell(include_loop=a.all, dry_run=a.dry_run, assume_yes=a.yes).run()
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="managedisks-cli",
        description="Manage disks, partitions and filesystems from the command line. "
                    "Sizes accept K/M/G/T (binary), KB/MB/GB (decimal), sectors (2048s) and percentages; "
                    "a bare number means MiB.",
        epilog="Run 'managedisks-cli shell' for an interactive, diskpart-style console.")
    p.add_argument("--version", action="version", version=f"ManageDisks {__version__}")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-y", "--yes", action="store_true", help="do not ask for confirmation")
    common.add_argument("-n", "--dry-run", action="store_true", help="only show what would be done")
    common.add_argument("-v", "--verbose", action="store_true", help="show the output of every tool")
    common.add_argument("-a", "--all", action="store_true", help="include loop devices")
    common.add_argument("--json", action="store_true", help="machine readable output (where supported)")
    sub = p.add_subparsers(dest="command", metavar="COMMAND")

    def add(name, fn, help_, **kw):
        sp = sub.add_parser(name, parents=[common], help=help_, description=help_, **kw)
        sp.set_defaults(fn=fn)
        return sp

    add("list", cmd_list, "list disks, partitions and unallocated space")
    sp = add("info", cmd_info, "show details of a disk or partition")
    sp.add_argument("device")
    add("support", cmd_support, "show which filesystems are supported and what is missing")

    sp = add("table", cmd_table, "create a new empty partition table (erases the disk)")
    sp.add_argument("disk")
    sp.add_argument("type", choices=["gpt", "mbr", "dos", "msdos"])

    sp = add("create", cmd_create, "create a partition in unallocated space")
    sp.add_argument("disk")
    sp.add_argument("-s", "--size", help="size (e.g. 20G, 50%%, max; default: the whole free region)")
    sp.add_argument("--start", help="position from the start of the disk (default: largest free region)")
    sp.add_argument("-f", "--fs", default="ext4", help="filesystem (ext4, btrfs, xfs, f2fs, fat32, exfat, "
                                                        "ntfs, hfsplus, apfs, linux-swap, none, ...)")
    sp.add_argument("-L", "--label", help="filesystem label")
    sp.add_argument("--name", help="GPT partition name")
    sp.add_argument("-t", "--type", help="partition type (linux, esp, swap, msdata, bios_grub, lvm, raid, "
                                         "home, a GUID or an MBR code)")
    sp.add_argument("--encrypt", action="store_true", help="encrypt with LUKS2 (asks for a passphrase)")
    g = sp.add_mutually_exclusive_group()
    g.add_argument("--logical", action="store_true", help="MBR: logical partition inside the extended one")
    g.add_argument("--extended", action="store_true", help="MBR: create an extended partition")

    sp = add("delete", cmd_delete, "delete a partition")
    sp.add_argument("partition")

    sp = add("format", cmd_format, "format a partition")
    sp.add_argument("partition")
    sp.add_argument("-f", "--fs", required=True)
    sp.add_argument("-L", "--label")
    sp.add_argument("--encrypt", action="store_true")

    sp = add("resize", cmd_resize, "resize and/or move a partition (with its filesystem)")
    sp.add_argument("partition")
    sp.add_argument("-s", "--size", help="new size: 30G, +5G, max, or --size=-5G to shrink by 5 GiB")
    g = sp.add_mutually_exclusive_group()
    g.add_argument("--start", help="new start position")
    g.add_argument("--move-by", help="move by an offset, e.g. --move-by=+1G or --move-by=-500M")

    sp = add("label", cmd_label, "set (or clear with '') the filesystem label")
    sp.add_argument("partition")
    sp.add_argument("label")

    sp = add("set", cmd_set, "change partition type, GPT name, GPT attributes or MBR boot flag")
    sp.add_argument("partition")
    sp.add_argument("-t", "--type")
    sp.add_argument("--name")
    sp.add_argument("--attr", action="append", metavar="NAME=on|off",
                    help="GPT attribute (RequiredPartition, NoBlockIOProtocol, LegacyBIOSBootable, GUID:63, ...)")
    sp.add_argument("--boot", choices=["on", "off"], help="MBR active/boot flag")

    sp = add("uuid", cmd_uuid, "give a filesystem a new random UUID")
    sp.add_argument("partition")

    sp = add("check", cmd_check, "check and repair a filesystem")
    sp.add_argument("partition")
    sp.add_argument("--no-repair", action="store_true", help="only report problems")

    sp = add("copy", cmd_copy, "copy a partition into unallocated space (same or other disk)")
    sp.add_argument("partition")
    sp.add_argument("disk")
    sp.add_argument("--start")
    sp.add_argument("-s", "--size", help="size of the copy (default: same as the source)")
    sp.add_argument("--keep-uuid", action="store_true", help="keep the filesystem UUID (duplicates!)")

    sp = add("wipe", cmd_wipe, "erase a disk or partition")
    sp.add_argument("device")
    sp.add_argument("-m", "--method", choices=list(WIPE_METHODS), default="quick")

    sp = add("convert", cmd_convert, "convert between MBR and GPT without losing data")
    sp.add_argument("disk")
    sp.add_argument("to", choices=["gpt", "mbr", "dos"])

    sp = add("mount", cmd_mount, "mount a filesystem (default: /mnt/<label>)")
    sp.add_argument("device")
    sp.add_argument("mountpoint", nargs="?")
    sp.add_argument("-o", "--options")
    sp = add("unmount", cmd_unmount, "unmount a filesystem")
    sp.add_argument("device")
    sp = add("open", cmd_open, "unlock a LUKS encrypted partition")
    sp.add_argument("device")
    sp = add("close", cmd_close, "lock a LUKS encrypted partition")
    sp.add_argument("device")

    sp = add("smart", cmd_smart, "show S.M.A.R.T. health or start a self-test")
    sp.add_argument("disk")
    sp.add_argument("--test", choices=["short", "long", "conveyance"])

    sp = add("surface", cmd_surface, "read-only surface test (finds unreadable sectors)")
    sp.add_argument("device")

    sp = add("image", cmd_image, "create a disk image or write one (ISO, .img, .img.xz/.gz) to a device")
    isub = sp.add_subparsers(dest="action", required=True)
    ic = isub.add_parser("create", parents=[common])
    ic.add_argument("device")
    ic.add_argument("file")
    ic.add_argument("--gzip", action="store_true")
    ir = isub.add_parser("restore", parents=[common])
    ir.add_argument("file")
    ir.add_argument("device")

    sp = add("clone", cmd_clone, "clone a whole disk to another disk")
    sp.add_argument("source")
    sp.add_argument("target")
    sp.add_argument("--keep-ids", action="store_true", help="keep disk/partition GUIDs")

    add("shell", cmd_shell, "interactive diskpart-style console")
    return p


def main(argv: list[str] | None = None) -> int:
    import signal
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)  # "managedisks-cli list | head" must not traceback
    parser = build_parser()
    a = parser.parse_args(argv)
    if not getattr(a, "fn", None):
        parser.print_help()
        return 0
    try:
        return a.fn(a)
    except CliError as e:
        print(red(f"error: {e}"), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    except (OSError, RuntimeError, ValueError) as e:
        print(red(f"error: {e}"), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
