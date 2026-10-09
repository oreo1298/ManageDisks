"""Partition type identifiers for GPT and MBR (DOS) partition tables."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PartType:
    key: str          # stable short key used by the app / CLI ("linux", "esp", ...)
    name: str         # human readable name
    gpt: str | None   # GPT type GUID (upper case)
    mbr: str | None   # MBR type code, lower-case hex without 0x ("83")


PART_TYPES: list[PartType] = [
    PartType("linux", "Linux filesystem", "0FC63DAF-8483-4772-8E79-3D69D8477DE4", "83"),
    PartType("swap", "Linux swap", "0657FD6D-A4AB-43C4-84E5-0933C84B4F4F", "82"),
    PartType("esp", "EFI System", "C12A7328-F81F-11D2-BA4B-00A0C93EC93B", "ef"),
    PartType("xbootldr", "Linux extended boot (/boot)", "BC13C2FF-59E6-4262-A352-B275FD6F7172", "ea"),
    PartType("bios_grub", "BIOS boot", "21686148-6449-6E6F-744E-656564454649", None),
    PartType("lvm", "Linux LVM", "E6D6D379-F507-44C2-A23C-238F2A3DF928", "8e"),
    PartType("raid", "Linux RAID", "A19D880F-05FC-4D3B-A006-743F0F84911E", "fd"),
    PartType("luks", "Linux LUKS", "CA7D7CCB-63ED-4C53-861C-1742536059CC", None),
    PartType("home", "Linux /home", "933AC7E1-2EB4-4F13-B844-0E14E2AEF915", None),
    PartType("root-x86-64", "Linux root (x86-64)", "4F68BCE3-E8CD-4DB1-96E7-FBCAF984B709", None),
    PartType("root-arm64", "Linux root (ARM64)", "B921B045-1DF0-41C3-AF44-4C6F280D3FAE", None),
    PartType("srv", "Linux /srv", "3B8F8425-20E0-4F3B-907F-1A25A76F98E8", None),
    PartType("var", "Linux /var", "4D21B016-B534-45C2-A9FB-5C16E091FD2D", None),
    PartType("msdata", "Microsoft basic data", "EBD0A0A2-B9E5-4433-87C0-68B6B72699C7", "07"),
    PartType("msr", "Microsoft reserved", "E3C9E316-0B5C-4DB8-817D-F92DF00215AE", None),
    PartType("winre", "Windows recovery environment", "DE94BBA4-06D1-4D40-A16A-BFD50179D6AC", "27"),
    PartType("fat32", "W95 FAT32 (LBA)", None, "0c"),
    PartType("fat16", "W95 FAT16 (LBA)", None, "0e"),
    PartType("extended", "Extended", None, "05"),
    PartType("apple-hfs", "Apple HFS/HFS+", "48465300-0000-11AA-AA11-00306543ECAC", "af"),
    PartType("apple-apfs", "Apple APFS", "7C3457EF-0000-11AA-AA11-00306543ECAC", None),
    PartType("zfs", "Solaris /usr & Apple ZFS", "6A898CC3-1DD2-11B2-99A6-080020736631", "bf"),
    PartType("freebsd", "FreeBSD data", "516E7CB4-6ECF-11D6-8FF8-00022D09712B", "a5"),
    PartType("chromeos-kernel", "ChromeOS kernel", "FE3A2A5D-4F32-41A7-B725-ACCC3285A309", None),
]

_BY_KEY = {t.key: t for t in PART_TYPES}
_BY_GPT = {t.gpt: t for t in PART_TYPES if t.gpt}
_BY_MBR = {t.mbr: t for t in PART_TYPES if t.mbr}

# Extra well-known MBR codes for nicer display.
_MBR_NAMES = {
    "00": "Empty", "01": "FAT12", "04": "FAT16 <32M", "05": "Extended",
    "06": "FAT16", "07": "HPFS/NTFS/exFAT", "0b": "W95 FAT32", "0c": "W95 FAT32 (LBA)",
    "0e": "W95 FAT16 (LBA)", "0f": "W95 Extended (LBA)", "11": "Hidden FAT12",
    "12": "Compaq diagnostics", "14": "Hidden FAT16 <32M", "16": "Hidden FAT16",
    "17": "Hidden HPFS/NTFS", "1b": "Hidden W95 FAT32", "1c": "Hidden W95 FAT32 (LBA)",
    "1e": "Hidden W95 FAT16 (LBA)", "27": "Hidden NTFS WinRE", "42": "SFS / Windows dynamic",
    "82": "Linux swap / Solaris", "83": "Linux", "85": "Linux extended", "8e": "Linux LVM",
    "a5": "FreeBSD", "a6": "OpenBSD", "a8": "Darwin UFS", "a9": "NetBSD", "af": "HFS / HFS+",
    "bf": "Solaris", "da": "Non-FS data", "ea": "Linux extended boot", "ee": "GPT protective",
    "ef": "EFI (FAT-12/16/32)", "fb": "VMware VMFS", "fc": "VMware VMKCORE", "fd": "Linux raid autodetect",
}

EXTENDED_MBR_CODES = {"05", "0f", "85"}

# Partition types that are "generic data" types. Formatting a partition with
# one of these types may switch it to the type matching the new filesystem.
GENERIC_TYPES = {
    "0FC63DAF-8483-4772-8E79-3D69D8477DE4", "EBD0A0A2-B9E5-4433-87C0-68B6B72699C7",
    "0657FD6D-A4AB-43C4-84E5-0933C84B4F4F", "CA7D7CCB-63ED-4C53-861C-1742536059CC",
    "48465300-0000-11AA-AA11-00306543ECAC", "7C3457EF-0000-11AA-AA11-00306543ECAC",
    "83", "07", "0b", "0c", "0e", "06", "82", "af",
}


def normalize_type(type_id: str | None, table: str | None) -> str:
    """Normalise a type id as reported by lsblk/sfdisk to our canonical form."""
    if not type_id:
        return ""
    t = type_id.strip()
    if table == "dos" or (len(t) <= 4 and not "-" in t):
        t = t.lower()
        if t.startswith("0x"):
            t = t[2:]
        return t.zfill(2)
    return t.upper()


def by_key(key: str) -> PartType | None:
    return _BY_KEY.get(key)


def type_name(type_id: str, table: str | None) -> str:
    t = normalize_type(type_id, table)
    if not t:
        return ""
    if table == "gpt":
        pt = _BY_GPT.get(t)
        return pt.name if pt else t
    return _MBR_NAMES.get(t) or (_BY_MBR[t].name if t in _BY_MBR else f"0x{t}")


def resolve(value: str, table: str) -> str:
    """Turn a key ("linux"), a GUID or an MBR code into a type id for ``table``."""
    v = value.strip()
    pt = _BY_KEY.get(v.lower())
    if pt:
        tid = pt.gpt if table == "gpt" else pt.mbr
        if not tid:
            raise ValueError(f"Partition type '{pt.name}' is not available on {table_label(table)} disks")
        return tid
    if table == "gpt":
        u = v.upper()
        if len(u) == 36 and u.count("-") == 4:
            return u
        raise ValueError(f"Not a valid GPT type: {value!r}")
    code = v.lower()
    if code.startswith("0x"):
        code = code[2:]
    try:
        n = int(code, 16)
    except ValueError:
        raise ValueError(f"Not a valid MBR type code: {value!r}") from None
    if not 0 < n < 256:
        raise ValueError(f"MBR type code out of range: {value!r}")
    return f"{n:02x}"


def choices(table: str) -> list[PartType]:
    return [t for t in PART_TYPES if (t.gpt if table == "gpt" else t.mbr) and t.key != "extended"]


def table_label(table: str | None) -> str:
    return {"gpt": "GPT", "dos": "MBR"}.get(table or "", "No partition table")


def mbr_to_gpt(code: str) -> str:
    code = normalize_type(code, "dos")
    mapping = {
        "83": "linux", "82": "swap", "8e": "lvm", "fd": "raid", "ef": "esp", "ea": "xbootldr",
        "07": "msdata", "0b": "msdata", "0c": "msdata", "0e": "msdata", "06": "msdata",
        "04": "msdata", "01": "msdata", "27": "winre", "af": "apple-hfs", "a5": "freebsd",
    }
    return _BY_KEY[mapping.get(code, "linux")].gpt  # type: ignore[return-value]


def gpt_to_mbr(guid: str) -> str:
    pt = _BY_GPT.get(normalize_type(guid, "gpt"))
    if pt and pt.mbr:
        return pt.mbr
    if pt and pt.key in ("home", "root-x86-64", "root-arm64", "srv", "var", "luks", "xbootldr"):
        return "83"
    return "83"


def default_type_for_fs(fstype: str, table: str) -> str:
    key = {
        "linux-swap": "swap", "swap": "swap",
        "ntfs": "msdata", "exfat": "msdata",
        "fat32": "msdata" if table == "gpt" else "fat32",
        "fat16": "msdata" if table == "gpt" else "fat16",
        "vfat": "msdata" if table == "gpt" else "fat32",
        "luks": "linux",
        "hfsplus": "apple-hfs", "hfs": "apple-hfs",
        "apfs": "apple-apfs",
    }.get(fstype, "linux")
    return resolve(key, table)
