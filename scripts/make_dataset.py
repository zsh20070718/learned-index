#!/usr/bin/env python3
"""Make a bounded synthetic dataset or convert line-delimited text to TLI strings."""
import argparse
from pathlib import Path
import random
import string
import struct


def write_dataset(path, keys):
    keys = sorted(set(keys))
    if len(keys) < 200:
        raise ValueError("Need at least 200 distinct keys for TLI range workload generation")
    if any(not key or b"\0" in key or len(key) > 4096 for key in keys):
        raise ValueError("Keys must be nonempty, NUL-free, and at most 4096 bytes")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as out:
        out.write(struct.pack("<Q", len(keys)))
        for key in keys:
            out.write(struct.pack("<I", len(key)))
            out.write(key)
    return len(keys)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, help="New filename ending in _string")
    parser.add_argument("--input", type=Path, help="Real dataset: one byte string per line (LF/CRLF)")
    parser.add_argument("--count", type=int, default=10000, help="Synthetic key count or maximum input lines")
    parser.add_argument("--shape", choices=("random", "prefix"), default="prefix")
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()
    if not 200 <= args.count <= 1000000:
        parser.error("count must be in [200, 1000000]; larger experiments need a separate resource plan")
    if not args.output.name.endswith("_string"):
        parser.error("output filename must end in _string")
    if args.input:
        keys = []
        with args.input.open("rb") as source:
            for _ in range(args.count):
                line = source.readline(4099)
                if not line:
                    break
                if len(line) > 4098:
                    parser.error("Input line exceeds the 4096-byte key limit")
                keys.append(line.removesuffix(b"\n").removesuffix(b"\r"))
    else:
        rng = random.Random(args.seed)
        alphabet = string.ascii_lowercase + string.digits
        keys = []
        for i in range(args.count):
            suffix = "".join(rng.choices(alphabet, k=rng.randint(4, 40)))
            prefix = f"https://example.org/group/{i % 16:02d}/shared/" if args.shape == "prefix" else ""
            keys.append(f"{prefix}{suffix}/{i:08d}".encode("ascii"))
    try:
        count = write_dataset(args.output, keys)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    print(f"Wrote {count} sorted unique string keys to {args.output}")


if __name__ == "__main__":
    main()
