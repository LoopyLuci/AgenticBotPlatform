"""Windows Firewall inbound-rule check and one-click fix for the dashboard
port — closes the single most common cause of "server linking doesn't
work": DASHBOARD_HOST=0.0.0.0 makes the app itself listen on every
interface, but that alone does nothing to the OS firewall, which silently
drops unsolicited inbound connections by default (a timeout, not a clean
rejection — the exact symptom that made this hard to diagnose from the
error message alone the first time around).

Only manages one specifically-named rule per port ("BotServer Dashboard
(TCP <port>)") rather than trying to determine whether the port is
*somehow* already reachable through some other rule, group policy, or
third-party firewall product — that's not reliably decidable from the
outside, and pretending otherwise would just trade one confusing failure
mode for another. This is honest about its own limits: a missing rule
warning means "we didn't add one," not "definitely blocked," and a
present rule means "we added it," not "definitely reachable" (a router or
another firewall product could still be in the way — see the docs this
module's callers point to).

Windows-only by design — Linux (ufw/firewalld/nftables) and macOS
(pf/socketfilterfw) firewalls vary too much in defaults and tooling to
offer one equally reliable check-and-fix here; both platforms are far
less likely to block LAN traffic by default in the first place. On
non-Windows, is_supported() is False and every other function is a no-op.
"""

from __future__ import annotations

import platform
import subprocess
from typing import Optional

_NO_WINDOW = 0x08000000  # CREATE_NO_WINDOW — see bot/desktop.py's same convention
_ERROR_CANCELLED = 1223  # Windows' own code for "the UAC prompt was declined"


def is_supported() -> bool:
    return platform.system() == "Windows"


def _rule_name(port: int) -> str:
    return f"BotServer Dashboard (TCP {port})"


def has_inbound_rule(port: int) -> Optional[bool]:
    """Whether *our* named rule exists for this port. Returns None (not
    False) on non-Windows or if the check itself fails to run — an unknown
    state is different from a confirmed-missing rule, and callers should
    show that distinction rather than a false "not present" warning.

    Checked by message content, not exit code: netsh's own exit code for
    "no rules match" is 1, not 0 — treating any non-zero exit as "unknown"
    would misreport the single most common real answer (a genuinely
    missing rule) as "couldn't tell"."""
    if not is_supported():
        return None
    try:
        result = subprocess.run(
            ["netsh", "advfirewall", "firewall", "show", "rule", f"name={_rule_name(port)}"],
            capture_output=True, text=True, timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception:
        return None
    if "No rules match" in result.stdout:
        return False
    if "Rule Name:" in result.stdout:
        return True
    return None


def open_inbound_port(port: int) -> tuple[bool, str]:
    """Adds the named inbound-allow rule, elevated via a UAC prompt the
    user has to explicitly approve — this process itself never runs
    elevated, only this one netsh command does, and only when the user
    clicks the button that calls this. Waits for the elevated process to
    actually finish (Start-Process -Wait -PassThru) so a real exit code
    comes back instead of a fire-and-forget launch with no way to tell
    success from a declined prompt."""
    if not is_supported():
        return False, "firewall automation is only implemented for Windows"

    rule_name = _rule_name(port)
    netsh_args = f'advfirewall firewall add rule name="{rule_name}" dir=in action=allow protocol=TCP localport={port}'
    ps_command = (
        "try { "
        f"$p = Start-Process -FilePath netsh.exe -ArgumentList '{netsh_args}' "
        "-Verb RunAs -WindowStyle Hidden -Wait -PassThru; "
        "exit $p.ExitCode "
        f"}} catch {{ exit {_ERROR_CANCELLED} }}"
    )
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps_command],
            capture_output=True, text=True, timeout=60,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception as exc:
        return False, f"couldn't launch the elevated firewall command: {exc}"

    if result.returncode == 0:
        return True, f"added an inbound rule allowing TCP {port}"
    if result.returncode == _ERROR_CANCELLED:
        return False, "the UAC prompt was declined — no rule was added"
    return False, f"netsh exited with code {result.returncode} — the rule may not have been added"


def status(port: int) -> dict:
    """One call for the dashboard endpoint: supported-ness plus current
    rule presence, in the shape the UI actually needs."""
    return {
        "supported": is_supported(),
        "port": port,
        "rule_present": has_inbound_rule(port) if is_supported() else None,
    }
