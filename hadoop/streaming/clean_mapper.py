#!/usr/bin/env python3
"""Emit usable rows in canonical form, keyed by user and movie for deduplication."""

from __future__ import annotations

import json
import sys

from scoring import classify


def load_rules() -> dict:
    path = sys.argv[1] if len(sys.argv) > 1 else "rules.json"
    with open(path, encoding="utf-8") as stream:
        return json.load(stream)


def main() -> None:
    rules = load_rules()
    out = sys.stdout.buffer
    for raw in sys.stdin.buffer:
        info = classify(raw.decode("latin-1"), rules)
        if info["bad"] is not None:
            continue
        row = info["row"]
        out.write(
            f"C:{row['user']}:{row['movie']}\t{row['timestamp']}\t{row['rating']:g}\n".encode("ascii")
        )


if __name__ == "__main__":
    main()
