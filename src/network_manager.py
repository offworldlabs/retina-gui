"""Network status, WiFi scanning, and WiFi connect for retina-gui.

Wraps nmcli (NetworkManager) the same way mender.py wraps the Mender CLI —
plain subprocess calls, no persisted state beyond what NetworkManager itself
keeps. Connecting to a new WiFi network never deletes another connection
profile: nmcli only touches the profile for the SSID being connected to, so
a previously-saved network stays available for autoconnect if the new one
fails.
"""

import ipaddress
import re
import subprocess
import threading
import time


DEV_NETWORKS = [
    {"ssid": "Dev Office WiFi", "signal": 88, "secured": True},
    {"ssid": "Dev Guest", "signal": 54, "secured": False},
    {"ssid": "Neighbor 5G", "signal": 31, "secured": True},
]


def _unescape_terse(value: str) -> str:
    """Undo nmcli's `-t` escaping of ':' and '\\' within a field."""
    return re.sub(r'\\(.)', r'\1', value)


def _split_terse_line(line: str, n_fields: int) -> list[str]:
    """Split an nmcli -t line on unescaped colons into exactly n_fields."""
    parts = re.split(r'(?<!\\):', line)
    parts = [_unescape_terse(p) for p in parts]
    # Tolerate trailing fields nmcli sometimes omits (e.g. empty SECURITY).
    while len(parts) < n_fields:
        parts.append('')
    return parts[:n_fields]


class NetworkManager:
    """Reads network status and drives WiFi scan/connect via nmcli."""

    def __init__(self, dev_mode: bool = False):
        self.dev_mode = dev_mode
        self._connect_lock = threading.Lock()
        self._connect_status = {"state": "idle", "ssid": None, "error": None}
        self._dev_wifi_ssid = None

    # ── Status ─────────────────────────────────────────────────

    def get_network_status(self, client_ip: str | None = None) -> dict:
        """Ethernet/WiFi connection summary for the Network panel.

        `client_ip` is the requesting browser's address (Flask's
        request.remote_addr) — used to warn the user when their own
        session is riding over the WiFi network they're about to
        reconfigure, since that connection won't survive the switch.
        """
        if self.dev_mode:
            return {
                "ethernet_connected": False,
                "wifi_connected": self._dev_wifi_ssid is not None,
                "wifi_ssid": self._dev_wifi_ssid,
                "client_on_wifi": False,
            }

        ethernet_connected = False
        wifi_connected = False
        try:
            result = subprocess.run(
                ["nmcli", "-t", "-f", "TYPE,STATE", "device", "status"],
                capture_output=True, text=True, timeout=5,
            )
            for line in result.stdout.strip().splitlines():
                dev_type, state = _split_terse_line(line, 2)
                if dev_type == "ethernet" and state == "connected":
                    ethernet_connected = True
                if dev_type == "wifi" and state == "connected":
                    wifi_connected = True
        except Exception:
            pass

        wifi_ssid = None
        if wifi_connected:
            try:
                result = subprocess.run(
                    ["nmcli", "-t", "-f", "ACTIVE,SSID", "device", "wifi"],
                    capture_output=True, text=True, timeout=5,
                )
                for line in result.stdout.strip().splitlines():
                    active, ssid = _split_terse_line(line, 2)
                    if active == "yes" and ssid:
                        wifi_ssid = ssid
                        break
            except Exception:
                pass

        return {
            "ethernet_connected": ethernet_connected,
            "wifi_connected": wifi_connected,
            "wifi_ssid": wifi_ssid,
            "client_on_wifi": wifi_connected and self._is_client_on_wifi_subnet(client_ip),
        }

    def _get_wifi_device(self) -> str | None:
        try:
            result = subprocess.run(
                ["nmcli", "-t", "-f", "DEVICE,TYPE", "device", "status"],
                capture_output=True, text=True, timeout=5,
            )
        except Exception:
            return None
        for line in result.stdout.strip().splitlines():
            device, dev_type = _split_terse_line(line, 2)
            if dev_type == "wifi":
                return device
        return None

    def _is_client_on_wifi_subnet(self, client_ip: str | None) -> bool:
        """Best-effort check: does client_ip fall within the WiFi device's subnet?

        A heuristic, not a guarantee — but good enough to warn a user before
        they reconfigure the WiFi network their own browser session is
        likely using.
        """
        if not client_ip:
            return False
        try:
            client = ipaddress.ip_address(client_ip)
        except ValueError:
            return False

        device = self._get_wifi_device()
        if not device:
            return False

        try:
            result = subprocess.run(
                ["nmcli", "-t", "-f", "IP4.ADDRESS", "device", "show", device],
                capture_output=True, text=True, timeout=5,
            )
        except Exception:
            return False

        for cidr in re.findall(r'\d{1,3}(?:\.\d{1,3}){3}/\d{1,2}', result.stdout):
            try:
                if client in ipaddress.ip_interface(cidr).network:
                    return True
            except ValueError:
                continue
        return False

    # ── Scan ───────────────────────────────────────────────────

    def scan_wifi(self) -> list[dict]:
        """Nearby WiFi networks, de-duped by SSID (strongest signal kept)."""
        if self.dev_mode:
            return list(DEV_NETWORKS)

        try:
            result = subprocess.run(
                ["nmcli", "-t", "-f", "SSID,SIGNAL,SECURITY", "device", "wifi",
                 "list", "--rescan", "yes"],
                capture_output=True, text=True, timeout=15,
            )
        except Exception:
            return []

        best_by_ssid: dict[str, dict] = {}
        for line in result.stdout.strip().splitlines():
            ssid, signal_raw, security = _split_terse_line(line, 3)
            if not ssid:
                continue  # hidden network — use manual entry instead
            try:
                signal = int(signal_raw)
            except ValueError:
                signal = 0
            secured = bool(security)
            existing = best_by_ssid.get(ssid)
            if existing is None or signal > existing["signal"]:
                best_by_ssid[ssid] = {"ssid": ssid, "signal": signal, "secured": secured}

        return sorted(best_by_ssid.values(), key=lambda n: n["signal"], reverse=True)

    # ── Connect ────────────────────────────────────────────────

    def connect_wifi(self, ssid: str, password: str | None = None, hidden: bool = False):
        """Start a background WiFi connect attempt. Returns immediately."""
        with self._connect_lock:
            self._connect_status = {"state": "connecting", "ssid": ssid, "error": None}

        if self.dev_mode:
            threading.Thread(target=self._dev_connect, args=(ssid,), daemon=True).start()
            return

        threading.Thread(
            target=self._run_connect, args=(ssid, password, hidden), daemon=True
        ).start()

    def get_connect_status(self) -> dict:
        with self._connect_lock:
            return dict(self._connect_status)

    def _dev_connect(self, ssid: str):
        time.sleep(2)
        with self._connect_lock:
            self._dev_wifi_ssid = ssid
            self._connect_status = {"state": "connected", "ssid": ssid, "error": None}

    def _run_connect(self, ssid: str, password: str | None, hidden: bool):
        cmd = ["nmcli", "device", "wifi", "connect", ssid]
        if password:
            cmd += ["password", password]
        if hidden:
            cmd += ["hidden", "yes"]

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=45)
        except subprocess.TimeoutExpired:
            with self._connect_lock:
                self._connect_status = {"state": "failed", "ssid": ssid, "error": "Connection attempt timed out"}
            return
        except Exception as e:
            with self._connect_lock:
                self._connect_status = {"state": "failed", "ssid": ssid, "error": str(e)}
            return

        if result.returncode != 0:
            with self._connect_lock:
                self._connect_status = {
                    "state": "failed", "ssid": ssid,
                    "error": (result.stderr or result.stdout or "nmcli connect failed").strip(),
                }
            return

        # Don't trust the exit code alone — verify the connection is actually
        # active before telling the UI it's safe to take the old network down.
        if self._is_ssid_active(ssid):
            with self._connect_lock:
                self._connect_status = {"state": "connected", "ssid": ssid, "error": None}
        else:
            with self._connect_lock:
                self._connect_status = {
                    "state": "failed", "ssid": ssid,
                    "error": "nmcli reported success but the connection isn't active",
                }

    def _is_ssid_active(self, ssid: str) -> bool:
        try:
            result = subprocess.run(
                ["nmcli", "-t", "-f", "ACTIVE,SSID", "device", "wifi"],
                capture_output=True, text=True, timeout=5,
            )
        except Exception:
            return False
        for line in result.stdout.strip().splitlines():
            active, line_ssid = _split_terse_line(line, 2)
            if active == "yes" and line_ssid == ssid:
                return True
        return False
