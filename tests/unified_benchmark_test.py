#!/usr/bin/env python3
"""Focused checks for the unified Python benchmark boundary."""

import csv
import copy
import contextlib
import hashlib
import io
import json
import struct
import subprocess
import sys
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import benchmark  # noqa: E402
import benchmark_lib.dataset as dataset_module  # noqa: E402
from benchmark_lib.adapters import (  # noqa: E402
    RUN_SCHEMA,
    PARALLEL_RUN_SCHEMA,
    initial_build_identity,
    invoke_runner,
    load_manifest,
    parse_runner_result,
    _source_git_identity,
)
from benchmark_lib.dataset import (  # noqa: E402
    LOGICAL_DOMAIN,
    encode_uleb128,
    logical_hash,
    prepare_dataset,
)
from benchmark_lib.errors import BuildFailure, InputError, ResourceBudget, RunFailure  # noqa: E402
from benchmark_lib.interchange import (  # noqa: E402
    QUERIES_MAGIC,
    ROWS_MAGIC,
    write_queries,
    write_rows,
)
from benchmark_lib.readers import iter_keys  # noqa: E402
from benchmark_lib.reports import SUMMARY_FIELDS, scaling_summaries, summary_row, write_reports  # noqa: E402
from benchmark_lib.workload import (  # noqa: E402
    POINT_SCHEMA,
    SCHEMA,
    WorkloadConfig,
    generate_queries,
    load_workload,
)


def make_manifest(root: Path, adapter_id: str = "fixture") -> Path:
    root.mkdir()
    (root / "CMakeLists.txt").write_text(
        "cmake_minimum_required(VERSION 3.16)\n", encoding="utf-8"
    )
    manifest = {
        "contract": "string-index/v1",
        "id": adapter_id,
        "source_dir": ".",
        "cmake_target": "fixture_adapter",
        "factory_header": "fixture_adapter.h",
        "factory_symbol": "fixture::make_index",
    }
    path = root / "benchmark-adapter.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path


def runner_validation(rows: int = 2, queries: int = 3) -> dict:
    return {
        "descriptor_checked": True,
        "capability_domain_checked": True,
        "empty_build_checked": True,
        "represented_boundaries_checked": True,
        "absent_key_checked": True,
        "finite_domain_full": False,
        "rows_checked": rows,
        "queries_checked": queries,
    }


def runner_descriptor() -> dict:
    return {
        "implementation": "fixture",
        "implementation_version": "v1",
        "adapter_version": "adapter-v1",
        "adaptation_notes": "",
        "capabilities": {
            "keys": {
                "min_bytes": 0,
                "max_bytes": None,
                "fixed_bytes": None,
                "allows_nul": True,
                "allows_high_bit": True,
                "allows_prefix_pairs": True,
            },
            "max_value": (1 << 64) - 1,
            "bulk_load": "native",
            "find": "native",
            "insert": "unsupported",
            "update": "unsupported",
            "erase": "unsupported",
            "scan": "unsupported",
            "range_sum": "unsupported",
        },
    }


def passed_runner_payload(
    repeats: int = 2, operations: int = 3, latency_samples: int = 1, rows: int = 2
) -> dict:
    measured_latency = min(latency_samples, operations)
    samples = []
    latencies = []
    for repeat in range(repeats):
        samples.append(
            {
                "repeat": repeat,
                "build_ns": 4,
                "latency_build_ns": 5 if measured_latency else None,
                "lookup_ns": 10,
                "operations": operations,
                "throughput_ops_per_second": operations * 1.0e9 / 10,
            }
        )
        quotient, remainder = divmod(operations, measured_latency or 1)
        for sample in range(measured_latency):
            latencies.append(
                {
                    "repeat": repeat,
                    "sample": sample,
                    "query_index": sample * quotient + min(sample, remainder),
                    "latency_ns": 2,
                }
            )
    return {
        "schema": RUN_SCHEMA,
        "status": "passed",
        "stage": "complete",
        "reason": "",
        "descriptor": runner_descriptor(),
        "validation": runner_validation(rows, operations),
        "memory": {
            "index_owned_bytes": None,
            "adapter_owned_bytes": None,
            "retained_input_bytes": None,
            "accounting_notes": "fixture accounting",
        },
        "samples": samples,
        "latency_samples": latencies,
        "checksum": 1,
    }


def failed_runner_payload(status: str = "failed") -> dict:
    payload = passed_runner_payload()
    payload.update(
        {
            "status": status,
            "stage": "correctness" if status == "failed" else "interchange",
            "reason": "safe structured failure",
            "samples": [],
            "latency_samples": [],
            "checksum": 0,
        }
    )
    return payload


def parallel_runner_payload(threads: int = 2, operations: int = 3) -> dict:
    payload = passed_runner_payload(operations=operations)
    payload["schema"] = PARALLEL_RUN_SCHEMA
    payload["execution"] = {
        "threads": threads,
        "mode": "serial" if threads == 1 else "shared-index-read-only",
        "partition": "contiguous-balanced",
        "latency_mode": "separate-single-thread",
    }
    payload["descriptor"]["capabilities"]["concurrent_find"] = True
    payload["validation"].update({
        "concurrent_find_checked": threads > 1,
        "concurrent_queries_checked": operations if threads > 1 else 0,
    })
    quotient, remainder = divmod(operations, threads)
    for sample in payload["samples"]:
        sample["workers"] = [
            {"worker": worker, "operations": quotient + (worker < remainder),
             "lookup_ns": 8, "checksum": worker}
            for worker in range(threads)
        ]
    return payload


class ParallelBoundaryTests(unittest.TestCase):
    def test_v2_parser_accepts_serial_parallel_and_uneven_partitions(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "result.json"
            for threads in (1, 2, 3):
                with self.subTest(threads=threads):
                    payload = parallel_runner_payload(threads)
                    path.write_text(json.dumps(payload), encoding="utf-8")
                    self.assertEqual(parse_runner_result(path, 2, 3, 1, 2, 0, threads), payload)

    def test_v2_parser_rejects_fabricated_concurrent_evidence(self) -> None:
        cases = [
            ("wrong threads", lambda p: p["execution"].update(threads=3)),
            ("wrong mode", lambda p: p["execution"].update(mode="serial")),
            ("wrong latency", lambda p: p["execution"].update(latency_mode="under-load")),
            ("missing capability", lambda p: p["descriptor"]["capabilities"].update(concurrent_find=False)),
            ("capability type", lambda p: p["descriptor"]["capabilities"].update(concurrent_find=1)),
            ("unchecked", lambda p: p["validation"].update(concurrent_find_checked=False)),
            ("wrong checked count", lambda p: p["validation"].update(concurrent_queries_checked=2)),
            ("missing worker", lambda p: p["samples"][0]["workers"].pop()),
            ("duplicate worker", lambda p: p["samples"][0]["workers"][1].update(worker=0)),
            ("uneven partition", lambda p: p["samples"][0]["workers"][0].update(operations=1)),
            ("bool operations", lambda p: p["samples"][0]["workers"][1].update(operations=True)),
            ("negative time", lambda p: p["samples"][0]["workers"][0].update(lookup_ns=-1)),
            ("oversized time", lambda p: p["samples"][0]["workers"][0].update(lookup_ns=11)),
            ("bad checksum", lambda p: p["samples"][0]["workers"][0].update(checksum=1 << 64)),
        ]
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "result.json"
            for name, mutate in cases:
                with self.subTest(name=name):
                    payload = parallel_runner_payload()
                    mutate(payload)
                    path.write_text(json.dumps(payload), encoding="utf-8")
                    with self.assertRaises(RunFailure):
                        parse_runner_result(path, 2, 3, 1, 2, 0, 2)

    def test_v1_result_cannot_certify_multiple_workers(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "result.json"
            path.write_text(json.dumps(passed_runner_payload()), encoding="utf-8")
            with self.assertRaisesRegex(RunFailure, "v1.*parallel"):
                parse_runner_result(path, 2, 3, 1, 2, 0, 2)

    def test_non_string_status_is_a_classified_runner_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "result.json"
            for status in ([], {}, None, True, 1):
                with self.subTest(status=status):
                    payload = parallel_runner_payload()
                    payload["status"] = status
                    path.write_text(json.dumps(payload), encoding="utf-8")
                    with self.assertRaisesRegex(RunFailure, "invalid status"):
                        parse_runner_result(path, 2, 3, 1, 2, 0, 2)

    def test_failed_parallel_run_retains_safe_status_without_measurements(self) -> None:
        payload = parallel_runner_payload()
        payload.update(status="unsupported", stage="capability", reason="concurrent find unsupported",
                       samples=[], latency_samples=[], checksum=0)
        payload["descriptor"]["capabilities"]["concurrent_find"] = False
        payload["validation"].update(concurrent_find_checked=False, concurrent_queries_checked=0)
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "result.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            self.assertEqual(parse_runner_result(path, 2, 3, 1, 2, 0, 2), payload)
            payload["samples"] = parallel_runner_payload()["samples"]
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(RunFailure, "measurements"):
                parse_runner_result(path, 2, 3, 1, 2, 0, 2)

    def test_thread_matrix_builds_once_and_continues_after_run_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest = load_manifest(make_manifest(root / "adapter"))
            identity = initial_build_identity(manifest, ROOT)
            with mock.patch.object(benchmark, "build_runner", return_value=(root / "runner", identity)) as build, \
                 mock.patch.object(benchmark, "invoke_runner", side_effect=[
                     parallel_runner_payload(1), RunFailure("runner timed out"), parallel_runner_payload(3)
                 ]) as run:
                results = benchmark._run_implementation(
                    manifest, 1, root / "output", root / "rows", root / "queries",
                    2, 1, 30, 30, 1, [1, 2, 3],
                )
            build.assert_called_once()
            self.assertEqual([r["status"] for r in results], ["passed", "failed", "passed"])
            self.assertEqual([r["threads"] for r in results], [1, 2, 3])
            self.assertTrue(all(r["build_identity"] == identity for r in results))
            for n, call in enumerate(run.call_args_list, 1):
                self.assertEqual(call.args[1:3], (root / "rows", root / "queries"))
                self.assertEqual(call.kwargs["threads"], n)

    def test_build_failure_has_every_requested_thread_count(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest = load_manifest(make_manifest(root / "adapter"))
            with mock.patch.object(benchmark, "build_runner", side_effect=BuildFailure("unavailable")), \
                 mock.patch.object(benchmark, "invoke_runner") as run:
                results = benchmark._run_implementation(
                    manifest, 1, root / "output", root / "r", root / "q", 1, 1, 1, 1, 1, [1, 4]
                )
            self.assertEqual([(r["threads"], r["stage"]) for r in results], [(1, "build"), (4, "build")])
            run.assert_not_called()

    def test_thread_arguments_reject_duplicates_zero_and_excess(self) -> None:
        for counts in (["1", "1"], ["0"], ["257"]):
            with self.subTest(counts=counts), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    benchmark.main(["--dataset", "missing", "--implementation", "missing", "--threads", *counts])
                self.assertEqual(raised.exception.code, 2)

    def test_global_input_failure_preserves_sorted_thread_matrix(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest = make_manifest(root / "adapter")
            with contextlib.redirect_stderr(io.StringIO()):
                code = benchmark.main(["--dataset", str(root / "missing.txt"),
                    "--implementation", str(manifest), "--output", str(root / "out"), "--threads", "4", "1", "2"])
            self.assertEqual(code, 2)
            report = json.loads((root / "out/report.json").read_text())
            self.assertEqual(report["config"]["threads"], [1, 2, 4])
            self.assertEqual([r["threads"] for r in report["results"]], [1, 2, 4])
            self.assertTrue(all(r["status"] == "invalid_input" for r in report["results"]))
            self.assertTrue((root / "out/scaling.csv").is_file())

    def test_scaling_requires_passed_matching_single_thread_baseline(self) -> None:
        baseline = {"implementation_id": "index", "threads": 1, "status": "passed",
                    "runner_result": parallel_runner_payload(1)}
        parallel = {"implementation_id": "index", "threads": 2, "status": "passed",
                    "runner_result": parallel_runner_payload(2)}
        for sample in parallel["runner_result"]["samples"]:
            sample["throughput_ops_per_second"] *= 1.8
        summaries = scaling_summaries([parallel, baseline])
        self.assertAlmostEqual(summaries[0]["speedup_vs_1_thread"], 1.8)
        self.assertAlmostEqual(summaries[0]["parallel_efficiency"], .9)
        self.assertEqual(summaries[1]["speedup_vs_1_thread"], 1)
        self.assertEqual(scaling_summaries([parallel])[0]["speedup_vs_1_thread"], "")
        failed_baseline = dict(baseline, status="failed")
        self.assertEqual(scaling_summaries([parallel, failed_baseline])[0]["speedup_vs_1_thread"], "")
        other_budget = copy.deepcopy(baseline)
        for sample in other_budget["runner_result"]["samples"]:
            sample["operations"] = 4
        self.assertEqual(scaling_summaries([parallel, other_budget])[0]["speedup_vs_1_thread"], "")

    def test_reports_keep_worker_evidence_and_label_serial_latency(self) -> None:
        result = {"implementation_id": "index", "threads": 2, "status": "passed",
                  "runner_result": parallel_runner_payload()}
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write_reports(root, {"threads": [2]}, {}, {}, [result])
            report = json.loads((root / "report.json").read_text())
            self.assertEqual(report["schema"], "string-index-benchmark-report/v2")
            self.assertEqual(report["scaling"][0]["speedup_vs_1_thread"], "")
            with (root / "workers.csv").open(newline="") as stream:
                workers = list(csv.DictReader(stream))
            self.assertEqual(len(workers), 4)
            self.assertEqual([int(w["operations"]) for w in workers], [2, 1, 2, 1])
            self.assertIn("独立单线程 P99", (root / "report.md").read_text())


class ReaderTests(unittest.TestCase):
    def test_raw_lines_preserve_byte_keys(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "keys.txt"
            long_key = b"z" * 10000
            path.write_bytes(b"\nA\x00B\n\x80\xff\npre\nprefix\n" + long_key + b"\npre\n")
            prepared = prepare_dataset(path, "raw-lines", None, 4 * 1024 * 1024)
            self.assertIn(b"", prepared.keys)
            self.assertIn(b"A\x00B", prepared.keys)
            self.assertIn(b"\x80\xff", prepared.keys)
            self.assertIn(long_key, prepared.keys)
            self.assertEqual(prepared.source_records, 7)
            self.assertEqual(prepared.duplicate_records, 1)
            self.assertEqual(prepared.keys, sorted(set(prepared.keys)))
            self.assertGreaterEqual(prepared.prefix_adjacent_pairs, 1)

    def test_raw_lines_handle_crlf_and_reject_key_option(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "keys.txt"
            path.write_bytes(b"a\r\n\r\n")
            self.assertEqual(list(iter_keys(path, "raw-lines")), [b"a", b""])
            with self.assertRaisesRegex(InputError, "--key"):
                list(iter_keys(path, "raw-lines", "field"))

    def test_jsonl_requires_explicit_string_and_rejects_surrogate(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "keys.jsonl"
            path.write_text('{"key":"","other":1}\n{"key":"é"}\n', encoding="utf-8")
            self.assertEqual(list(iter_keys(path, "jsonl", "key")), [b"", "é".encode()])
            with self.assertRaisesRegex(InputError, "explicit --key"):
                list(iter_keys(path, "jsonl"))
            path.write_text('{"key":"\\ud800"}\n', encoding="utf-8")
            with self.assertRaisesRegex(InputError, "not valid Unicode"):
                list(iter_keys(path, "jsonl", "key"))

    def test_jsonl_rejects_duplicate_members_and_non_string(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "keys.jsonl"
            path.write_text('{"key":"a","key":"b"}\n', encoding="utf-8")
            with self.assertRaisesRegex(InputError, "malformed"):
                list(iter_keys(path, "jsonl", "key"))
            path.write_text('{"key":3}\n', encoding="utf-8")
            with self.assertRaisesRegex(InputError, "not a string"):
                list(iter_keys(path, "jsonl", "key"))

    def test_legacy_tli_is_strict(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "keys.tli"
            path.write_bytes(
                struct.pack("<Q", 3)
                + struct.pack("<I", 0)
                + struct.pack("<I", 3)
                + b"a\x00b"
                + struct.pack("<I", 2)
                + b"\x80x"
            )
            self.assertEqual(list(iter_keys(path, "legacy-tli")), [b"", b"a\x00b", b"\x80x"])
            path.write_bytes(path.read_bytes() + b"x")
            with self.assertRaisesRegex(InputError, "trailing"):
                list(iter_keys(path, "legacy-tli"))

    def test_legacy_tli_rejects_truncation_and_impossible_count(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "keys.tli"
            path.write_bytes(struct.pack("<Q", 1) + struct.pack("<I", 5) + b"ab")
            with self.assertRaisesRegex(InputError, "truncated"):
                list(iter_keys(path, "legacy-tli"))
            path.write_bytes(struct.pack("<Q", 100))
            with self.assertRaisesRegex(InputError, "record count"):
                list(iter_keys(path, "legacy-tli"))


class DatasetAndWorkloadTests(unittest.TestCase):
    def test_logical_hash_uses_declared_canonical_stream(self) -> None:
        keys = [b"", b"a", b"a\x00", b"\xff"]
        stream = LOGICAL_DOMAIN + encode_uleb128(len(keys))
        for key in keys:
            stream += encode_uleb128(len(key)) + key
        self.assertEqual(logical_hash(keys), hashlib.sha256(stream).hexdigest())

    def test_source_mutation_during_provenance_pass_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "keys.txt"
            path.write_bytes(b"a\n")
            real_hash = dataset_module.file_hash

            def mutating_hash(target: Path) -> str:
                digest = real_hash(target)
                target.write_bytes(b"changed\n")
                return digest

            with mock.patch.object(dataset_module, "file_hash", side_effect=mutating_hash):
                with self.assertRaisesRegex(InputError, "changed"):
                    prepare_dataset(path, "raw-lines", None, 1024 * 1024)

    def test_resource_budget_has_its_own_exception(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "keys.txt"
            path.write_bytes(b"x" * 100)
            with self.assertRaises(ResourceBudget):
                prepare_dataset(path, "raw-lines", None, 10)

    def test_workload_has_exact_misses_and_is_deterministic(self) -> None:
        config = WorkloadConfig(
            seed=7,
            operations=37,
            miss_ratio=Fraction(10, 37),
            repeats=2,
            latency_samples=5,
        )
        keys = [b"alpha", b"beta", b"gamma"]
        first, misses = generate_queries(keys, config, 1024 * 1024)
        second, second_misses = generate_queries(keys, config, 1024 * 1024)
        self.assertEqual(first, second)
        self.assertEqual((misses, second_misses), (10, 10))
        self.assertEqual(sum(not present for _, present, _ in first), 10)
        for key, present, _ in first:
            if not present:
                self.assertTrue(key.isascii())
                self.assertNotIn(b"\x00", key)
                self.assertTrue(any(key.startswith(source) for source in keys))

    def test_long_key_misses_are_budgeted_before_repeated_copying(self) -> None:
        config = WorkloadConfig(
            seed=9,
            operations=100,
            miss_ratio=Fraction(1, 1),
            repeats=1,
            latency_samples=0,
        )
        with self.assertRaisesRegex(ResourceBudget, "workload generation"):
            generate_queries([b"x" * 200000], config, 512 * 1024)

    def test_workload_rejects_pathological_decimal(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "workload.json"
            path.write_text(
                json.dumps({"schema": SCHEMA, "miss_ratio": "0." + "1" * 200}),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(InputError, "precision"):
                load_workload(path)

    def test_numeric_and_string_miss_ratios_keep_exact_decimal_spelling(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            decimal = "0.28999999999999999999999999999"
            numeric_path = root / "numeric.json"
            string_path = root / "string.json"
            numeric_path.write_text(
                '{"schema":"' + SCHEMA + '","operations":100,"miss_ratio":' + decimal + "}",
                encoding="utf-8",
            )
            string_path.write_text(
                json.dumps(
                    {"schema": SCHEMA, "operations": 100, "miss_ratio": decimal}
                ),
                encoding="utf-8",
            )
            numeric = load_workload(numeric_path)
            string = load_workload(string_path)
            expected = Fraction(decimal)
            self.assertEqual(numeric.miss_ratio, expected)
            self.assertEqual(string.miss_ratio, expected)
            self.assertEqual(
                (100 * numeric.miss_ratio.numerator) // numeric.miss_ratio.denominator,
                28,
            )

    def test_interchange_bytes_match_frozen_layout(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            rows = Path(temp) / "rows.bin"
            queries = Path(temp) / "queries.bin"
            write_rows(rows, [b"", b"a"])
            write_queries(queries, [(b"", True, 0), (b"z", False, 0)])
            expected_rows = (
                ROWS_MAGIC
                + struct.pack("<Q", 2)
                + struct.pack("<Q", 0)
                + struct.pack("<Q", 0)
                + struct.pack("<Q", 1)
                + b"a"
                + struct.pack("<Q", 1)
            )
            expected_queries = (
                QUERIES_MAGIC
                + struct.pack("<Q", 2)
                + struct.pack("<BQQ", 1, 0, 0)
                + struct.pack("<BQQ", 0, 0, 1)
                + b"z"
            )
            self.assertEqual(rows.read_bytes(), expected_rows)
            self.assertEqual(queries.read_bytes(), expected_queries)


class SkewWorkloadTests(unittest.TestCase):
    def _load(self, settings: dict) -> WorkloadConfig:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "workload.json"
            path.write_text(json.dumps(settings), encoding="utf-8")
            return load_workload(path)

    def test_v1_query_bytes_keep_the_pre_extension_fingerprint(self) -> None:
        config = WorkloadConfig(seed=7, operations=37, miss_ratio=Fraction(10, 37))
        queries, misses = generate_queries([b"alpha", b"beta", b"gamma"], config, 1024 * 1024)
        with tempfile.TemporaryDirectory() as temp:
            digest = write_queries(Path(temp) / "queries.bin", queries)
        self.assertEqual(misses, 10)
        self.assertEqual(digest, "df0fdedd3985420eb147af79e69311fbd20713e2492309c02dd4a29ae06780c2")
        self.assertNotIn("distribution", config.metadata(len(queries), misses))

    def test_v2_uniform_and_zero_zipf_reuse_v1_queries(self) -> None:
        keys = [b"", b"a\x00", b"prefix", b"prefix-more", b"\xff"]
        legacy = WorkloadConfig(seed=123, operations=57, miss_ratio=Fraction(1, 3))
        expected = generate_queries(keys, legacy, 1024 * 1024)
        for distribution in ("uniform", "zipf"):
            with self.subTest(distribution=distribution):
                config = WorkloadConfig(
                    seed=123, operations=57, miss_ratio=Fraction(1, 3),
                    schema=POINT_SCHEMA, distribution=distribution, zipf_exponent=Fraction(0),
                )
                self.assertEqual(generate_queries(keys, config, 1024 * 1024), expected)

    def test_examples_load_with_matching_parameters(self) -> None:
        for distribution in ("uniform", "hotspot", "zipf"):
            with self.subTest(distribution=distribution):
                config = load_workload(ROOT / "examples/workloads" / (distribution + ".json"))
                self.assertEqual(config.schema, POINT_SCHEMA)
                self.assertEqual(config.distribution, distribution)
                self.assertEqual(config.operations, 10000)
                self.assertEqual(config.miss_ratio, Fraction(1, 10))

    def test_schema_and_distribution_reject_invalid_or_unused_settings(self) -> None:
        cases = [
            {"schema": SCHEMA, "distribution": "uniform"},
            {"distribution": "hotspot"},
            {"schema": "unknown"},
            {"schema": POINT_SCHEMA, "distribution": []},
            {"schema": POINT_SCHEMA, "distribution": "unknown"},
            {"schema": POINT_SCHEMA, "distribution": "uniform", "zipf_exponent": 1},
            {"schema": POINT_SCHEMA, "distribution": "zipf", "hot_probability": "0.9"},
            {"schema": POINT_SCHEMA, "distribution": "hotspot", "hotset_fraction": 0},
            {"schema": POINT_SCHEMA, "distribution": "hotspot", "hotset_fraction": 1},
            {"schema": POINT_SCHEMA, "distribution": "hotspot", "hot_probability": True},
            {"schema": POINT_SCHEMA, "distribution": "hotspot", "hot_probability": -1},
            {"schema": POINT_SCHEMA, "distribution": "zipf", "zipf_exponent": 4},
            {"schema": POINT_SCHEMA, "distribution": "zipf", "zipf_exponent": "NaN"},
            {"schema": POINT_SCHEMA, "distribution": "zipf", "zipf_exponent": "0." + "1" * 100},
        ]
        for settings in cases:
            with self.subTest(settings=settings):
                with self.assertRaises(InputError):
                    self._load(settings)

    def test_hotset_rounding_keeps_exact_decimal_and_nonempty_regions(self) -> None:
        config = self._load({
            "schema": POINT_SCHEMA, "distribution": "hotspot", "operations": 100,
            "hotset_fraction": "0.28999999999999999999999999999", "miss_ratio": "0",
        })
        keys = [str(index).encode() for index in range(100)]
        queries, misses = generate_queries(keys, config, 1024 * 1024)
        observed = config.metadata(len(queries), misses, queries, len(keys))["observed"]
        self.assertEqual(observed["hotset_key_count"], 28)
        for fraction in (Fraction(1, 1000), Fraction(999, 1000)):
            config = WorkloadConfig(
                schema=POINT_SCHEMA, distribution="hotspot", operations=20,
                hotset_fraction=fraction,
            )
            queries, misses = generate_queries([b"a", b"b"], config, 1024 * 1024)
            self.assertEqual(config.metadata(20, misses, queries, 2)["observed"]["hotset_key_count"], 1)

    def test_hot_probability_endpoints_select_only_the_requested_region(self) -> None:
        keys = [str(index).encode() for index in range(100)]
        for probability in (Fraction(0), Fraction(1)):
            with self.subTest(probability=probability):
                config = WorkloadConfig(
                    schema=POINT_SCHEMA, distribution="hotspot", operations=1000,
                    miss_ratio=Fraction(0), hot_probability=probability,
                )
                queries, _misses = generate_queries(keys, config, 1024 * 1024)
                self.assertTrue(all((value < 10) == bool(probability) for _, _, value in queries))

    def test_hotspot_concentrates_hits_and_metadata_matches_the_queries(self) -> None:
        keys = [str(index).encode() for index in range(100)]
        config = WorkloadConfig(
            schema=POINT_SCHEMA, distribution="hotspot", operations=20000,
            miss_ratio=Fraction(1, 2),
        )
        queries, misses = generate_queries(keys, config, 8 * 1024 * 1024)
        observed = config.metadata(len(queries), misses, queries, len(keys))["observed"]
        hit_values = [value for _key, present, value in queries if present]
        self.assertEqual(observed["unique_hit_keys"], len(set(hit_values)))
        self.assertEqual(observed["hotset_hit_queries"], sum(value < 10 for value in hit_values))
        self.assertEqual((observed["hit_queries"], observed["miss_queries"]), (10000, 10000))
        self.assertGreater(observed["hotset_hit_fraction"], 0.88)
        self.assertLess(observed["hotset_hit_fraction"], 0.92)

    def test_zipf_frequency_agrees_with_independent_finite_weight_sum(self) -> None:
        keys = [str(index).encode() for index in range(100)]
        for exponent in (Fraction(1), Fraction(3)):
            with self.subTest(exponent=exponent):
                config = WorkloadConfig(
                    schema=POINT_SCHEMA, distribution="zipf", operations=30000,
                    miss_ratio=Fraction(0), zipf_exponent=exponent,
                )
                queries, _misses = generate_queries(keys, config, 8 * 1024 * 1024)
                observed = sum(value == 0 for _key, _present, value in queries) / len(queries)
                expected = 1 / sum(rank ** -float(exponent) for rank in range(1, 101))
                self.assertAlmostEqual(observed, expected, delta=0.02)

    def test_all_distributions_keep_sources_when_miss_ratio_changes(self) -> None:
        keys = ["key{:03d}".format(index).encode() for index in range(50)]
        for distribution in ("uniform", "hotspot", "zipf"):
            baseline = None
            for ratio in (Fraction(0), Fraction(1, 3), Fraction(1)):
                with self.subTest(distribution=distribution, ratio=ratio):
                    config = WorkloadConfig(
                        schema=POINT_SCHEMA, distribution=distribution, operations=300,
                        miss_ratio=ratio,
                    )
                    queries, misses = generate_queries(keys, config, 1024 * 1024)
                    self.assertEqual(generate_queries(keys, config, 1024 * 1024), (queries, misses))
                    self.assertEqual(misses, (300 * ratio.numerator) // ratio.denominator)
                    sources = [key.split(b"~sib-miss-")[0] for key, _, _ in queries]
                    if baseline is None:
                        baseline = sources
                    self.assertEqual(sources, baseline)
                    for key, present, value in queries:
                        self.assertEqual(key in keys, present)
                        if present:
                            self.assertEqual(key, keys[value])

    def test_zipf_cdf_budget_is_checked_before_allocation(self) -> None:
        keys = [str(index).encode() for index in range(1000)]
        config = WorkloadConfig(
            schema=POINT_SCHEMA, distribution="zipf", operations=1, miss_ratio=Fraction(0),
        )
        required = 192 + len(keys) * (64 + 40)
        with mock.patch("benchmark_lib.workload.random.Random") as random_factory:
            with self.assertRaises(ResourceBudget):
                generate_queries(keys, config, required - 1)
            random_factory.assert_not_called()
        self.assertEqual(len(generate_queries(keys, config, required)[0]), 1)

    def test_single_key_hotspot_precondition_precedes_workload_budget(self) -> None:
        config = WorkloadConfig(schema=POINT_SCHEMA, distribution="hotspot")
        for budget in (0, 1000000, 3000000):
            with self.subTest(budget=budget), mock.patch("benchmark_lib.workload.random.Random") as rng:
                with self.assertRaisesRegex(InputError, "at least two unique keys"):
                    generate_queries([b"a"], config, budget)
                rng.assert_not_called()

    def test_zero_hits_reports_unmeasured_share_instead_of_zero(self) -> None:
        for distribution in ("hotspot", "zipf"):
            with self.subTest(distribution=distribution), tempfile.TemporaryDirectory() as temp:
                config = WorkloadConfig(
                    schema=POINT_SCHEMA, distribution=distribution, operations=20,
                    miss_ratio=Fraction(1),
                )
                queries, misses = generate_queries([b"a", b"b"], config, 1024 * 1024)
                metadata = config.metadata(len(queries), misses, queries, 2)
                observed = metadata["observed"]
                name = "hotset_hit_fraction" if distribution == "hotspot" else "rank_one_hit_fraction"
                self.assertIsNone(observed[name])
                self.assertEqual(observed["unique_hit_keys"], 0)
                write_reports(Path(temp), {}, {}, metadata, [])
                report = (Path(temp) / "report.md").read_text(encoding="utf-8")
                self.assertIn("未测量（没有命中查询）", report)
                self.assertIn("合成访问模型", report)

    def test_single_unique_key_hotspot_never_reaches_adapter_build(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            dataset = root / "keys.txt"
            dataset.write_bytes(b"a\na\n")
            for budget in (1, 4):
                with self.subTest(budget_mib=budget):
                    output = root / ("out-" + str(budget))
                    with mock.patch.object(benchmark, "_run_implementation") as run:
                        status = benchmark.main([
                            "--dataset", str(dataset), "--implementation", str(ROOT / "adapters/std_map"),
                            "--workload", str(ROOT / "examples/workloads/hotspot.json"), "--output", str(output),
                            "--memory-budget-mib", str(budget),
                        ])
                        run.assert_not_called()
                    self.assertEqual(status, 2)
                    report = json.loads((output / "report.json").read_text(encoding="utf-8"))
                    self.assertEqual(report["results"][0]["status"], "invalid_input")
                    self.assertEqual(report["results"][0]["stage"], "workload")
                    self.assertEqual(report["workload"]["schema"], POINT_SCHEMA)
                    self.assertEqual(report["workload"]["distribution"], "hotspot")
                    self.assertIsNone(report["workload"]["actual_operations"])
                    self.assertIsNone(report["workload"]["observed"])
                    self.assertFalse((output / "builds").exists())


class AdapterAndReportingTests(unittest.TestCase):
    def test_source_git_identity_records_clean_and_modified_tracked_sources(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            source = root / "model.h"
            source.write_text("original\n")
            subprocess.run(["git", "-C", str(root), "add", "model.h"], check=True)
            subprocess.run(["git", "-c", "user.name=Benchmark Test", "-c", "user.email=test@example.invalid",
                            "-c", "commit.gpgsign=false", "-C", str(root), "commit", "-qm", "fixture"], check=True)
            clean = _source_git_identity(root)
            self.assertRegex(clean["commit"], r"^[0-9a-f]{40,64}$")
            self.assertIs(clean["tracked_dirty"], False)
            (root / "untracked").write_text("not included\n")
            self.assertIs(_source_git_identity(root)["tracked_dirty"], False)
            source.write_text("modified\n")
            dirty = _source_git_identity(root)
            self.assertEqual(dirty["commit"], clean["commit"])
            self.assertIs(dirty["tracked_dirty"], True)

    def test_source_git_identity_is_explicitly_unavailable_without_git(self) -> None:
        with mock.patch("benchmark_lib.adapters.shutil.which", return_value=None):
            identity = _source_git_identity(Path("missing"))
        self.assertIsNone(identity["commit"])
        self.assertIsNone(identity["tracked_dirty"])

    def test_manifest_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            manifest_path = make_manifest(Path(temp) / "adapter")
            manifest = load_manifest(manifest_path.parent)
            self.assertEqual(manifest.adapter_id, "fixture")
            raw = json.loads(manifest_path.read_text(encoding="utf-8"))
            raw["factory_symbol"] = "bad-symbol"
            manifest_path.write_text(json.dumps(raw), encoding="utf-8")
            with self.assertRaisesRegex(InputError, "factory_symbol"):
                load_manifest(manifest_path)

            raw["factory_symbol"] = "fixture::make_index"
            raw["source_dir"] = "private-key\u0000source"
            manifest_path.write_text(json.dumps(raw), encoding="utf-8")
            with self.assertRaisesRegex(InputError, "source_dir") as raised:
                load_manifest(manifest_path)
            self.assertNotIn("private-key", str(raised.exception))

    def test_initial_build_identity_has_stable_bounded_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            manifest_path = make_manifest(Path(temp) / "adapter")
            manifest = load_manifest(manifest_path)
            identity = initial_build_identity(manifest, ROOT)
            self.assertEqual(identity["schema"], "string-index-build-identity/v1")
            self.assertEqual(
                identity["adapter_manifest_sha256"],
                hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
            )
            self.assertEqual(
                identity["runner_harness_sha256"],
                hashlib.sha256((ROOT / "src/unified/runner.cpp").read_bytes()).hexdigest(),
            )
            self.assertIsNone(identity["external_source_tree_sha256"])
            self.assertIn("not recursively hashed", identity["external_source_identity_note"])
            self.assertEqual(identity["configuration"]["cxx_standard"], "17")

    def test_runner_parser_preserves_invalid_input(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "result.json"
            payload = failed_runner_payload("invalid_input")
            payload["stage"] = "descriptor"
            payload["reason"] = "invalid capability descriptor"
            path.write_text(json.dumps(payload), encoding="utf-8")
            self.assertEqual(parse_runner_result(path), payload)
            payload["status"] = "made-up"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(RunFailure, "invalid status"):
                parse_runner_result(path)

    def test_runner_parser_rejects_malformed_or_fake_passed_results(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "result.json"
            payload = passed_runner_payload()
            path.write_text(json.dumps(payload), encoding="utf-8")
            self.assertEqual(
                parse_runner_result(path, 2, 3, 1, 2, 0), payload
            )

            duplicate = json.dumps(payload).replace(
                '{"schema":', '{"schema":"shadow","schema":', 1
            )
            path.write_text(duplicate, encoding="utf-8")
            with self.assertRaisesRegex(RunFailure, "malformed"):
                parse_runner_result(path, 2, 3, 1, 2, 0)

            cases = []
            non_finite = json.loads(json.dumps(payload))
            non_finite["samples"][0]["throughput_ops_per_second"] = float("nan")
            cases.append(("non-finite", non_finite))
            malformed_nested = json.loads(json.dumps(payload))
            del malformed_nested["descriptor"]["capabilities"]["keys"]["allows_nul"]
            cases.append(("nested schema", malformed_nested))
            fake_metric = json.loads(json.dumps(payload))
            fake_metric["samples"][0]["throughput_ops_per_second"] = 1
            cases.append(("fake throughput", fake_metric))
            wrong_repeat = json.loads(json.dumps(payload))
            wrong_repeat["samples"].pop()
            cases.append(("repeat count", wrong_repeat))
            wrong_operations = json.loads(json.dumps(payload))
            wrong_operations["samples"][0]["operations"] = 2
            cases.append(("operation count", wrong_operations))
            wrong_latency = json.loads(json.dumps(payload))
            wrong_latency["latency_samples"].pop()
            cases.append(("latency count", wrong_latency))
            incomplete_validation = json.loads(json.dumps(payload))
            incomplete_validation["validation"]["empty_build_checked"] = False
            cases.append(("validation evidence", incomplete_validation))

            for name, invalid in cases:
                with self.subTest(name=name):
                    path.write_text(json.dumps(invalid), encoding="utf-8")
                    with self.assertRaises(RunFailure):
                        parse_runner_result(path, 2, 3, 1, 2, 0)

    def test_runner_exit_one_preserves_structured_failure_and_pairs_status(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            rows = root / "rows.bin"
            queries = root / "queries.bin"
            write_rows(rows, [b"a", b"b"])
            write_queries(queries, [(b"a", True, 0), (b"z", False, 0), (b"b", True, 1)])
            executable = root / "runner"
            executable.write_text("", encoding="utf-8")

            def completed_with(payload: dict, returncode: int):
                def run(command, **_kwargs):
                    result_path = Path(command[command.index("--result") + 1])
                    result_path.write_text(json.dumps(payload), encoding="utf-8")
                    return subprocess.CompletedProcess(command, returncode)

                return run

            failed = failed_runner_payload("failed")
            with mock.patch(
                "benchmark_lib.adapters.subprocess.run",
                side_effect=completed_with(failed, 1),
            ):
                self.assertEqual(
                    invoke_runner(executable, rows, queries, root / "failed", 2, 1, 10),
                    failed,
                )

            invalid = failed_runner_payload("invalid_input")
            with mock.patch(
                "benchmark_lib.adapters.subprocess.run",
                side_effect=completed_with(invalid, 1),
            ):
                self.assertEqual(
                    invoke_runner(executable, rows, queries, root / "invalid", 2, 1, 10),
                    invalid,
                )

            with mock.patch(
                "benchmark_lib.adapters.subprocess.run",
                side_effect=completed_with(failed, 0),
            ):
                with self.assertRaisesRegex(RunFailure, "inconsistent"):
                    invoke_runner(executable, rows, queries, root / "mismatch", 2, 1, 10)

    def test_zero_latency_result_uses_null_and_no_latency_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "result.json"
            payload = passed_runner_payload(latency_samples=0)
            path.write_text(json.dumps(payload), encoding="utf-8")
            parsed = parse_runner_result(path, 2, 3, 0, 2, 0)
            self.assertEqual(parsed["latency_samples"], [])
            self.assertTrue(
                all(sample["latency_build_ns"] is None for sample in parsed["samples"])
            )

    def test_build_and_run_failures_are_classified(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest = load_manifest(make_manifest(root / "adapter"))
            with mock.patch.object(benchmark, "build_runner", side_effect=BuildFailure("timeout")):
                result = benchmark._run_implementation(
                    manifest, 1, root / "out-a", root / "r", root / "q", 1, 1, 1, 1, 1
                )[0]
            self.assertEqual((result["status"], result["stage"]), ("failed", "build"))

            executable = root / "runner"
            executable.write_text("", encoding="utf-8")
            identity = initial_build_identity(manifest, ROOT)
            with mock.patch.object(benchmark, "build_runner", return_value=(executable, identity)), mock.patch.object(
                benchmark, "invoke_runner", side_effect=RunFailure("timeout")
            ):
                result = benchmark._run_implementation(
                    manifest, 1, root / "out-b", root / "r", root / "q", 1, 1, 1, 1, 1
                )[0]
            self.assertEqual((result["status"], result["stage"]), ("failed", "run"))
            self.assertEqual(result["build_identity"], identity)

            structured = failed_runner_payload("invalid_input")
            structured["stage"] = "interchange"
            with mock.patch.object(
                benchmark, "build_runner", return_value=(executable, identity)
            ), mock.patch.object(benchmark, "invoke_runner", return_value=structured):
                result = benchmark._run_implementation(
                    manifest, 1, root / "out-c", root / "r", root / "q", 1, 1, 1, 1, 1
                )[0]
            self.assertEqual(
                (result["status"], result["stage"], result["reason"]),
                ("invalid_input", "interchange", "safe structured failure"),
            )
            self.assertEqual(result["runner_result"], structured)

    def test_reports_keep_status_rows_and_throughput_dispersion(self) -> None:
        passed = {
            "implementation_id": "ok",
            "status": "passed",
            "stage": "complete",
            "reason": "",
            "runner_result": {
                "descriptor": {},
                "memory": {},
                "samples": [
                    {"repeat": 0, "build_ns": 4, "lookup_ns": 10, "operations": 5, "throughput_ops_per_second": 2},
                    {"repeat": 1, "build_ns": 6, "lookup_ns": 8, "operations": 5, "throughput_ops_per_second": 4},
                ],
                "latency_samples": [2, 8],
            },
        }
        failed = {
            "implementation_id": "bad",
            "status": "failed",
            "stage": "build",
            "reason": "failed",
            "runner_result": None,
        }
        row = summary_row(passed)
        self.assertEqual(row["median_throughput_ops_per_second"], 3)
        self.assertEqual(row["min_throughput_ops_per_second"], 2.0)
        self.assertEqual(row["max_throughput_ops_per_second"], 4.0)
        self.assertEqual(row["stdev_throughput_ops_per_second"], 1.0)
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp)
            write_reports(
                output,
                {},
                {"reader": "raw-lines", "logical_sha256": "x"},
                {"schema": SCHEMA},
                [passed, failed],
            )
            with (output / "summary.csv").open(encoding="utf-8", newline="") as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual([item["status"] for item in rows], ["passed", "failed"])
            self.assertEqual(rows[1]["median_throughput_ops_per_second"], "")
            self.assertTrue(set(SUMMARY_FIELDS).issubset(rows[0]))
            self.assertIn("构建中位数", (output / "report.md").read_text(encoding="utf-8"))


class CliTests(unittest.TestCase):
    def test_help_names_scope_and_options(self) -> None:
        completed = subprocess.run(
            [sys.executable, "-B", str(ROOT / "scripts/benchmark.py"), "--help"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0)
        for text in (
            "--implementation",
            "--workload",
            "miss_ratio",
            "--build-timeout",
            "--run-timeout",
            "--jobs",
            "--memory-budget-mib",
            "bulk_load/find only",
        ):
            self.assertIn(text, completed.stdout)

    def test_malformed_dataset_never_reaches_build(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            dataset = root / "bad.jsonl"
            dataset.write_text("not-json\n", encoding="utf-8")
            adapter = make_manifest(root / "adapter")
            output = root / "output"
            with mock.patch.object(benchmark, "_run_implementation") as run:
                status = benchmark.main(
                    [
                        "--dataset",
                        str(dataset),
                        "--reader",
                        "jsonl",
                        "--key",
                        "key",
                        "--implementation",
                        str(adapter),
                        "--output",
                        str(output),
                    ]
                )
            self.assertEqual(status, 2)
            self.assertTrue(output.is_dir())
            self.assertFalse((output / "interchange").exists())
            for name in ("report.md", "report.json", "summary.csv", "samples.csv"):
                self.assertTrue((output / name).is_file())
            report = json.loads((output / "report.json").read_text(encoding="utf-8"))
            self.assertEqual(report["dataset"]["source_records"], None)
            self.assertEqual(report["results"][0]["status"], "invalid_input")
            self.assertEqual(report["results"][0]["stage"], "dataset")
            run.assert_not_called()

    def test_malformed_workload_reports_all_requested_implementations(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            dataset = root / "keys.txt"
            dataset.write_text("a\n", encoding="utf-8")
            first = make_manifest(root / "first", "first")
            second = make_manifest(root / "second", "second")
            workload = root / "bad.json"
            workload.write_text("not-json", encoding="utf-8")
            output = root / "output"
            with mock.patch.object(benchmark, "_run_implementation") as run:
                status = benchmark.main(
                    [
                        "--dataset",
                        str(dataset),
                        "--implementation",
                        str(first),
                        "--implementation",
                        str(second),
                        "--workload",
                        str(workload),
                        "--output",
                        str(output),
                    ]
                )
            self.assertEqual(status, 2)
            self.assertFalse((output / "interchange").exists())
            report = json.loads((output / "report.json").read_text(encoding="utf-8"))
            self.assertEqual(len(report["results"]), 2)
            self.assertEqual(
                [(item["status"], item["stage"]) for item in report["results"]],
                [("invalid_input", "workload"), ("invalid_input", "workload")],
            )
            self.assertIsNone(report["dataset"]["source_records"])
            run.assert_not_called()

    def test_resource_budget_creates_skipped_report_without_build(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            dataset = root / "large.txt"
            dataset.write_bytes(b"x" * (1024 * 1024 + 1))
            adapter = make_manifest(root / "adapter")
            output = root / "output"
            with mock.patch.object(benchmark, "_run_implementation") as run:
                status = benchmark.main(
                    [
                        "--dataset",
                        str(dataset),
                        "--implementation",
                        str(adapter),
                        "--output",
                        str(output),
                        "--memory-budget-mib",
                        "1",
                    ]
                )
            self.assertEqual(status, 0)
            run.assert_not_called()
            report = json.loads((output / "report.json").read_text(encoding="utf-8"))
            self.assertEqual(report["results"][0]["status"], "skipped")
            self.assertEqual(report["results"][0]["stage"], "resource_budget")
            self.assertFalse((output / "interchange").exists())

    def test_existing_output_is_never_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            output = root / "output"
            output.mkdir()
            sentinel = output / "keep"
            sentinel.write_text("unchanged", encoding="utf-8")
            status = benchmark.main(
                [
                    "--dataset",
                    str(root / "missing.txt"),
                    "--implementation",
                    str(root / "missing-adapter"),
                    "--output",
                    str(output),
                ]
            )
            self.assertEqual(status, 2)
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "unchanged")

    def test_invalid_manifest_is_reported_as_invalid_input(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            dataset = root / "keys.txt"
            dataset.write_text("a\n", encoding="utf-8")
            adapter = root / "adapter"
            adapter.mkdir()
            (adapter / "benchmark-adapter.json").write_text("{}", encoding="utf-8")
            output = root / "output"
            with mock.patch.object(benchmark, "_run_implementation") as run:
                status = benchmark.main(
                    [
                        "--dataset",
                        str(dataset),
                        "--implementation",
                        str(adapter),
                        "--output",
                        str(output),
                    ]
                )
            self.assertEqual(status, 1)
            run.assert_not_called()
            result = json.loads((output / "report.json").read_text(encoding="utf-8"))[
                "results"
            ][0]
            self.assertEqual((result["status"], result["stage"]), ("invalid_input", "manifest"))
            self.assertIn("adapter_manifest_sha256", result["build_identity"])

    def test_manifest_filesystem_failure_is_reported_before_build(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            dataset = root / "keys.txt"
            dataset.write_text("a\n", encoding="utf-8")
            manifest_path = make_manifest(root / "adapter")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["source_dir"] = "private-key\u0000source"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            output = root / "output"
            with mock.patch.object(benchmark, "_run_implementation") as run:
                status = benchmark.main(
                    [
                        "--dataset",
                        str(dataset),
                        "--implementation",
                        str(manifest_path),
                        "--output",
                        str(output),
                    ]
                )
            self.assertEqual(status, 1)
            run.assert_not_called()
            result = json.loads((output / "report.json").read_text(encoding="utf-8"))["results"][0]
            self.assertEqual((result["status"], result["stage"]), ("invalid_input", "manifest"))
            self.assertNotIn("private-key", result["reason"])


if __name__ == "__main__":
    unittest.main()
