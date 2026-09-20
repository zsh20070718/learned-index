#!/usr/bin/env python3
"""Generate reproducible TLI string point lookups with misses and hot-set access."""
import argparse
import bisect
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import random
import struct

NOT_FOUND = (1 << 64) - 1
MAX_KEY_BYTES = 4096
MAX_ITEMS = 1000000
MAX_DATA_BYTES = 128 * 1024 * 1024
MAX_WORKLOAD_BYTES = 256 * 1024 * 1024


def bounded_decimal(value):
    try:
        ratio = Decimal(str(value))
    except InvalidOperation as error:
        raise ValueError("Expected a decimal ratio") from error
    if not ratio.is_finite():
        raise ValueError("Ratios must be finite")
    parts = ratio.as_tuple()
    if len(parts.digits) > 128 or abs(parts.exponent) > 256:
        raise ValueError("Ratio precision limit: 128 digits and absolute exponent <= 256")
    return ratio


def ratio_argument(value):
    try:
        return bounded_decimal(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from error


def bounded_zipf_exponent(value):
    try:
        exponent = Decimal(str(value))
    except InvalidOperation as error:
        raise ValueError("zipf_exponent must be a decimal number") from error
    if not exponent.is_finite():
        raise ValueError("zipf_exponent must be finite")
    parts = exponent.as_tuple()
    if len(parts.digits) > 128 or abs(parts.exponent) > 256:
        raise ValueError("zipf_exponent precision limit: 128 digits and absolute exponent <= 256")
    if not 0 <= exponent <= 3:
        raise ValueError("zipf_exponent must be in [0, 3]")
    return exponent


def zipf_exponent_argument(value):
    try:
        return bounded_zipf_exponent(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from error


def fraction_count(total, ratio):
    # Integer division avoids both binary-float error and Decimal context
    # rounding near an integer boundary, even for long CLI decimal literals.
    numerator, denominator = bounded_decimal(ratio).as_integer_ratio()
    return total * numerator // denominator


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_keys(path):
    """Read the bounded little-endian TLI string format, rejecting bad input."""
    if path.stat().st_size > MAX_DATA_BYTES:
        raise ValueError("Dataset exceeds the 128 MiB local workload limit")
    with path.open("rb") as source:
        def read_exact(size):
            data = source.read(size)
            if len(data) != size:
                raise ValueError("Truncated string dataset")
            return data

        count, = struct.unpack("<Q", read_exact(8))
        if not 1 <= count <= MAX_ITEMS:
            raise ValueError("Dataset key count must be in [1, 1000000]")
        keys = []
        for _ in range(count):
            length, = struct.unpack("<I", read_exact(4))
            if not 1 <= length <= MAX_KEY_BYTES:
                raise ValueError("Dataset keys must have 1–4096 bytes")
            key = read_exact(length)
            if b"\0" in key or (keys and key <= keys[-1]):
                raise ValueError("Dataset keys must be NUL-free, sorted and unique")
            keys.append(key)
        if source.read(1):
            raise ValueError("Trailing data after the declared keys")
    return keys


def missing_key(source_key, key_set, rng):
    # Preserve the source prefix where possible; membership is checked against
    # the entire dataset, not only the keys selected for positive queries.
    for _ in range(32):
        suffix = b"~miss/" + f"{rng.getrandbits(64):016x}".encode("ascii")
        candidate = source_key[:MAX_KEY_BYTES - len(suffix)] + suffix
        if candidate not in key_set:
            return candidate
    raise ValueError("Unable to generate an absent key after 32 attempts")


def generate(data_path, operations, miss_ratio=0.0, distribution="uniform",
             hotset_fraction=0.1, hot_probability=0.9, seed=42,
             zipf_exponent=1.1):
    if not data_path.name.endswith("_string"):
        raise ValueError("Dataset filename must end in _string")
    if not 1 <= operations <= MAX_ITEMS:
        raise ValueError("operations must be in [1, 1000000]")
    miss_ratio = bounded_decimal(miss_ratio)
    hotset_fraction = bounded_decimal(hotset_fraction)
    hot_probability = bounded_decimal(hot_probability)
    zipf_exponent = bounded_zipf_exponent(zipf_exponent)
    for name, ratio in (("miss_ratio", miss_ratio), ("hot_probability", hot_probability)):
        if not 0 <= ratio <= 1:
            raise ValueError(f"{name} must be in [0, 1]")
    if not 0 < hotset_fraction < 1:
        raise ValueError("hotset_fraction must be in (0, 1)")
    if distribution not in ("uniform", "hotspot", "zipf"):
        raise ValueError("distribution must be uniform, hotspot or zipf")
    keys = read_keys(data_path)
    if distribution == "hotspot" and len(keys) < 2:
        raise ValueError("Hotspot distribution requires at least two keys")
    key_set = set(keys)
    hot_count = max(1, min(len(keys) - 1, fraction_count(len(keys), hotset_fraction)))
    negative_count = fraction_count(operations, miss_ratio)
    actual_miss_ratio = negative_count / operations
    max_query_bytes = max(map(len, keys))
    if negative_count:
        max_query_bytes = min(MAX_KEY_BYTES, max_query_bytes + 22)
    if 8 + operations * (17 + max_query_bytes) > MAX_WORKLOAD_BYTES:
        raise ValueError("Workload could exceed the 256 MiB local output limit; reduce operations")
    if distribution == "zipf":
        exponent_label = format(zipf_exponent.normalize(), "g").lower()
        settings = f"zipf_ze{exponent_label}_rankbytes_s{seed}"
    else:
        settings = f"{distribution}_s{seed}"
    if distribution == "hotspot":
        settings += f"_hf{hotset_fraction:g}_hp{hot_probability:g}"
    # The fractional nl field is informational; authoritative exact counts are
    # in the sidecar. TLI requires the rq/i filename fields for dispatch.
    output = data_path.with_name(
        f"{data_path.name}_ops_{operations}_0.000000rq_{actual_miss_ratio:.6f}nl_0.000000i_{settings}")
    sidecar = output.with_name(output.name + ".json")
    if output.exists() or sidecar.exists():
        raise FileExistsError(f"Refusing to overwrite workload or metadata: {output}")

    # Independent streams keep the source access trace fixed across miss ratios.
    source_rng = random.Random(seed)
    miss_rng = random.Random(seed + 1)
    negative_rng = random.Random(seed + 2)
    negatives = set(miss_rng.sample(range(operations), negative_count))
    cached_misses = {}
    unique_hits, unique_misses = set(), set()
    hot_sources = hit_hot_sources = miss_hot_sources = 0
    zipf_cdf = None
    if distribution == "zipf" and zipf_exponent:
        cumulative = 0.0
        zipf_cdf = []
        exponent = float(zipf_exponent)
        for rank in range(1, len(keys) + 1):
            cumulative += rank ** -exponent
            zipf_cdf.append(cumulative)
    with output.open("xb") as out:
        try:
            out.write(struct.pack("<Q", operations))
            for operation in range(operations):
                if distribution == "uniform" or (distribution == "zipf" and not zipf_exponent):
                    offset = source_rng.randrange(len(keys))
                elif distribution == "hotspot" and source_rng.random() < hot_probability:
                    offset = source_rng.randrange(hot_count)
                elif distribution == "hotspot":
                    offset = source_rng.randrange(hot_count, len(keys))
                else:
                    offset = bisect.bisect(zipf_cdf, source_rng.random() * zipf_cdf[-1])
                hot_sources += offset < hot_count
                if operation in negatives:
                    if offset not in cached_misses:
                        cached_misses[offset] = missing_key(keys[offset], key_set, negative_rng)
                    key, result = cached_misses[offset], NOT_FOUND
                    unique_misses.add(key)
                    miss_hot_sources += offset < hot_count
                else:
                    key, result = keys[offset], 0  # TLI uses 0 for a positive lookup.
                    unique_hits.add(key)
                    hit_hot_sources += offset < hot_count
                out.write(struct.pack("<BQI", 0, result, len(key)))
                out.write(key)
                out.write(struct.pack("<I", 0))  # Empty high endpoint for a point lookup.
        except BaseException:
            # This path was exclusively created by this invocation.
            output.unlink()
            raise

    metadata = {
        "schema_version": 1, "generator": "make_lookup_workload.py",
        "workload_file": str(output), "dataset_file": str(data_path),
        "dataset_sha256": sha256(data_path), "workload_sha256": sha256(output),
        "dataset_keys": len(keys), "operations": operations, "seed": seed,
        "lookup_count": operations, "range_count": 0, "insert_count": 0,
        "hit_count": operations - negative_count, "miss_count": negative_count,
        "requested_miss_ratio": float(miss_ratio), "actual_miss_ratio": actual_miss_ratio,
        "requested_miss_ratio_decimal": str(miss_ratio),
        "distribution": distribution, "unique_hit_keys": len(unique_hits),
        "unique_miss_keys": len(unique_misses),
        "miss_method": "cached per source key; suffix mutation checked absent from entire dataset",
        "hotset": {
            "definition": "first hotset_key_count keys in byte-sorted dataset",
            "hotset_key_count": hot_count,
            "requested_fraction": float(hotset_fraction),
            "requested_fraction_decimal": str(hotset_fraction),
            "actual_fraction": hot_count / len(keys),
            "requested_probability": float(hot_probability) if distribution == "hotspot" else None,
            "requested_probability_decimal": str(hot_probability) if distribution == "hotspot" else None,
            "source_count": hot_sources, "actual_source_ratio": hot_sources / operations,
            "hit_source_count": hit_hot_sources, "miss_source_count": miss_hot_sources,
        },
    }
    if distribution == "zipf":
        metadata["zipf"] = {
            "exponent": float(zipf_exponent),
            "exponent_decimal": str(zipf_exponent),
            "rank_definition": "1-based position in byte-sorted dataset order; rank 1 is the first key",
            "probability": "rank^-exponent, normalized over ranks 1..dataset_keys",
        }
    # Retain a completed workload if metadata creation fails; never overwrite it.
    with sidecar.open("x") as out:
        json.dump(metadata, out, indent=2)
        out.write("\n")
    return metadata


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("data", type=Path)
    parser.add_argument("operations", type=int)
    parser.add_argument("--miss-ratio", type=ratio_argument, default=Decimal("0"),
                        help="Fraction of point lookups that miss; count is rounded down")
    parser.add_argument("--distribution", choices=("uniform", "hotspot", "zipf"), default="uniform")
    parser.add_argument("--hotset-fraction", type=ratio_argument, default=Decimal("0.1"))
    parser.add_argument("--hot-probability", type=ratio_argument, default=Decimal("0.9"))
    parser.add_argument("--zipf-exponent", type=zipf_exponent_argument, default=Decimal("1.1"))
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()
    try:
        result = generate(args.data, args.operations, args.miss_ratio, args.distribution,
                          args.hotset_fraction, args.hot_probability, args.seed,
                          args.zipf_exponent)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    print(json.dumps(result))


if __name__ == "__main__":
    main()
