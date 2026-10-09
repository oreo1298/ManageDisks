"""S.M.A.R.T. health information via ``smartctl --json``."""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from .runner import have, run_simple

# ATA attributes whose raw value > 0 indicates a failing drive.
CRITICAL_ATTRS = {5, 10, 184, 187, 188, 196, 197, 198, 201}


@dataclass
class Attribute:
    id: int
    name: str
    value: int | None
    worst: int | None
    threshold: int | None
    raw: str
    failing: bool = False
    warning: bool = False


@dataclass
class SmartReport:
    available: bool
    message: str = ""
    passed: bool | None = None
    model: str = ""
    serial: str = ""
    firmware: str = ""
    temperature: int | None = None
    power_on_hours: int | None = None
    power_cycles: int | None = None
    percentage_used: int | None = None       # NVMe wear
    available_spare: int | None = None       # NVMe
    data_read: int | None = None             # bytes (NVMe)
    data_written: int | None = None          # bytes (NVMe)
    media_errors: int | None = None
    unsafe_shutdowns: int | None = None
    attributes: list[Attribute] = field(default_factory=list)
    self_test_status: str = ""
    raw: dict = field(default_factory=dict)

    @property
    def verdict(self) -> str:
        """'good', 'warning', 'bad' or 'unknown'."""
        if not self.available or self.passed is None:
            return "unknown"
        if not self.passed or any(a.failing for a in self.attributes):
            return "bad"
        if (any(a.warning for a in self.attributes) or (self.media_errors or 0) > 0
                or (self.percentage_used or 0) >= 90 or
                (self.available_spare is not None and self.available_spare < 10)):
            return "warning"
        return "good"


def read_raw(disk: str) -> dict:
    if not have("smartctl"):
        return {"_error": "smartctl is not installed (package: smartmontools)"}
    res = run_simple(["smartctl", "--json=c", "-a", disk], check=False, stderr=False, timeout=60)
    try:
        return json.loads(res.output)
    except ValueError:
        return {"_error": res.output.strip() or "smartctl produced no output"}


def parse(data: dict) -> SmartReport:
    if "_error" in data:
        return SmartReport(False, data["_error"])
    msgs = [m.get("string", "") for m in data.get("smartctl", {}).get("messages", [])
            if m.get("severity") == "error"]
    status = data.get("smart_status")
    if status is None and "ata_smart_attributes" not in data and \
            "nvme_smart_health_information_log" not in data:
        return SmartReport(False, "; ".join(msgs) or "This device does not support S.M.A.R.T.", raw=data)
    r = SmartReport(True, raw=data)
    r.passed = status.get("passed") if status else None
    r.model = data.get("model_name", "")
    r.serial = data.get("serial_number", "")
    r.firmware = data.get("firmware_version", "")
    r.temperature = (data.get("temperature") or {}).get("current")
    r.power_on_hours = (data.get("power_on_time") or {}).get("hours")
    r.power_cycles = data.get("power_cycle_count")
    nvme = data.get("nvme_smart_health_information_log")
    if nvme:
        r.percentage_used = nvme.get("percentage_used")
        r.available_spare = nvme.get("available_spare")
        r.media_errors = nvme.get("media_errors")
        r.unsafe_shutdowns = nvme.get("unsafe_shutdowns")
        if nvme.get("data_units_read") is not None:
            r.data_read = nvme["data_units_read"] * 512 * 1000
        if nvme.get("data_units_written") is not None:
            r.data_written = nvme["data_units_written"] * 512 * 1000
        if r.temperature is None:
            r.temperature = nvme.get("temperature")
    for a in (data.get("ata_smart_attributes") or {}).get("table", []):
        raw = a.get("raw", {})
        attr = Attribute(
            id=a.get("id", 0), name=a.get("name", "").replace("_", " "),
            value=a.get("value"), worst=a.get("worst"), threshold=a.get("thresh"),
            raw=str(raw.get("string", raw.get("value", ""))),
        )
        attr.failing = bool(a.get("when_failed")) or (
            attr.threshold not in (None, 0) and attr.value is not None and attr.value <= attr.threshold)
        if attr.id in CRITICAL_ATTRS:
            try:
                attr.warning = int(raw.get("value", 0)) > 0
            except (TypeError, ValueError):
                pass
        r.attributes.append(attr)
    st = (data.get("ata_smart_data") or {}).get("self_test", {}).get("status", {})
    if st:
        r.self_test_status = st.get("string", "")
    return r


def start_self_test(disk: str, kind: str = "short") -> str:
    if kind not in ("short", "long", "conveyance"):
        raise ValueError("Unknown self-test type")
    res = run_simple(["smartctl", "-t", kind, disk], check=False, timeout=60)
    if res.returncode & 0b11:
        raise RuntimeError(res.output.strip().splitlines()[-1] if res.output.strip() else "smartctl failed")
    for line in res.output.splitlines():
        if "Please wait" in line or "Testing has begun" in line:
            return line.strip()
    return "Self-test started"
