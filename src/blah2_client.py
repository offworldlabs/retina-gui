"""HTTP client for the blah2_api service.

Isolates all blah2_api HTTP calls so the calibrator logic can be tested
against a fake client. All getters return parsed JSON dicts or None on any
transport/parse failure — callers treat None as "no data yet".
"""

import requests


class Blah2Client:
    """Thin wrapper over blah2_api's HTTP endpoints."""

    def __init__(self, base_url, timeout=3):
        self.base_url = base_url.rstrip('/')
        self.timeout = timeout

    def _get_json(self, path):
        try:
            resp = requests.get(f"{self.base_url}{path}", timeout=self.timeout)
            resp.raise_for_status()
            return resp.json()
        except (requests.RequestException, ValueError):
            return None

    def retune(self, fc, gain_a, gain_b, lna_state):
        """Request a live retune. Returns (generation, error)."""
        try:
            resp = requests.post(
                f"{self.base_url}/capture/retune",
                json={
                    "fc": int(fc),
                    "gainReductionA": int(gain_a),
                    "gainReductionB": int(gain_b),
                    "lnaState": int(lna_state),
                },
                timeout=self.timeout,
            )
            body = resp.json()
            if resp.status_code == 200 and body.get("success"):
                return body.get("generation"), None
            return None, body.get("error") or f"HTTP {resp.status_code}"
        except requests.RequestException as e:
            return None, str(e)
        except ValueError:
            return None, "invalid response from blah2_api"

    def get_retune_status(self):
        """Last retune blah2 actually applied: {generation, fc, ..., appliedAt}."""
        return self._get_json("/capture/retune/status")

    def get_rf_status(self):
        """Per-tuner RF overload state: {overloadA, overloadB, timestamp}."""
        return self._get_json("/capture/rf-status")

    def get_detection(self):
        """Latest per-CPI CFAR detections: {timestamp, delay[], doppler[], snr[]}."""
        return self._get_json("/api/detection")

    def get_tracker(self):
        """Latest track snapshot: {timestamp, nActive, ..., data[]}."""
        return self._get_json("/api/tracker")

    def get_adsb_tracks(self):
        """Current ADS-B aircraft, extrapolated to expected delay/doppler for
        this node's rx/tx geometry and fc — {hex: {delay, doppler, ...}}.
        None on failure or if truth.adsb.enabled is false on the node."""
        return self._get_json("/api/adsb2dd")
