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
from make_lookup_workload import generate, read_keys, fraction_count, ratio_argument, NOT_FOUND


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
