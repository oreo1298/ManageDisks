import os
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def _can_loop() -> bool:
    return os.geteuid() == 0 and shutil.which("losetup") is not None and os.path.exists("/dev/loop-control")


@pytest.fixture
def loopdisk(tmp_path):
    """A fresh 1 GiB loop device (root only)."""
    if not _can_loop():
        pytest.skip("needs root and loop devices")
    img = tmp_path / "disk.img"
    with open(img, "wb") as f:
        f.truncate(1024 ** 3)
    dev = subprocess.check_output(["losetup", "-f", "--show", str(img)], text=True).strip()
    try:
        yield dev
    finally:
        subprocess.run(["umount", "-R", str(tmp_path / "mnt")], capture_output=True)
        for name in (os.listdir("/dev/mapper") if os.path.isdir("/dev/mapper") else []):
            if name.startswith("managedisks-" + os.path.basename(dev)):
                subprocess.run(["cryptsetup", "close", name], capture_output=True)
        subprocess.run(["swapoff", "-a"], capture_output=True)  # loop swap from tests only
        subprocess.run(["partx", "-d", dev], capture_output=True)
        subprocess.run(["losetup", "-d", dev], capture_output=True)


def kernel_has_fs(name: str) -> bool:
    try:
        with open("/proc/filesystems") as f:
            return any(line.split()[-1] == name for line in f if line.strip())
    except OSError:
        return False


def has_capability(bit: int) -> bool:
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("CapEff:"):
                    return bool(int(line.split()[1], 16) & (1 << bit))
    except OSError:
        pass
    return False
