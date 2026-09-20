#!/usr/bin/env python3
"""Offline contracts for source verification, extraction and unbiased key sampling."""
import gzip
import hashlib
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from prepare_real_datasets import KeySample, extract_key, fetch, scan_source, describe_keys


class DatasetTest(unittest.TestCase):
    def test_fields_preserve_bytes(self):
        self.assertEqual(extract_key(b" AbC \r\n", "lines"), b" AbC ")
        self.assertEqual(extract_key(b"\t\t2008-08-01 00:00:00\t2\tB\thttp://x/a?b=1\r\n", "cluster_urls"), b"http://x/a?b=1")
        self.assertEqual(extract_key(b"\t2\t1\tquoted phrase\t45\r\n", "cluster_phrases"), b"quoted phrase")
        for kind in ("cluster_urls", "cluster_phrases"):
            self.assertIsNone(extract_key(b"2\t8\troot phrase\t1\n", kind))
            self.assertIsNone(extract_key(b"\t\t<Tm>\t<Fq>\t<UrlTy>\t<Url>\n", kind))
        with self.assertRaisesRegex(ValueError, "Malformed"):
            extract_key(b"\t\t2008\t2\tB\n", "cluster_urls")

    def test_sample_order_duplicates_seed_and_filtering(self):
        keys = [f"word-{i:06d}".encode() for i in range(10000)]
        a, b, c = KeySample(200, 42), KeySample(200, 42), KeySample(200, 7)
        for key in keys:
            a.add(key)
            c.add(key)
        for key in reversed(keys * 2):
            b.add(key)
        self.assertEqual(a.keys(), b.keys())
        self.assertNotEqual(a.keys(), c.keys())
        self.assertEqual(len(a.keys()), 200)
        # Independently compute the expected bottom-k without the streaming heap.
        salt = hashlib.sha256(b"42").digest()
        expected = sorted(sorted(keys, key=lambda k: hashlib.blake2b(k, key=salt, digest_size=16).digest())[:200])
        self.assertEqual(a.keys(), expected)
        self.assertTrue(any(key > b"word-009000" for key in a.keys()))
        for key in (b"", b"a\0b", b"x" * 4097):
            a.add(key)
        self.assertEqual(a.rejected, {"empty": 1, "nul": 1, "overlength": 1})
        self.assertEqual(a.keys(), expected)

    def test_cache_integrity_and_offline(self):
        with tempfile.TemporaryDirectory() as directory:
            raw = Path(directory)
            source = {"filename": "words.txt", "bytes": 4, "sha256": hashlib.sha256(b"abc\n").hexdigest()}
            with self.assertRaises(FileNotFoundError):
                fetch(source, raw, offline=True)
            (raw / "words.txt").write_bytes(b"abc\n")
            self.assertEqual(fetch(source, raw, offline=True), raw / "words.txt")
            (raw / "words.txt").write_bytes(b"def\n")
            with self.assertRaisesRegex(ValueError, "does not match"):
                fetch(source, raw, offline=True)

    def test_complete_gzip_and_bounds(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fixture.gz"
            with gzip.open(path, "wb") as out:
                out.write(b"one\ntwo\nthree\n")
            sample = KeySample(200, 42)
            report = scan_source(path, "gzip", [("lines", sample)])
            self.assertEqual(sample.keys(), [b"one", b"three", b"two"])
            self.assertEqual(report["source_lines"], 3)
            with patch("prepare_real_datasets.MAX_EXPANDED_BYTES", 3):
                with self.assertRaisesRegex(ValueError, "limit"):
                    scan_source(path, "gzip", [("lines", sample)])
            path.write_bytes(path.read_bytes()[:-4])
            with self.assertRaises(EOFError):
                scan_source(path, "gzip", [("lines", sample)])

    def test_download_checks_and_exclusive_partial(self):
        with tempfile.TemporaryDirectory() as directory:
            raw = Path(directory)
            source = {"filename": "words.txt", "bytes": 4, "url": "https://example.invalid/words.txt",
                      "sha256": hashlib.sha256(b"abc\n").hexdigest()}

            def response(data):
                stream = io.BytesIO(data)
                stream.geturl = lambda: source["url"]
                return stream

            for bad in (b"x", b"def\n", b"abc\nextra"):
                with patch("urllib.request.urlopen", return_value=response(bad)):
                    with self.assertRaises(ValueError):
                        fetch(source, raw)
                self.assertFalse((raw / "words.txt").exists())
                self.assertFalse((raw / "words.txt.part").exists())
            (raw / "words.txt.part").write_bytes(b"other process")
            with self.assertRaises(FileExistsError):
                fetch(source, raw)
            self.assertEqual((raw / "words.txt.part").read_bytes(), b"other process")
            (raw / "words.txt.part").unlink()
            with patch("urllib.request.urlopen", return_value=response(b"abc\n")):
                self.assertEqual(fetch(source, raw).read_bytes(), b"abc\n")
            self.assertFalse((raw / "words.txt.part").exists())

    def test_structure_statistics(self):
        report = describe_keys([b"ab", b"abcd", b"abce", b"z"])
        self.assertEqual(report["key_bytes"]["max"], 4)
        self.assertEqual(report["adjacent_lcp_bytes"]["mean"], 5 / 3)
        self.assertEqual(report["adjacent_lcp_bytes"]["max"], 3)


if __name__ == "__main__":
    unittest.main()
