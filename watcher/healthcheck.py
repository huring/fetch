"""Docker HEALTHCHECK probe: hits the admin app's /healthz and exits 0/1."""
from __future__ import annotations

import os
import sys
import urllib.request


def main() -> int:
    port = os.environ.get("ADMIN_PORT", "8000")
    url = f"http://127.0.0.1:{port}/healthz"
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            return 0 if response.status == 200 else 1
    except Exception:
        return 1


if __name__ == "__main__":
    sys.exit(main())
