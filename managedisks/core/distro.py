"""Linux distribution detection and package name hints."""

from __future__ import annotations

import functools
import os

FAMILIES = ("arch", "debian", "fedora", "suse", "void", "alpine", "gentoo", "nixos", "solus", "mageia")

_FAMILY_IDS = {
    "arch": {"arch", "archarm", "manjaro", "manjaro-arm", "endeavouros", "garuda", "cachyos",
             "artix", "arcolinux", "rebornos", "biglinux", "steamos", "parabola"},
    "debian": {"debian", "ubuntu", "linuxmint", "pop", "elementary", "zorin", "kali", "raspbian",
               "neon", "deepin", "mx", "pureos", "devuan", "parrot", "tuxedo", "kubuntu", "xubuntu"},
    "fedora": {"fedora", "rhel", "centos", "rocky", "almalinux", "nobara", "ultramarine", "ol",
               "amzn", "bazzite"},
    "suse": {"opensuse", "opensuse-tumbleweed", "opensuse-leap", "opensuse-slowroll", "sles",
             "suse", "opensuse-microos", "aeon", "kalpa"},
    "void": {"void"},
    "alpine": {"alpine", "postmarketos"},
    "gentoo": {"gentoo", "funtoo", "calculate"},
    "nixos": {"nixos"},
    "solus": {"solus"},
    "mageia": {"mageia", "openmandriva"},
}

INSTALL_CMD = {
    "arch": "sudo pacman -S --needed {pkgs}",
    "debian": "sudo apt install {pkgs}",
    "fedora": "sudo dnf install {pkgs}",
    "suse": "sudo zypper install {pkgs}",
    "void": "sudo xbps-install -S {pkgs}",
    "alpine": "sudo apk add {pkgs}",
    "gentoo": "sudo emerge --ask {pkgs}",
    "nixos": "Add to environment.systemPackages: {pkgs}",
    "solus": "sudo eopkg install {pkgs}",
    "mageia": "sudo dnf install {pkgs}",
}


def parse_os_release(text: str) -> dict[str, str]:
    data: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        data[k.strip()] = v
    return data


@functools.lru_cache(maxsize=1)
def os_release() -> dict[str, str]:
    for path in ("/etc/os-release", "/usr/lib/os-release"):
        try:
            with open(path, encoding="utf-8") as f:
                return parse_os_release(f.read())
        except OSError:
            continue
    return {}


def family_of(info: dict[str, str]) -> str | None:
    ids = [info.get("ID", "").lower()] + info.get("ID_LIKE", "").lower().split()
    for i in ids:
        for fam, members in _FAMILY_IDS.items():
            if i in members:
                return fam
    for i in ids:  # "ubuntu" in ID_LIKE etc. handled above; try prefixes
        for fam in FAMILIES:
            if i.startswith(fam):
                return fam
    return None


@functools.lru_cache(maxsize=1)
def family() -> str | None:
    env = os.environ.get("MANAGEDISKS_DISTRO_FAMILY")
    if env:
        return env
    return family_of(os_release())


def pretty_name() -> str:
    info = os_release()
    return info.get("PRETTY_NAME") or info.get("NAME") or "Linux"


def install_command(packages: list[str], fam: str | None = None) -> str:
    from .filesystems import UNOFFICIAL
    fam = fam or family()
    wanted = sorted(set(p for p in packages if p))
    unofficial = [p for p in wanted if p in UNOFFICIAL.get(fam or "", set())]
    official = [p for p in wanted if p not in unofficial]
    parts = []
    if official:
        template = INSTALL_CMD.get(fam or "", "Install with your package manager: {pkgs}")
        parts.append(template.format(pkgs=" ".join(official)))
    if unofficial:
        parts.append("not in the official repositories (look in the AUR): " + " ".join(unofficial))
    return "; ".join(parts)
