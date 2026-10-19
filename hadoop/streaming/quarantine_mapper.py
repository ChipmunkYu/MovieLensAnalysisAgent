#!/usr/bin/env python3
"""Write rejected rows with a reason instead of silently dropping them."""

from __future__ import annotations

import json
import os
import sys

from scoring import classify


def load_rules() -> dict:
    path = sys.argv[1] if len(sys.argv) > 1 else "rules.json"
    with open(path, encoding="utf-8") as stream:
        return json.load(stream)


def main() -> None:
    rules = load_rules()
    out = sys.stdout.buffer
    process = os.getpid()
    counter = 0
    for raw in sys.stdin.buffer:
        info = classify(raw.decode("latin-1"), rules)
        if info["bad"] is None:
            continue
        counter += 1
        out.write(f"Q:{process}:{counter}\t{info['bad']}\t{info['raw']}\n".encode("latin-1"))


if __name__ == "__main__":
    main()
