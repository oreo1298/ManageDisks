"""Synthetic disks for tests that must not touch real devices."""

from managedisks.core.model import Disk, Partition, estimate_usable
from managedisks.core.units import GiB, MiB

LINUX = "0FC63DAF-8483-4772-8E79-3D69D8477DE4"
ESP = "C12A7328-F81F-11D2-BA4B-00A0C93EC93B"


def gpt_disk(size=100 * GiB, parts=(), path="/dev/sdx"):
    d = Disk(path=path, name=path.rsplit("/", 1)[-1], size=size, table="gpt")
    d.first_usable, d.last_usable = estimate_usable("gpt", d.sectors, 512)
    for i, (start_mib, size_mib, fstype) in enumerate(parts, 1):
        d.partitions.append(Partition(number=i, start=start_mib * MiB // 512, size=size_mib * MiB // 512,
                                      path=f"{path}{i}", type_id=LINUX, fstype=fstype,
                                      fs_size=size_mib * MiB if fstype else None))
    return d


def mbr_disk(size=100 * GiB, path="/dev/sdy"):
    d = Disk(path=path, name=path.rsplit("/", 1)[-1], size=size, table="dos")
    d.first_usable, d.last_usable = estimate_usable("dos", d.sectors, 512)
    return d
