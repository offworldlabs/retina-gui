"""HTTP client for the blah2_api service.

Isolates all blah2_api HTTP calls so tracker_capture's logic can be tested
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

    def get_detection(self):
        """Latest per-CPI CFAR detections: {timestamp, delay[], doppler[], snr[]}."""
        return self._get_json("/api/detection")
