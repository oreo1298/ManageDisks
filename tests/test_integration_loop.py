"""End-to-end tests against real loop devices (run as root)."""

import os
import shutil
import subprocess

import pytest

from conftest import has_capability, kernel_has_fs

from managedisks.core import executor, scanner
from managedisks.core.model import find_disk
from managedisks.core.operations import (CheckFilesystem, ConvertTable, CopyPartition, CreatePartition,
                                         CreateTable, DeletePartition, FormatPartition, NewUUID, OpError,
                                         Plan, ResizeMovePartition, SetLabel, SetPartitionProps, Wipe)
from managedisks.core.units import MiB


def _disk(dev):
    return find_disk(scanner.scan(include_loop=True), dev)


def _run(ops, dev):
    events = []
    ok = executor.execute(ops, events.append)
    errors = [e for e in events if e["type"] in ("op_failed", "done") and e.get("error")]
    log = "\n".join(e["line"] for e in events if e["type"] == "log")
    assert ok, f"{errors}\n{log}"
    return events


def _plan(dev, *ops):
    disks = scanner.scan(include_loop=True)
    plan = Plan()
    for op in ops:
        plan.add(disks, op)
    return plan.ops


def test_create_table_and_partitions(loopdisk):
    d = _disk(loopdisk)
    a = d.alignment
    ops = _plan(loopdisk,
                CreateTable(loopdisk, "gpt"),
                CreatePartition(loopdisk, start=a, size=200 * MiB // 512, fstype="ext4", label="root", name="Linux"),
                CreatePartition(loopdisk, start=a + 200 * MiB // 512, size=100 * MiB // 512, fstype="fat32", label="efi"),
                CreatePartition(loopdisk, start=a + 300 * MiB // 512, size=100 * MiB // 512, fstype="ntfs", label="win"),
                CreatePartition(loopdisk, start=a + 400 * MiB // 512, size=64 * MiB // 512, fstype="linux-swap"))
    _run(ops, loopdisk)
    d = _disk(loopdisk)
    assert d.table == "gpt"
    got = [(p.number, p.fstype, p.label) for p in d.sorted_partitions()]
    assert got == [(1, "ext4", "root"), (2, "vfat", "EFI"), (3, "ntfs", "win"), (4, "swap", "")]
    assert d.by_number(1).name == "Linux"
    assert d.by_number(3).type_id == "EBD0A0A2-B9E5-4433-87C0-68B6B72699C7"


def _write_marker(path, tmp="/run/md-test-mnt"):
    os.makedirs(tmp, exist_ok=True)
    subprocess.check_call(["mount", path, tmp])
    try:
        with open(os.path.join(tmp, "marker.bin"), "wb") as f:
            f.write(b"managedisks" * 100000)
    finally:
        subprocess.check_call(["umount", tmp])


def _read_marker(path, tmp="/run/md-test-mnt"):
    os.makedirs(tmp, exist_ok=True)
    subprocess.check_call(["mount", "-o", "ro", path, tmp])
    try:
        with open(os.path.join(tmp, "marker.bin"), "rb") as f:
            return f.read() == b"managedisks" * 100000
    finally:
        subprocess.check_call(["umount", tmp])


@pytest.mark.parametrize("fstype", ["ext4", "ntfs", "btrfs"])
def test_shrink_move_grow_keeps_data(loopdisk, fstype):
    if fstype == "ntfs" and not shutil.which("ntfsresize"):
        pytest.skip("ntfs-3g missing")
    if fstype == "btrfs" and not kernel_has_fs("btrfs"):
        pytest.skip("kernel without btrfs")
    d = _disk(loopdisk)
    a = d.alignment
    size = 400 * MiB // 512
    _run(_plan(loopdisk, CreateTable(loopdisk, "gpt"),
               CreatePartition(loopdisk, start=a, size=size, fstype=fstype, label="data")), loopdisk)
    path = _disk(loopdisk).by_number(1).path
    _write_marker(path)
    # shrink by half
    _run(_plan(loopdisk, ResizeMovePartition(loopdisk, start=a, new_start=a, new_size=size // 2)), loopdisk)
    assert _disk(loopdisk).by_number(1).size == size // 2
    assert _read_marker(path)
    # move right by 100 MiB
    new_start = a + 100 * MiB // 512
    _run(_plan(loopdisk, ResizeMovePartition(loopdisk, start=a, new_start=new_start, new_size=size // 2)), loopdisk)
    p = _disk(loopdisk).by_number(1)
    assert p.start == new_start
    assert _read_marker(p.path)
    # grow and move left at the same time
    _run(_plan(loopdisk, ResizeMovePartition(loopdisk, start=new_start, new_start=a, new_size=size)), loopdisk)
    p = _disk(loopdisk).by_number(1)
    assert (p.start, p.size) == (a, size)
    assert _read_marker(p.path)
    info = subprocess.check_output(["blkid", "-p", "-o", "value", "-s", "TYPE", p.path], text=True).strip()
    assert info == fstype


def test_online_grow_ext4(loopdisk, tmp_path):
    if not has_capability(24):  # CAP_SYS_RESOURCE, needed by online resize2fs
        pytest.skip("no CAP_SYS_RESOURCE in this environment")
    d = _disk(loopdisk)
    a = d.alignment
    _run(_plan(loopdisk, CreateTable(loopdisk, "gpt"),
               CreatePartition(loopdisk, start=a, size=100 * MiB // 512, fstype="ext4")), loopdisk)
    p = _disk(loopdisk).by_number(1)
    mnt = tmp_path / "mnt"
    mnt.mkdir()
    subprocess.check_call(["mount", p.path, str(mnt)])
    try:
        # A mounted partition may grow but not move/shrink.
        with pytest.raises(OpError):
            _plan(loopdisk, ResizeMovePartition(loopdisk, start=a, new_start=a, new_size=50 * MiB // 512))
        disks = scanner.scan(include_loop=True)
        assert find_disk(disks, loopdisk).by_number(1).is_mounted
        _run(_plan(loopdisk, ResizeMovePartition(loopdisk, start=a, new_start=a, new_size=300 * MiB // 512)),
             loopdisk)
        st = os.statvfs(str(mnt))
        assert st.f_blocks * st.f_frsize > 250 * MiB
    finally:
        subprocess.run(["umount", str(mnt)])


def test_mbr_logical_partitions_and_delete(loopdisk):
    d = _disk(loopdisk)
    a = d.alignment
    mb = MiB // 512
    _run(_plan(loopdisk, CreateTable(loopdisk, "dos"),
               CreatePartition(loopdisk, start=a, size=100 * mb, fstype="ext4", kind="primary"),
               CreatePartition(loopdisk, start=a + 100 * mb, size=600 * mb, kind="extended", fstype="")),
         loopdisk)
    ext = _disk(loopdisk).extended()
    assert ext is not None and ext.size == 600 * mb
    s1 = ext.start + a
    _run(_plan(loopdisk,
               CreatePartition(loopdisk, start=s1, size=100 * mb, fstype="ext4", kind="logical", label="L5"),
               CreatePartition(loopdisk, start=s1 + 101 * mb, size=100 * mb, fstype="fat32", kind="logical")),
         loopdisk)
    d = _disk(loopdisk)
    assert [(p.number, p.kind) for p in d.sorted_partitions()] == [
        (1, "primary"), (2, "extended"), (5, "logical"), (6, "logical")]
    _run(_plan(loopdisk, DeletePartition(loopdisk, start=s1)), loopdisk)
    d = _disk(loopdisk)
    assert [p.number for p in d.logicals()] == [5]
    assert d.logicals()[0].fstype == "vfat"
    with pytest.raises(OpError):
        _plan(loopdisk, DeletePartition(loopdisk, start=ext.start))  # still holds a logical


def test_convert_gpt_mbr_roundtrip(loopdisk):
    d = _disk(loopdisk)
    a = d.alignment
    mb = MiB // 512
    _run(_plan(loopdisk, CreateTable(loopdisk, "dos"),
               CreatePartition(loopdisk, start=a, size=100 * mb, fstype="ext4", label="one"),
               CreatePartition(loopdisk, start=a + 100 * mb, size=100 * mb, fstype="fat32", label="two")),
         loopdisk)
    _write_marker(_disk(loopdisk).by_number(1).path)
    _run(_plan(loopdisk, ConvertTable(loopdisk, "gpt")), loopdisk)
    d = _disk(loopdisk)
    assert d.table == "gpt"
    assert [p.fstype for p in d.sorted_partitions()] == ["ext4", "vfat"]
    assert d.by_number(2).type_id == "EBD0A0A2-B9E5-4433-87C0-68B6B72699C7"
    assert _read_marker(d.by_number(1).path)
    _run(_plan(loopdisk, ConvertTable(loopdisk, "dos")), loopdisk)
    d = _disk(loopdisk)
    assert d.table == "dos"
    assert _read_marker(d.by_number(1).path)


def test_label_uuid_props_check_format(loopdisk):
    d = _disk(loopdisk)
    a = d.alignment
    mb = MiB // 512
    _run(_plan(loopdisk, CreateTable(loopdisk, "gpt"),
               CreatePartition(loopdisk, start=a, size=100 * mb, fstype="ext4")), loopdisk)
    old_uuid = _disk(loopdisk).by_number(1).fsuuid
    _run(_plan(loopdisk, SetLabel(loopdisk, start=a, label="hello"),
               NewUUID(loopdisk, start=a),
               SetPartitionProps(loopdisk, start=a, type_id="home", name="Home", attrs=["LegacyBIOSBootable"]),
               CheckFilesystem(loopdisk, start=a)), loopdisk)
    p = _disk(loopdisk).by_number(1)
    assert p.label == "hello" and p.fsuuid != old_uuid
    assert p.name == "Home" and p.type_id == "933AC7E1-2EB4-4F13-B844-0E14E2AEF915"
    assert "LegacyBIOSBootable" in p.attrs
    _run(_plan(loopdisk, FormatPartition(loopdisk, start=a, fstype="exfat", label="stick")), loopdisk)
    p = _disk(loopdisk).by_number(1)
    assert (p.fstype, p.label) == ("exfat", "stick")


def test_copy_partition_and_wipe(loopdisk):
    d = _disk(loopdisk)
    a = d.alignment
    mb = MiB // 512
    _run(_plan(loopdisk, CreateTable(loopdisk, "gpt"),
               CreatePartition(loopdisk, start=a, size=100 * mb, fstype="ext4", label="src")), loopdisk)
    _write_marker(_disk(loopdisk).by_number(1).path)
    _run(_plan(loopdisk, CopyPartition(loopdisk, start=a, dest_disk=loopdisk, dest_start=a + 200 * mb,
                                       dest_size=150 * mb)), loopdisk)
    d = _disk(loopdisk)
    src, dst = d.by_number(1), d.by_number(2)
    assert dst.label == "src" and dst.fsuuid != src.fsuuid
    assert _read_marker(dst.path)
    _run(_plan(loopdisk, Wipe(loopdisk, start=dst.start, method="zero")), loopdisk)
    assert _disk(loopdisk).by_number(2).fstype == ""
    _run(_plan(loopdisk, Wipe(loopdisk, method="quick")), loopdisk)
    d = _disk(loopdisk)
    assert d.table is None and not d.partitions


def test_luks_partition(loopdisk):
    if not shutil.which("cryptsetup") or "device-mapper" not in open("/proc/devices").read():
        pytest.skip("cryptsetup or device-mapper missing")
    d = _disk(loopdisk)
    a = d.alignment
    _run(_plan(loopdisk, CreateTable(loopdisk, "gpt"),
               CreatePartition(loopdisk, start=a, size=100 * MiB // 512, fstype="ext4", label="secret",
                               encrypt=True, passphrase="correct horse battery")), loopdisk)
    p = _disk(loopdisk).by_number(1)
    assert p.fstype == "crypto_LUKS"
    subprocess.run(["cryptsetup", "open", "--key-file=-", p.path, "mdtest"],
                   input=b"correct horse battery", check=True)
    try:
        t = subprocess.check_output(["blkid", "-p", "-o", "value", "-s", "TYPE", "/dev/mapper/mdtest"], text=True)
        assert t.strip() == "ext4"
    finally:
        subprocess.run(["cryptsetup", "close", "mdtest"])


def test_dry_run_runs_nothing(loopdisk):
    d = _disk(loopdisk)
    a = d.alignment
    ops = _plan(loopdisk, CreateTable(loopdisk, "gpt"),
                CreatePartition(loopdisk, start=a, size=100 * MiB // 512, fstype="ext4"))
    events = []
    assert executor.execute(ops, events.append, dry_run=True)
    cmds = [e["line"] for e in events if e["type"] == "log" and e["line"].startswith("$ ")]
    assert any("mkfs.ext4" in c for c in cmds)
    assert _disk(loopdisk).table is None


def _blkid(path, tag):
    return subprocess.run(["blkid", "-p", "-o", "value", "-s", tag, path], capture_output=True,
                          text=True).stdout.strip()


def test_mac_filesystems(loopdisk):
    if not shutil.which("mkfs.hfsplus"):
        pytest.skip("hfsprogs missing")
    d = _disk(loopdisk)
    a = d.alignment
    mb = MiB // 512
    ops = [CreateTable(loopdisk, "gpt"),
           CreatePartition(loopdisk, start=a, size=200 * mb, fstype="hfsplus", label="Mac Data")]
    if shutil.which("mkapfs"):
        ops.append(CreatePartition(loopdisk, start=a + 600 * mb, size=200 * mb, fstype="apfs", label="MacAPFS"))
    _run(_plan(loopdisk, *ops), loopdisk)
    d = _disk(loopdisk)
    hfs = d.by_number(1)
    assert (hfs.fstype, hfs.label, hfs.type_id) == ("hfsplus", "Mac Data", "48465300-0000-11AA-AA11-00306543ECAC")
    if shutil.which("mkapfs"):
        apfs = d.by_number(2)
        assert (apfs.fstype, apfs.type_id) == ("apfs", "7C3457EF-0000-11AA-AA11-00306543ECAC")
        _run(_plan(loopdisk, CheckFilesystem(loopdisk, start=apfs.start)), loopdisk)
    # Move the HFS+ volume right, into the free space; it must stay intact.
    new_start = a + 300 * mb
    _run(_plan(loopdisk, CheckFilesystem(loopdisk, start=a),
               ResizeMovePartition(loopdisk, start=a, new_start=new_start, new_size=hfs.size)), loopdisk)
    moved = _disk(loopdisk).find(new_start)
    assert moved is not None and _blkid(moved.path, "LABEL") == "Mac Data"
    subprocess.run(["fsck.hfsplus", "-f", "-n", moved.path], check=True, capture_output=True)
    # Reformat it for Linux: the partition type follows the filesystem.
    _run(_plan(loopdisk, FormatPartition(loopdisk, start=new_start, fstype="ext4")), loopdisk)
    assert _disk(loopdisk).find(new_start).type_id == "0FC63DAF-8483-4772-8E79-3D69D8477DE4"
