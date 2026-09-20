#!/usr/bin/env python3
"""Check generated records against the TLI wire format and declared workload."""
import hashlib
import json
from decimal import Decimal
from pathlib import Path
import struct
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from make_lookup_workload import (
    NOT_FOUND,
    fraction_count,
    generate,
    ratio_argument,
    read_keys,
    zipf_exponent_argument,
)


def write_keys(path, keys):
    with path.open("wb") as out:
        out.write(struct.pack("<Q", len(keys)))
        for key in keys:
            out.write(struct.pack("<I", len(key)) + key)


def read_operations(path):
    # Independent decoder of upstream util.h's Operation<string> serialization.
    with path.open("rb") as source:
        count, = struct.unpack("<Q", source.read(8))
        rows = []
        for _ in range(count):
            op, expected, length = struct.unpack("<BQI", source.read(13))
            key = source.read(length)
            hi_length, = struct.unpack("<I", source.read(4))
            hi_key = source.read(hi_length)
            if len(key) != length or len(hi_key) != hi_length:
                raise ValueError("Incomplete generated operation")
            rows.append((op, expected, key, hi_key))
        if source.read():
            raise ValueError("Trailing generated operations")
        return rows


class WorkloadTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name) / "fixture_string"
        self.keys = [f"https://example.org/item/{i:06d}".encode() for i in range(200)]
        write_keys(self.data, self.keys)

    def test_hit_miss_counts_and_reproducibility(self):
        for ratio in (0.0, 0.5, 1.0):
            report = generate(self.data, 101, ratio)
            output = Path(report["workload_file"])
            rows = read_operations(output)
            self.assertEqual(sum(row[1] == NOT_FOUND for row in rows), int(101 * ratio))
            for op, expected, key, hi in rows:
                self.assertEqual((op, hi), (0, b""))
                self.assertEqual(key not in self.keys, expected == NOT_FOUND)
            self.assertEqual(report["workload_sha256"], hashlib.sha256(output.read_bytes()).hexdigest())
            self.assertEqual(json.loads(Path(str(output) + ".json").read_text()), report)
            other = Path(self.temp.name) / f"copy{ratio}_string"
            write_keys(other, self.keys)
            repeat = generate(other, 101, ratio)
            self.assertEqual(output.read_bytes(), Path(repeat["workload_file"]).read_bytes())
            with self.assertRaises(FileExistsError):
                generate(self.data, 101, ratio)

    def test_hotspot_probability_extremes_and_trace(self):
        for probability in (0.0, 1.0):
            report = generate(self.data, 400, 0.5, "hotspot", 0.1, probability)
            self.assertEqual(report["hotset"]["source_count"], int(400 * probability))
            self.assertEqual(report["hotset"]["hit_source_count"], int(200 * probability))
            self.assertEqual(report["hotset"]["miss_source_count"], int(200 * probability))
        hit = generate(self.data, 1000, 0, "hotspot")
        mixed = generate(self.data, 1000, 0.5, "hotspot")
        a, b = read_operations(Path(hit["workload_file"])), read_operations(Path(mixed["workload_file"]))
        self.assertEqual(hit["hotset"]["source_count"], mixed["hotset"]["source_count"])
        for hit_row, mixed_row in zip(a, b):
            source_key, query_key = hit_row[2], mixed_row[2]
            self.assertTrue(query_key == source_key or query_key.startswith(source_key + b"~miss/"))
        self.assertGreater(hit["hotset"]["actual_source_ratio"], 0.85)
        self.assertLess(hit["hotset"]["actual_source_ratio"], 0.95)

    def test_zipf_wire_reproducibility_misses_and_skew(self):
        report = generate(self.data, 20000, 0.25, "zipf", seed=17, zipf_exponent=1.5)
        output = Path(report["workload_file"])
        rows = read_operations(output)
        self.assertEqual(sum(row[1] == NOT_FOUND for row in rows), 5000)
        source_counts = {key: 0 for key in self.keys}
        for op, expected, key, hi in rows:
            self.assertEqual((op, hi), (0, b""))
            source_key = key.split(b"~miss/", 1)[0] if expected == NOT_FOUND else key
            self.assertIn(source_key, source_counts)
            self.assertEqual(key not in self.keys, expected == NOT_FOUND)
            source_counts[source_key] += 1
        top_decile = sum(source_counts[key] for key in self.keys[:20]) / len(rows)
        self.assertGreater(top_decile, 0.75)
        self.assertIn("_zipf_ze1.5_rankbytes_s17", output.name)
        self.assertEqual(report["zipf"]["exponent"], 1.5)
        self.assertIn("byte-sorted", report["zipf"]["rank_definition"])

        other = Path(self.temp.name) / "zipf_copy_string"
        write_keys(other, self.keys)
        repeat = generate(other, 20000, 0.25, "zipf", seed=17, zipf_exponent=1.5)
        self.assertEqual(output.read_bytes(), Path(repeat["workload_file"]).read_bytes())

        hits = generate(self.data, 1000, 0, "zipf", seed=29, zipf_exponent=1.5)
        mixed = generate(self.data, 1000, 0.5, "zipf", seed=29, zipf_exponent=1.5)
        hit_rows = read_operations(Path(hits["workload_file"]))
        mixed_rows = read_operations(Path(mixed["workload_file"]))
        for hit_row, mixed_row in zip(hit_rows, mixed_rows):
            mixed_source = mixed_row[2].split(b"~miss/", 1)[0]
            self.assertEqual(hit_row[2], mixed_source)

    def test_zipf_zero_exponent_matches_uniform(self):
        uniform = generate(self.data, 2000, 0.3, "uniform", seed=31)
        other = Path(self.temp.name) / "zero_zipf_string"
        write_keys(other, self.keys)
        zero_zipf = generate(other, 2000, 0.3, "zipf", seed=31, zipf_exponent=0)
        self.assertEqual(
            Path(uniform["workload_file"]).read_bytes(),
            Path(zero_zipf["workload_file"]).read_bytes(),
        )
        self.assertNotIn("zipf", uniform)
        self.assertEqual(zero_zipf["zipf"]["exponent"], 0.0)

    def test_zipf_exponent_validation(self):
        import argparse
        for exponent in (-0.1, 3.1, float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                generate(self.data, 10, distribution="zipf", zipf_exponent=exponent)
            with self.assertRaises(argparse.ArgumentTypeError):
                zipf_exponent_argument(str(exponent))
        self.assertEqual(zipf_exponent_argument("3"), Decimal("3"))
        tiny = generate(
            self.data, 10, distribution="zipf", zipf_exponent=Decimal("1e-256")
        )
        self.assertIn("_zipf_ze1e-256_rankbytes_", Path(tiny["workload_file"]).name)

    def test_decimal_ratio_boundaries(self):
        report = generate(self.data, 100, 0.29, "hotspot", 0.29, 1)
        self.assertEqual(report["miss_count"], 29)
        self.assertEqual(report["hotset"]["hotset_key_count"], 58)
        rows = read_operations(Path(report["workload_file"]))
        self.assertEqual(sum(row[1] == NOT_FOUND for row in rows), 29)
        # A decimal infinitesimally below one must still round down, regardless
        # of the default Decimal precision or binary float conversion.
        nearly_one = Decimal("0." + "9" * 60)
        self.assertEqual(fraction_count(100, nearly_one), 99)
        report = generate(self.data, 100, nearly_one)
        self.assertEqual(report["miss_count"], 99)
        self.assertEqual(report["requested_miss_ratio_decimal"], str(nearly_one))

    def test_ratio_resource_limits(self):
        import argparse
        for value in ("1e-1000000000", "0." + "9" * 129):
            with self.assertRaises(argparse.ArgumentTypeError):
                ratio_argument(value)
            with self.assertRaisesRegex(ValueError, "precision limit"):
                fraction_count(100, Decimal(value))
            with self.assertRaisesRegex(ValueError, "precision limit"):
                generate(self.data, 100, Decimal(value))
        self.assertEqual(fraction_count(100, Decimal("1e-256")), 0)
        self.assertEqual(fraction_count(100, Decimal("0." + "9" * 128)), 99)

    def test_long_keys_and_existing_suffixes(self):
        keys = [b"a", b"a~miss/0000000000000000", b"x" * 4096]
        write_keys(self.data, keys)
        report = generate(self.data, 200, 1.0)
        for _, expected, key, _ in read_operations(Path(report["workload_file"])):
            self.assertEqual(expected, NOT_FOUND)
            self.assertNotIn(key, keys)
            self.assertLessEqual(len(key), 4096)
            self.assertNotIn(b"\0", key)

    def test_invalid_data_and_ratios(self):
        for raw in (b"", struct.pack("<Q", 1), struct.pack("<QI", 1, 4097)):
            self.data.write_bytes(raw)
            with self.assertRaises(ValueError):
                read_keys(self.data)
        for keys in ([b"a", b"a"], [b"z", b"a"], [b"a\0b"]):
            write_keys(self.data, keys)
            with self.assertRaises(ValueError):
                read_keys(self.data)
        write_keys(self.data, self.keys)
        for ratio in (-0.1, 1.1, float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                generate(self.data, 10, ratio)
        write_keys(self.data, [b"x" * 4096])
        with self.assertRaisesRegex(ValueError, "256 MiB"):
            generate(self.data, 1000000)


if __name__ == "__main__":
    unittest.main()
