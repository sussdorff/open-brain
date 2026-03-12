#!/usr/bin/env python3
"""Generate a bcrypt-hashed users.json entry.

Usage:
    uv run python scripts/create_user.py <username> <password>

Output (add to users.json):
    {"username": "malte", "password": "$2b$12$..."}
"""
import sys
import json
import bcrypt


def main():
    if len(sys.argv) != 3:
        print("Usage: uv run python scripts/create_user.py <username> <password>", file=sys.stderr)
        sys.exit(1)
    username = sys.argv[1]
    password = sys.argv[2].encode("utf-8")
    hashed = bcrypt.hashpw(password, bcrypt.gensalt(rounds=12)).decode("utf-8")
    entry = {"username": username, "password": hashed}
    print(json.dumps(entry))


if __name__ == "__main__":
    main()
