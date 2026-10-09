"""Unit tests for the engine (no root, no real disks)."""

import pytest

from helpers import ESP, LINUX, gpt_disk, mbr_disk
from managedisks.core import distro, filesystems, parttypes
from managedisks.core.model import Disk, Partition
from managedisks.core.operations import (ConvertTable, CopyPartition, CreatePartition, CreateTable,
                                         DeletePartition, FormatPartition, Operation, OpError, Plan,
                                         ResizeMovePartition, SetLabel, SetPartitionProps, Wipe, redact,
                                         simulate)
from managedisks.core.units import GiB, KiB, MiB, SizeError, align_down, align_up, format_size, parse_size

S = MiB // 512  # sectors per MiB


# -- units -------------------------------------------------------------------------
@pytest.mark.parametrize("n,text", [(0, "0 B"), (1023, "1023 B"), (1536, "1.5 KiB"), (GiB, "1 GiB"),
                                    (int(2.25 * GiB), "2.2 GiB"), (None, "—")])
def test_format_size(n, text):
    assert format_size(n) == text


@pytest.mark.parametrize("text,expected", [("10G", 10 * GiB), ("10GiB", 10 * GiB), ("10GB", 10 * 10 ** 9),
                                           ("512", 512 * MiB), ("2048s", 2048 * 512), ("1.5M", int(1.5 * MiB)),
                                           ("4k", 4 * KiB), ("50%", 50)])
def test_parse_size(text, expected):
    assert parse_size(text, total=100) == expected


@pytest.mark.parametrize("bad", ["", "abc", "10Q", "-5G", "150%"])
def test_parse_size_rejects(bad):
    with pytest.raises(SizeError):
        parse_size(bad, total=100)


def test_alignment():
    assert align_up(1, 2048) == 2048
    assert align_up(2048, 2048) == 2048
    assert align_down(4095, 2048) == 2048


# -- partition types ------------------------------------------------------------
def test_parttype_resolution():
    assert parttypes.resolve("esp", "gpt") == ESP
    assert parttypes.resolve("esp", "dos") == "ef"
    assert parttypes.resolve("0x83", "dos") == "83"
    assert parttypes.resolve("c12a7328-f81f-11d2-ba4b-00a0c93ec93b", "gpt") == ESP
    with pytest.raises(ValueError):
        parttypes.resolve("bios_grub", "dos")
    with pytest.raises(ValueError):
        parttypes.resolve("zz", "dos")
    assert parttypes.mbr_to_gpt("07") == parttypes.by_key("msdata").gpt
    assert parttypes.gpt_to_mbr(ESP) == "ef"
    assert parttypes.type_name("83", "dos") == "Linux"
    assert parttypes.default_type_for_fs("fat32", "dos") == "0c"
    assert parttypes.default_type_for_fs("linux-swap", "gpt") == parttypes.by_key("swap").gpt


# -- model ---------------------------------------------------------------------------
def test_free_space_and_segments():
    d = gpt_disk(parts=[(1, 512, "vfat"), (1024, 1024, "ext4")])
    frees = d.free_spaces()
    assert [(f.start // S, f.size // S) for f in frees][:1] == [(513, 511)]
    assert frees[-1].start == 2048 * S
    kinds = [type(x).__name__ for x in d.segments()]
    assert kinds == ["Partition", "FreeSpace", "Partition", "FreeSpace"]
    start, size = d.usable_extent(frees[0])
    assert start % d.alignment == 0 and size % d.alignment == 0


def test_neighbours():
    d = gpt_disk(parts=[(1, 512, "vfat"), (1024, 1024, "ext4"), (4096, 100, "")])
    lo, hi = d.neighbours(d.by_number(2))
    assert lo == 513 * S and hi == 4096 * S - 1


def test_tiny_gaps_are_not_free_space():
    d = gpt_disk(parts=[(1, 100, ""), (101, 100, "")])
    d.partitions[1].start += 10  # 10 sector gap
    assert all(f.size >= S for f in d.free_spaces())
    assert d.free_spaces()[0].start > d.partitions[1].end


def test_disk_roundtrip_dict():
    d = gpt_disk(parts=[(1, 512, "vfat")])
    assert Disk.from_dict(d.to_dict()) == d


def test_media_kind_and_title():
    d = Disk(path="/dev/nvme0n1", name="nvme0n1", size=GiB, transport="nvme", vendor="0x1af4", model="X")
    assert d.media_kind() == "nvme" and d.title() == "X"


# -- operations ------------------------------------------------------------------
def test_create_partition_preview_and_number():
    d = gpt_disk(parts=[(1, 512, "vfat")])
    op = CreatePartition(d.path, start=1024 * S, size=1024 * S, fstype="ext4", label="data")
    [p] = [x for x in simulate([d], [op])[0].partitions if x.is_new]
    assert p.number == 2 and p.fstype == "ext4" and p.label == "data" and p.type_id == LINUX


def test_create_partition_rejects_overlap_and_bounds():
    d = gpt_disk(parts=[(1, 512, "vfat")])
    with pytest.raises(OpError):
        simulate([d], [CreatePartition(d.path, start=100 * S, size=10 * S, fstype="ext4")])
    with pytest.raises(OpError):
        simulate([d], [CreatePartition(d.path, start=d.last_usable - 10, size=100 * S, fstype="ext4")])


def test_create_rejects_bad_label_and_tiny_fs():
    d = gpt_disk()
    with pytest.raises(OpError):
        simulate([d], [CreatePartition(d.path, start=S, size=100 * S, fstype="fat32", label="WAY TOO LONG LABEL")])
    with pytest.raises(OpError):
        simulate([d], [CreatePartition(d.path, start=S, size=100 * S, fstype="xfs")])  # XFS needs 300 MiB


def test_fat_label_is_uppercased():
    d = gpt_disk()
    p = simulate([d], [CreatePartition(d.path, start=S, size=100 * S, fstype="fat32", label="efi")])[0].partitions[0]
    assert p.label == "EFI"


def test_mbr_rules():
    d = mbr_disk()
    ops = [CreatePartition(d.path, start=S * (1 + i * 100), size=100 * S, fstype="ext4") for i in range(4)]
    preview = simulate([d], ops)[0]
    assert [p.number for p in preview.partitions] == [1, 2, 3, 4]
    assert all(p.kind == "primary" for p in preview.partitions)
    with pytest.raises(OpError):  # fifth primary
        simulate([d], ops + [CreatePartition(d.path, start=S * 500, size=100 * S, fstype="ext4")])


def test_mbr_extended_and_logical():
    d = mbr_disk()
    plan = Plan()
    plan.add([d], CreatePartition(d.path, start=S, size=100 * S, fstype="ext4"))
    plan.add([d], CreatePartition(d.path, start=101 * S, size=1000 * S, kind="extended", fstype=""))
    preview = plan.add([d], CreatePartition(d.path, start=102 * S, size=100 * S, kind="logical", fstype="ext4"))
    logical = preview[0].logicals()
    assert [p.number for p in logical] == [5]
    with pytest.raises(OpError):  # logical outside the extended partition
        plan.add([d], CreatePartition(d.path, start=2000 * S, size=100 * S, kind="logical", fstype="ext4"))
    with pytest.raises(OpError):  # second extended
        plan.add([d], CreatePartition(d.path, start=2000 * S, size=100 * S, kind="extended", fstype=""))
    with pytest.raises(OpError):  # extended still holds a logical partition
        plan.add([d], DeletePartition(d.path, start=101 * S))


def test_delete_requires_idle():
    d = gpt_disk(parts=[(1, 512, "ext4")])
    d.partitions[0].mountpoints = ["/home"]
    with pytest.raises(OpError, match="mounted"):
        simulate([d], [DeletePartition(d.path, start=S)])


def test_resize_rules():
    d = gpt_disk(parts=[(1, 1024, "ext4"), (2048, 1024, "xfs"), (4096, 1024, "exfat")])
    # ext4 grow into free space
    p = simulate([d], [ResizeMovePartition(d.path, start=S, new_start=S, new_size=2047 * S)])[0].by_number(1)
    assert p.size == 2047 * S and "resized" in p.pending
    # cannot overlap the next partition
    with pytest.raises(OpError):
        simulate([d], [ResizeMovePartition(d.path, start=S, new_start=S, new_size=3000 * S)])
    # XFS cannot shrink, exFAT cannot grow
    with pytest.raises(OpError, match="shrunk"):
        simulate([d], [ResizeMovePartition(d.path, start=2048 * S, new_start=2048 * S, new_size=512 * S)])
    with pytest.raises(OpError, match="grown"):
        simulate([d], [ResizeMovePartition(d.path, start=4096 * S, new_start=4096 * S, new_size=2048 * S)])
    # exFAT can move though
    p = simulate([d], [ResizeMovePartition(d.path, start=4096 * S, new_start=5000 * S, new_size=1024 * S)])[0]
    assert p.find(5000 * S) is not None


def test_mounted_partition_can_only_grow_online():
    d = gpt_disk(parts=[(1, 1024, "ext4")])
    d.partitions[0].mountpoints = ["/"]
    simulate([d], [ResizeMovePartition(d.path, start=S, new_start=S, new_size=2048 * S)])
    with pytest.raises(OpError):
        simulate([d], [ResizeMovePartition(d.path, start=S, new_start=S, new_size=512 * S)])
    with pytest.raises(OpError):
        simulate([d], [ResizeMovePartition(d.path, start=S, new_start=10 * S, new_size=1024 * S)])


def test_shrink_below_used_space_is_refused():
    d = gpt_disk(parts=[(1, 1024, "ext4")])
    d.partitions[0].fs_used = 800 * MiB
    with pytest.raises(OpError, match="too small"):
        simulate([d], [ResizeMovePartition(d.path, start=S, new_start=S, new_size=512 * S)])


def test_unknown_filesystem_cannot_be_resized():
    d = gpt_disk(parts=[(1, 1024, "zfs_member")])
    with pytest.raises(OpError):
        simulate([d], [ResizeMovePartition(d.path, start=S, new_start=S, new_size=2048 * S)])


def test_format_changes_generic_type():
    d = gpt_disk(parts=[(1, 1024, "ext4")])
    p = simulate([d], [FormatPartition(d.path, start=S, fstype="ntfs", label="win")])[0].by_number(1)
    assert p.fstype == "ntfs" and p.type_id == parttypes.by_key("msdata").gpt
    d.partitions[0].type_id = ESP
    p = simulate([d], [FormatPartition(d.path, start=S, fstype="fat32")])[0].by_number(1)
    assert p.type_id == ESP  # special types are kept


def test_encrypted_format_preview():
    d = gpt_disk(parts=[(1, 1024, "")])
    p = simulate([d], [FormatPartition(d.path, start=S, fstype="ext4", encrypt=True, passphrase="x")])[0].by_number(1)
    assert p.fstype == "crypto_LUKS" and p.content() == "ext4"
    with pytest.raises(OpError):
        simulate([d], [FormatPartition(d.path, start=S, fstype="ext4", encrypt=True, passphrase="")])


def test_props_and_label():
    d = gpt_disk(parts=[(1, 1024, "ext4")])
    p = simulate([d], [SetPartitionProps(d.path, start=S, type_id="home", name="Home",
                                         attrs=["LegacyBIOSBootable"]),
                       SetLabel(d.path, start=S, label="home")])[0].by_number(1)
    assert p.name == "Home" and p.attrs == ["LegacyBIOSBootable"] and p.label == "home"
    with pytest.raises(OpError):  # boot flag is MBR only
        simulate([d], [SetPartitionProps(d.path, start=S, bootable=True)])


def test_convert_tables():
    d = mbr_disk()
    d.partitions = [Partition(number=1, start=S, size=1024 * S, kind="primary", type_id="83", path="/dev/sdy1",
                              bootable=True),
                    Partition(number=2, start=1025 * S, size=1024 * S, kind="primary", type_id="07", path="/dev/sdy2")]
    g = simulate([d], [ConvertTable(d.path, to="gpt")])[0]
    assert g.table == "gpt"
    assert [p.type_id for p in g.sorted_partitions()] == [LINUX, parttypes.by_key("msdata").gpt]
    assert g.by_number(1).attrs == ["LegacyBIOSBootable"]
    # last partition touching the end of the disk leaves no room for the backup GPT
    d.partitions[1].size = d.sectors - d.partitions[1].start
    with pytest.raises(OpError, match="backup GPT"):
        simulate([d], [ConvertTable(d.path, to="gpt")])


def test_convert_gpt_to_mbr_limits():
    d = gpt_disk(parts=[(1, 10, ""), (20, 10, ""), (40, 10, ""), (60, 10, ""), (80, 10, "")])
    with pytest.raises(OpError, match="4 primary"):
        simulate([d], [ConvertTable(d.path, to="dos")])


def test_create_table_requires_unused_disk():
    d = gpt_disk(parts=[(1, 1024, "ext4")])
    d.partitions[0].mountpoints = ["/mnt"]
    with pytest.raises(OpError):
        simulate([d], [CreateTable(d.path, "gpt")])
    big = mbr_disk(size=4 * 1024 * GiB)
    with pytest.raises(OpError, match="MBR"):
        simulate([big], [CreateTable(big.path, "dos")])


def test_copy_between_disks():
    src = gpt_disk(parts=[(1, 1024, "ext4")])
    dst = mbr_disk()
    preview = simulate([src, dst], [CopyPartition(src.path, start=S, dest_disk=dst.path, dest_start=S,
                                                  dest_size=2048 * S)])
    p = preview[1].partitions[0]
    assert p.fstype == "ext4" and p.type_id == "83" and p.fsuuid == "(new)"
    with pytest.raises(OpError, match="at least"):
        simulate([src, dst], [CopyPartition(src.path, start=S, dest_disk=dst.path, dest_start=S, dest_size=10 * S)])


def test_wipe_whole_disk_preview():
    d = gpt_disk(parts=[(1, 1024, "ext4")])
    w = simulate([d], [Wipe(d.path)])[0]
    assert w.table is None and not w.partitions


def test_plan_undo_and_prune():
    d = gpt_disk()
    plan = Plan()
    plan.add([d], CreatePartition(d.path, start=S, size=100 * S, fstype="ext4"))
    plan.add([d], CreatePartition(d.path, start=200 * S, size=100 * S, fstype="ext4"))
    assert plan.undo() is not None and len(plan.ops) == 1
    other = gpt_disk(path="/dev/sdz")
    assert len(plan.prune([other])) == 1 and plan.ops == []


def test_serialisation_and_redaction():
    op = CreatePartition("/dev/sdx", start=S, size=100 * S, fstype="ext4", encrypt=True, passphrase="secret")
    d = op.to_dict()
    assert Operation.from_dict(d) == op
    assert redact(d)["passphrase"] == "********"
    with pytest.raises(OpError):
        Operation.from_dict({"op": "rm -rf"})


# -- filesystems & distro ------------------------------------------------------
def test_lookup_and_names():
    assert filesystems.lookup("vfat", "FAT32").id == "fat32"
    assert filesystems.lookup("vfat", "FAT16").id == "fat16"
    assert filesystems.lookup("swap").id == "linux-swap"
    assert filesystems.lookup("crypto_LUKS").id == "luks"
    assert filesystems.lookup("LVM2_member") is None
    assert filesystems.display_name("LVM2_member") == "LVM physical volume"


def test_package_names():
    assert filesystems.package_for("mkfs.ntfs", "fedora") == "ntfsprogs"
    assert filesystems.package_for("mkfs.ntfs", "arch") == "ntfs-3g"
    assert filesystems.package_for("sfdisk", "debian") == "fdisk"
    assert filesystems.package_for("mkfs.btrfs", "suse") == "btrfsprogs"


@pytest.mark.parametrize("text,family", [
    ('ID=arch\n', "arch"), ('ID=endeavouros\nID_LIKE=arch\n', "arch"),
    ('ID=linuxmint\nID_LIKE="ubuntu debian"\n', "debian"), ('ID=fedora\n', "fedora"),
    ('ID="opensuse-tumbleweed"\nID_LIKE="opensuse suse"\n', "suse"), ('ID=void\n', "void"),
    ('ID=nixos\n', "nixos"), ('ID=rocky\nID_LIKE="rhel centos fedora"\n', "fedora"), ('ID=weird\n', None),
])
def test_distro_family(text, family):
    assert distro.family_of(distro.parse_os_release(text)) == family


def test_install_command():
    assert distro.install_command(["b", "a"], "arch") == "sudo pacman -S --needed a b"
    assert distro.install_command(["x"], "debian") == "sudo apt install x"


# -- Mac filesystems -------------------------------------------------------------
APPLE_HFS = "48465300-0000-11AA-AA11-00306543ECAC"
APPLE_APFS = "7C3457EF-0000-11AA-AA11-00306543ECAC"


def test_mac_filesystems_registry():
    assert filesystems.lookup("hfsplus").name == "HFS+"
    assert filesystems.lookup("apfs").name == "APFS"
    assert filesystems.lookup("hfs").implemented("check")
    assert not filesystems.lookup("hfs").implemented("create")       # classic HFS: check only
    for fs_id in ("hfsplus", "apfs"):
        fs = filesystems.BY_ID[fs_id]
        assert not fs.implemented("grow") and not fs.implemented("shrink")
        assert fs.implemented("move")
    assert filesystems.BY_ID["apfs"].warning and not filesystems.BY_ID["hfsplus"].warning


def test_mac_partition_types():
    d = gpt_disk()
    p = simulate([d], [CreatePartition(d.path, start=S, size=200 * S, fstype="hfsplus", label="Mac")])[0].partitions[0]
    assert (p.fstype, p.type_id, p.label) == ("hfsplus", APPLE_HFS, "Mac")
    p = simulate([d], [CreatePartition(d.path, start=S, size=200 * S, fstype="apfs")])[0].partitions[0]
    assert p.type_id == APPLE_APFS
    m = mbr_disk()
    p = simulate([m], [CreatePartition(m.path, start=S, size=200 * S, fstype="hfsplus")])[0].partitions[0]
    assert p.type_id == "af"


def test_apfs_rules():
    d, m = gpt_disk(), mbr_disk()
    with pytest.raises(OpError, match="GPT"):
        simulate([m], [CreatePartition(m.path, start=S, size=200 * S, fstype="apfs")])
    with pytest.raises(OpError, match="128 MiB"):
        simulate([d], [CreatePartition(d.path, start=S, size=100 * S, fstype="apfs")])
    with pytest.raises(OpError, match=":"):
        simulate([d], [CreatePartition(d.path, start=S, size=200 * S, fstype="hfsplus", label="a:b")])
    op = FormatPartition(d.path, start=S, fstype="apfs")
    assert op.warnings([]) and "experimental" in op.warnings([])[0]
    assert CreatePartition(d.path, start=S, size=200 * S, fstype="hfsplus").warnings([]) == []


def test_reformat_switches_between_linux_and_mac_types():
    d = gpt_disk(parts=[(1, 1024, "ext4")])
    p = simulate([d], [FormatPartition(d.path, start=S, fstype="hfsplus")])[0].by_number(1)
    assert p.type_id == APPLE_HFS
    d.partitions[0].type_id, d.partitions[0].fstype = APPLE_HFS, "hfsplus"
    p = simulate([d], [FormatPartition(d.path, start=S, fstype="exfat")])[0].by_number(1)
    assert p.type_id == parttypes.by_key("msdata").gpt


def test_mac_filesystems_cannot_be_resized_but_can_move():
    d = gpt_disk(parts=[(1, 1024, "hfsplus")])
    with pytest.raises(OpError, match="grown"):
        simulate([d], [ResizeMovePartition(d.path, start=S, new_start=S, new_size=2048 * S)])
    p = simulate([d], [ResizeMovePartition(d.path, start=S, new_start=2048 * S, new_size=1024 * S)])[0]
    assert p.find(2048 * S) is not None


def test_copy_of_hfsplus_keeps_uuid_in_preview():
    d = gpt_disk(parts=[(1, 1024, "hfsplus")])
    d.partitions[0].fsuuid = "abc"
    p = simulate([d], [CopyPartition(d.path, start=S, dest_disk=d.path, dest_start=2048 * S,
                                     dest_size=1024 * S)])[0].by_number(2)
    assert p.fsuuid == "abc"   # HFS+ UUIDs cannot be changed on Linux


def test_install_command_marks_unofficial_packages():
    cmd = distro.install_command(["hfsprogs", "dosfstools"], "arch")
    assert cmd.startswith("sudo pacman -S --needed dosfstools") and "AUR" in cmd and "hfsprogs" in cmd
    assert filesystems.package_for("mkfs.hfsplus", "fedora") == "hfsplus-tools"
    assert filesystems.package_for("mkapfs", "debian") == "apfsprogs"
