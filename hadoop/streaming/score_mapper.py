#!/usr/bin/env python3
"""Emit one counter record per input line; Hadoop sorts and groups them."""

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
        records = ["__total\t1"]
        if info["complete"]:
            records.append("__complete\t1")
        if info["consistent"]:
            records.append("__consistent\t1")
        if info["accurate"]:
            records.append("__accurate\t1")
        if info["timely"]:
            records.append("__timely\t1")
        if info["repaired"]:
            records.append("__repaired\t1")
            for kind in info["repair_kinds"]:
                records.append(f"__repair:{kind}\t1")
        if info["bad"]:
            records.append(f"__bad:{info['bad']}\t1")
        if info["timely"]:
            records.append(f"P:{info['row']['user']}:{info['row']['movie']}\t1")
        out.write(("\n".join(records) + "\n").encode("ascii"))


if __name__ == "__main__":
    main()
