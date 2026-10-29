"""Windows WHEA-Logger events: the OS's record of corrected and fatal hardware errors (System log).

    read(limit)        -> list[dict]   newest first; [] off Windows or when the log cannot be read
    parse(xml_text)    -> list[dict]   the events in `wevtutil qe ... /f:xml` output
    summarize(events, ack_ts, now, caution_days) -> {"new", "recent_fatal", "last", ...}

An event: {"record_id", "event_id", "ts", "time", "level", "severity": "fatal"|"corrected"|"info", "source", "error_type",
"apic_id", "bank", "kind"}. The raw data blob is dropped. Event 18 = fatal (the machine went down), 19/47 = corrected.
"""
from __future__ import annotations

import datetime as _dt
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from typing import Any, Optional

PROVIDER = "Microsoft-Windows-WHEA-Logger"
_NS = "{http://schemas.microsoft.com/win/2004/08/events/event}"
FATAL_IDS = {18, 20, 46}
CORRECTED_IDS = {17, 19, 47}
KINDS = {17: "corrected PCIe/bus error", 18: "fatal machine check", 19: "corrected machine check", 20: "fatal PCIe/bus error",
         46: "fatal memory error", 47: "corrected memory error"}
SOURCES = {"3": "Machine Check Exception"}
ERROR_TYPES = {"9": "Cache Hierarchy Error", "10": "Bus/Interconnect Error"}   # as Windows names them in the event text


def _ts(s: str) -> float:
    s = s.rstrip("Z")
    if "." in s:
        head, frac = s.split(".", 1)
        s = f"{head}.{frac[:6]}"
    try:
        return _dt.datetime.fromisoformat(s).replace(tzinfo=_dt.timezone.utc).timestamp()
    except ValueError:
        return 0.0


def parse(xml_text: str) -> list[dict]:
    out: list[dict] = []
    for chunk in re.findall(r"<Event[ >].*?</Event>", xml_text or "", flags=re.S):
        try:
            ev = ET.fromstring(chunk)
        except ET.ParseError:
            continue
        sysn = ev.find(f"{_NS}System")
        if sysn is None:
            continue
        prov = sysn.find(f"{_NS}Provider")
        if prov is None or prov.get("Name") != PROVIDER:
            continue
        try:
            eid = int((sysn.findtext(f"{_NS}EventID") or "0").strip())
            level = int((sysn.findtext(f"{_NS}Level") or "0").strip())
        except ValueError:
            continue
        tc = sysn.find(f"{_NS}TimeCreated")
        ts = _ts(tc.get("SystemTime", "")) if tc is not None else 0.0
        data = {d.get("Name"): (d.text or "").strip() for d in ev.iter(f"{_NS}Data") if d.get("Name") and d.get("Name") != "RawData"}
        sev = "fatal" if eid in FATAL_IDS or (eid not in CORRECTED_IDS and 0 < level <= 2) else "corrected" if eid in CORRECTED_IDS or level == 3 else "info"
        out.append({"record_id": int(sysn.findtext(f"{_NS}EventRecordID") or 0), "event_id": eid, "ts": ts,
                    "time": _dt.datetime.fromtimestamp(ts, _dt.timezone.utc).isoformat(timespec="seconds") if ts else "",
                    "level": level, "severity": sev, "kind": KINDS.get(eid, f"WHEA event {eid}"),
                    "source": SOURCES.get(data.get("ErrorSource", ""), data.get("ErrorSource") or None),
                    "error_type": ERROR_TYPES.get(data.get("ErrorType", ""), data.get("ErrorType") or None),
                    "apic_id": data.get("ApicId"), "bank": data.get("MCABank")})
    out.sort(key=lambda e: (e["ts"], e["record_id"]), reverse=True)
    return out


def read(limit: int = 50) -> list[dict]:
    if sys.platform != "win32":
        return []
    q = f"*[System[Provider[@Name='{PROVIDER}']]]"
    try:
        res = subprocess.run(["wevtutil", "qe", "System", f"/q:{q}", "/f:xml", f"/c:{int(limit)}", "/rd:true"],
                             capture_output=True, text=True, timeout=20, errors="replace", creationflags=0x08000000)
    except (OSError, subprocess.SubprocessError):
        return []
    return parse(res.stdout)


def summarize(events: list[dict], ack_ts: Optional[float], now: float, caution_days: float = 7.0) -> dict[str, Any]:
    """`new`: events after the owner's last acknowledgement (or after the guard first started); `recent_fatal`: fatal events
    inside the caution window, acknowledged or not."""
    base = float(ack_ts or 0.0)
    new = [e for e in events if e["ts"] > base]
    window = now - caution_days * 86400
    recent_fatal = [e for e in events if e["severity"] == "fatal" and e["ts"] >= window]
    recent_corrected = [e for e in events if e["severity"] == "corrected" and e["ts"] >= window]
    return {"new": new, "recent_fatal": len(recent_fatal), "recent_corrected": len(recent_corrected),
            "last": events[0] if events else None, "count": len(events), "ack_ts": ack_ts}
