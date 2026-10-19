#!/usr/bin/env python3
"""Sum counter records and count distinct (user, movie) pairs.

Hadoop guarantees sorted keys, so every ``P:`` key appears exactly once as a
contiguous group; counting a new group keeps memory constant.
"""

from __future__ import annotations

import sys


def main() -> None:
    out = sys.stdout.buffer
    counts: dict[str, int] = {}
    unique = 0
    previous = None
    for raw in sys.stdin.buffer:
        key, separator, value = raw.rstrip(b"\r\n").decode("ascii").partition("\t")
        if not separator:
            continue
        if key.startswith("P:"):
            if key != previous:
                unique += 1
                previous = key
            continue
        try:
            counts[key] = counts.get(key, 0) + int(value)
        except ValueError:
            continue
    for key, value in counts.items():
        out.write(f"M\t{key}\t{value}\n".encode("ascii"))
    out.write(f"M\t__unique\t{unique}\n".encode("ascii"))


if __name__ == "__main__":
    main()
