#!/usr/bin/env python3
"""Setup script for open-brain plugin: configure server URL and API key."""

import json
import sys
import urllib.request
import urllib.error
from pathlib import Path

CONFIG_DIR = Path.home() / ".open-brain"
CONFIG_FILE = CONFIG_DIR / "config.json"


def main():
    print("open-brain Plugin Setup")
    print("=" * 40)
    print()

    # Load existing config if present
    existing = {}
    if CONFIG_FILE.exists():
        try:
            existing = json.loads(CONFIG_FILE.read_text())
            print(f"Existing config found at {CONFIG_FILE}")
            print()
        except Exception:
            pass

    # Server URL
    default_url = existing.get("server_url", "http://localhost:8091")
    server_url = input(f"Server URL [{default_url}]: ").strip() or default_url

    # Check server health
    print(f"\nChecking {server_url}/health ...")
    try:
        req = urllib.request.Request(f"{server_url.rstrip('/')}/health")
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode())
            if data.get("status") == "ok":
                print(f"  Server is healthy: {data}")
            else:
                print(f"  Warning: unexpected response: {data}")
    except Exception as e:
        print(f"  Warning: Could not reach server: {e}")
        proceed = input("Continue anyway? [y/N]: ").strip().lower()
        if proceed != "y":
            print("Setup cancelled.")
            sys.exit(1)

    # API key
    default_key = existing.get("api_key", "")
    masked = f"{default_key[:6]}...{default_key[-4:]}" if len(default_key) > 10 else default_key
    api_key = input(f"\nAPI Key [{masked or 'none'}]: ").strip() or default_key

    if api_key:
        # Verify API key
        print("\nVerifying API key ...")
        try:
            req = urllib.request.Request(
                f"{server_url.rstrip('/')}/api/context?limit=1",
                headers={"X-API-Key": api_key},
            )
            urllib.request.urlopen(req, timeout=5)
            print("  API key is valid.")
        except urllib.error.HTTPError as e:
            if e.code == 401:
                print("  Warning: API key rejected (401 Unauthorized).")
            else:
                print(f"  Warning: Server returned {e.code}.")
        except Exception as e:
            print(f"  Warning: Could not verify: {e}")

    # Write config
    config = {
        **existing,
        "server_url": server_url,
        "api_key": api_key,
    }

    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(config, indent=2) + "\n")
    print(f"\nConfig written to {CONFIG_FILE}")
    print("\nSetup complete. The plugin will start capturing observations on your next Claude Code session.")


if __name__ == "__main__":
    main()
