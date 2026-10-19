#!/usr/bin/env python3
"""Keep the newest rating per (user, movie) and emit MovieLens ``::`` lines."""

from __future__ import annotations

import sys


def main() -> None:
    out = sys.stdout.buffer
    previous = None
    best_timestamp = None
    best_rating = None

    def flush() -> None:
        if previous is None:
            return
        _, user, movie = previous.split(":", 2)
        line = f"{user}::{movie}::{best_rating}::{best_timestamp}\n"
        out.write(line.encode("latin-1"))

    for raw in sys.stdin.buffer:
        key, separator, value = raw.rstrip(b"\r\n").decode("ascii").partition("\t")
        if not separator:
            continue
        timestamp_text, _, rating_text = value.partition("\t")
        try:
            timestamp = int(timestamp_text)
            rating = f"{float(rating_text):g}"
        except (ValueError, OverflowError):
            continue
        if key != previous:
            flush()
            previous = key
            best_timestamp = timestamp
            best_rating = rating
        elif timestamp >= best_timestamp:
            best_timestamp = timestamp
            best_rating = rating
    flush()


if __name__ == "__main__":
    main()
