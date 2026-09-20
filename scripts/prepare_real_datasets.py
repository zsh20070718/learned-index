#!/usr/bin/env python3
"""Fetch pinned upstream-linked corpora and sample byte-preserving TLI keysets."""
import argparse
import gzip
import hashlib
import heapq
import json
import os
from pathlib import Path
import time
import urllib.request

from make_dataset import write_dataset
from make_lookup_workload import MAX_DATA_BYTES, sha256

ROOT = Path(__file__).resolve().parents[1]
MAX_EXPANDED_BYTES = 4 * 1024**3
MAX_LINE_BYTES = 64 * 1024


def fetch(source, raw_dir, offline=False):
    """Verify cached bytes, or download once with size/time bounds and SHA-256."""
    path = raw_dir / source["filename"]
    if path.exists():
        if path.stat().st_size != source["bytes"] or sha256(path) != source["sha256"]:
            raise ValueError(f"Cached source does not match lock: {path}")
        return path
    if offline:
        raise FileNotFoundError(f"Offline source missing: {path}")
    raw_dir.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + ".part")
    # Exclusive creation also prevents a concurrent fetch from sharing a partial.
    with partial.open("xb") as out:
        try:
            started, size = time.monotonic(), 0
            digest = hashlib.sha256()
            request = urllib.request.Request(source["url"], headers={"User-Agent": "string-benchmark-datasets/1"})
            with urllib.request.urlopen(request, timeout=30) as response:
                if not response.geturl().startswith("https://"):
                    raise ValueError("Dataset download redirected away from HTTPS")
                while True:
                    block = response.read(1024 * 1024)
                    if time.monotonic() - started > 300:
                        raise ValueError("Dataset download exceeded 300 seconds")
                    if not block:
                        break
                    size += len(block)
                    if size > source["bytes"]:
                        raise ValueError("Dataset download exceeds locked size")
                    digest.update(block)
                    out.write(block)
            if size != source["bytes"] or digest.hexdigest() != source["sha256"]:
                raise ValueError("Dataset download does not match locked size/SHA-256")
        except BaseException:
            partial.unlink()
            raise
    try:
        os.link(partial, path)  # Atomic publish without overwriting existing data.
    finally:
        partial.unlink()
    return path


def extract_key(line, kind):
    """Return raw key bytes, or None for metadata and other record types."""
    line = line.removesuffix(b"\n").removesuffix(b"\r")
    if kind == "lines":
        return line
    fields = line.split(b"\t")
    if kind == "cluster_urls":
        if not line.startswith(b"\t\t") or line.startswith(b"\t\t<Tm>"):
            return None
        if len(fields) != 6 or not fields[3].isdigit() or fields[4] not in (b"B", b"M"):
            raise ValueError("Malformed MemeTracker URL record")
        return fields[5]
    if kind == "cluster_phrases":
        if not line.startswith(b"\t") or line.startswith((b"\t\t", b"\t<QtFq>")):
            return None
        if len(fields) != 5 or not all(fields[i].isdigit() for i in (1, 2, 4)):
            raise ValueError("Malformed MemeTracker phrase record")
        return fields[3]
    raise ValueError(f"Unknown input format: {kind}")


class KeySample:
    """Bottom-k keyed hashes: stable unique-key sample, independent of row order."""
    def __init__(self, count, seed):
        self.count = count
        self.salt = hashlib.sha256(str(seed).encode("ascii")).digest()
        self.heap = []
        self.members = set()
        self.records = 0
        self.rejected = {"empty": 0, "nul": 0, "overlength": 0}

    def add(self, key):
        self.records += 1
        reason = "empty" if not key else "nul" if b"\0" in key else "overlength" if len(key) > 4096 else None
        if reason:
            self.rejected[reason] += 1
            return
        if key in self.members:
            return
        # Full bytes break the astronomically unlikely digest tie consistently.
        rank = int.from_bytes(hashlib.blake2b(key, key=self.salt, digest_size=16).digest(), "big")
        item = (-rank, key)
        if len(self.heap) < self.count:
            heapq.heappush(self.heap, item)
            self.members.add(key)
        elif item > self.heap[0]:
            removed = heapq.heapreplace(self.heap, item)
            self.members.remove(removed[1])
            self.members.add(key)

    def keys(self):
        return sorted(self.members)


def describe_keys(keys):
    """Lengths and adjacent longest common prefix, all measured in bytes."""
    lengths = sorted(map(len, keys))
    prefixes = []
    for left, right in zip(keys, keys[1:]):
        common = 0
        for a, b in zip(left, right):
            if a != b:
                break
            common += 1
        prefixes.append(common)

    def stats(values):
        values = sorted(values)
        return {"min": values[0], "median": values[(len(values) - 1) // 2],
                "p95": values[(95 * len(values) + 99) // 100 - 1],
                "max": values[-1], "mean": sum(values) / len(values)}

    return {"keys": len(keys), "key_bytes": stats(lengths),
            "adjacent_lcp_bytes": stats(prefixes),
            "non_ascii_keys": sum(any(byte >= 128 for byte in key) for key in keys)}


def scan_source(path, compression, samplers):
    opener = gzip.open if compression == "gzip" else open
    expanded = lines = 0
    with opener(path, "rb") as source:
        while True:
            line = source.readline(MAX_LINE_BYTES + 1)
            if not line:
                break
            expanded += len(line)
            lines += 1
            if len(line) > MAX_LINE_BYTES or expanded > MAX_EXPANDED_BYTES:
                raise ValueError("Source exceeds line (64 KiB) or expanded size (4 GiB) limit")
            for kind, sample in samplers:
                key = extract_key(line, kind)
                if key is not None:
                    sample.add(key)
    return {"source_lines": lines, "expanded_bytes": expanded, "scan": "complete source file"}


def prepare(names, count, seed, raw_dir, output_dir, offline=False):
    if not 200 <= count <= 100000:
        raise ValueError("count must be in [200, 100000]")
    catalog = json.loads((ROOT / "datasets.lock.json").read_text())
    outputs = {name: output_dir / f"{name}_{count}_s{seed}_string" for name in names}
    for path in outputs.values():
        if path.exists() or Path(str(path) + ".json").exists():
            raise FileExistsError(f"Refusing to overwrite dataset or metadata: {path}")
    reports = []
    for source_id, source in catalog["sources"].items():
        selected = [name for name in names if catalog["datasets"][name]["source"] == source_id]
        if not selected:
            continue
        raw = fetch(source, raw_dir, offline)
        print(f"Verified source: {raw}", flush=True)
        samples = {name: KeySample(count, seed) for name in selected}
        scan = scan_source(raw, source["compression"],
                           [(catalog["datasets"][name]["format"], samples[name]) for name in selected])
        for name, sample in samples.items():
            keys = sample.keys()
            if len(keys) != count:
                raise ValueError(f"{name}: requested {count} unique keys, found only {len(keys)}")
            if 8 + sum(4 + len(key) for key in keys) > MAX_DATA_BYTES:
                raise ValueError("Selected dataset exceeds 128 MiB; reduce count")
            output = outputs[name]
            write_dataset(output, keys)
            report = {
                "schema_version": 1, "dataset": name, "kind": "real",
                "source": source, "format": catalog["datasets"][name]["format"],
                "sampling": "bottom-k BLAKE2b-128 keyed by SHA256(decimal seed); unique keys, whole source",
                "seed": seed, "requested_keys": count, "keys": len(keys),
                "candidate_records": sample.records, "rejected_records": sample.rejected,
                "scan": scan, "normalization": "remove LF/CRLF only; preserve original key bytes; sort/deduplicate",
                "dataset_sha256": sha256(output), "statistics": describe_keys(keys),
                "generator_sha256": sha256(Path(__file__)),
            }
            with Path(str(output) + ".json").open("x") as out:
                json.dump(report, out, indent=2)
                out.write("\n")
            reports.append(report)
            print(f"Prepared {name}: {len(keys)} keys -> {output}", flush=True)
    return reports


def main():
    catalog = json.loads((ROOT / "datasets.lock.json").read_text())
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--datasets", nargs="+", choices=list(catalog["datasets"]), default=list(catalog["datasets"]))
    parser.add_argument("--count", type=int, default=10000)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--raw-dir", type=Path, default=ROOT / "data/raw")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "data/real")
    parser.add_argument("--offline", action="store_true", help="Require verified cached source files")
    args = parser.parse_args()
    try:
        prepare(list(dict.fromkeys(args.datasets)), args.count, args.seed,
                args.raw_dir, args.output_dir, args.offline)
    except (OSError, ValueError, EOFError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
