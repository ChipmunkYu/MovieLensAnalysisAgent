#!/usr/bin/env python3
"""Pass quarantined rows through as ``reason<TAB>original`` lines."""

from __future__ import annotations

import sys


def main() -> None:
    out = sys.stdout.buffer
    for raw in sys.stdin.buffer:
        _, separator, value = raw.rstrip(b"\r\n").partition(b"\t")
        if separator:
            out.write(value + b"\n")


if __name__ == "__main__":
    main()
