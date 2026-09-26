#!/usr/bin/env python3
"""Unified static point-lookup benchmark for external string-index adapters."""

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from benchmark_lib.adapters import (
    AdapterManifest,
    build_runner,
    initial_build_identity,
    invoke_runner,
    load_manifest,
)
from benchmark_lib.errors import BuildFailure, InputError, ResourceBudget, RunFailure
from benchmark_lib.interchange import write_queries, write_rows
from benchmark_lib.reports import write_reports
from benchmark_lib.dataset import prepare_dataset
from benchmark_lib.workload import generate_queries, load_workload


ROOT = Path(__file__).resolve().parents[1]


def _positive_integer(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be an integer")
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def _jobs(value: str) -> int:
    parsed = _positive_integer(value)
    if parsed > 4:
        raise argparse.ArgumentTypeError("must be between 1 and 4")
    return parsed


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description=(
            "Run the first-slice single-threaded static point-lookup benchmark. "
            "Implemented readers: raw-lines, JSONL string field, and strict legacy TLI."
        ),
        epilog=(
            "This slice validates and measures bulk_load/find only; insert, update, "
            "erase, scan, range_sum, directories, and reader plugins are not implemented."
        ),
    )
    result.add_argument("--dataset", required=True, type=Path, help="one input file")
    result.add_argument(
        "--implementation",
        required=True,
        action="append",
        type=Path,
        help="adapter directory or benchmark-adapter.json; repeat for comparisons",
    )
    result.add_argument(
        "--output",
        type=Path,
        help="new result directory (default: a unique directory below results/)",
    )
    result.add_argument(
        "--reader",
        choices=("auto", "raw-lines", "jsonl", "legacy-tli"),
        default="auto",
        help="dataset reader; auto recognizes .txt/.keys/.lines, .jsonl/.ndjson, and .tli",
    )
    result.add_argument(
        "--key", help="required top-level string field name for the JSONL reader"
    )
    result.add_argument(
        "--workload",
        type=Path,
        help=(
            "static-uniform-point/v1 JSON overriding seed/operations/"
            "miss_ratio/repeats/latency_samples"
        ),
    )
    result.add_argument(
        "--build-timeout",
        "--build-timeout-seconds",
        dest="build_timeout_seconds",
        type=_positive_integer,
        default=120,
        help="seconds allowed for each configure and build command (default: 120)",
    )
    result.add_argument(
        "--run-timeout",
        "--run-timeout-seconds",
        dest="run_timeout_seconds",
        type=_positive_integer,
        default=120,
        help="seconds allowed for each adapter runner (default: 120)",
    )
    result.add_argument(
        "--jobs",
        type=_jobs,
        default=2,
        help="conservative CMake build parallelism, 1-4 (default: 2)",
    )
    result.add_argument(
        "--memory-budget-mib",
        type=_positive_integer,
        default=1024,
        help=(
            "explicit estimated in-process data/workload preparation budget "
            "(default: 1024 MiB)"
        ),
    )
    return result


def _default_output() -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    base = ROOT / "results" / "benchmark-{}-{}".format(stamp, os.getpid())
    candidate = base
    suffix = 1
    while candidate.exists():
        candidate = Path("{}-{}".format(base, suffix))
        suffix += 1
    return candidate


def _invalid_manifest_result(path: Path, ordinal: int, reason: str) -> Dict[str, Any]:
    return {
        "implementation_id": "invalid-{}".format(ordinal),
        "requested_path": str(path),
        "status": "invalid_input",
        "stage": "manifest",
        "reason": reason,
        "build_identity": initial_build_identity(None, ROOT, path),
        "runner_result": None,
    }


def _validated_manifests(
    requested: List[Path],
) -> List[Tuple[Optional[AdapterManifest], Optional[Dict[str, Any]]]]:
    entries = []  # type: List[Tuple[Optional[AdapterManifest], Optional[Dict[str, Any]]]]
    seen = set()
    for ordinal, path in enumerate(requested, 1):
        try:
            manifest = load_manifest(path)
            if manifest.adapter_id in seen:
                entries.append(
                    (
                        None,
                        _invalid_manifest_result(
                            path, ordinal, "duplicate adapter id in this invocation"
                        ),
                    )
                )
            else:
                seen.add(manifest.adapter_id)
                entries.append((manifest, None))
        except InputError as error:
            entries.append((None, _invalid_manifest_result(path, ordinal, str(error))))
    return entries


def _status_result(
    manifest: AdapterManifest,
    status: str,
    stage: str,
    reason: str,
    build_identity: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    return {
        "implementation_id": manifest.adapter_id,
        "requested_path": str(manifest.requested_path),
        "manifest_path": str(manifest.manifest_path),
        "manifest": manifest.raw,
        "status": status,
        "stage": stage,
        "reason": reason,
        "build_identity": (
            build_identity
            if build_identity is not None
            else initial_build_identity(manifest, ROOT)
        ),
        "runner_result": None,
    }


def _create_output(output: Path) -> bool:
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        output.mkdir(exist_ok=False)
    except FileExistsError:
        return False
    return True


def _config_metadata(args: argparse.Namespace, output: Path) -> Dict[str, Any]:
    return {
        "dataset": str(args.dataset.resolve()),
        "implementations": [str(path.resolve()) for path in args.implementation],
        "output": str(output),
        "reader": args.reader,
        "key": args.key,
        "workload": str(args.workload.resolve()) if args.workload else None,
        "build_timeout_seconds": args.build_timeout_seconds,
        "run_timeout_seconds": args.run_timeout_seconds,
        "jobs": args.jobs,
        "memory_budget_mib": args.memory_budget_mib,
        "network_downloads": "not performed by the benchmark",
    }


def _resource_budget_report(
    args: argparse.Namespace,
    output: Path,
    manifest_entries: List[Tuple[Optional[AdapterManifest], Optional[Dict[str, Any]]]],
    workload: Any,
    reason: str,
    dataset_metadata: Optional[Dict[str, Any]],
) -> int:
    if not _create_output(output):
        print("error: output directory already exists; refusing to overwrite", file=sys.stderr)
        return 2
    results = []  # type: List[Dict[str, Any]]
    for manifest, invalid_result in manifest_entries:
        if invalid_result is not None:
            results.append(invalid_result)
        else:
            assert manifest is not None
            results.append(
                _status_result(manifest, "skipped", "resource_budget", reason)
            )
    if dataset_metadata is None:
        dataset_metadata = _unavailable_dataset_metadata(args, "resource_budget")
    workload_metadata = workload.metadata(0, 0)
    workload_metadata["status"] = "resource_budget"
    write_reports(
        output,
        _config_metadata(args, output),
        dataset_metadata,
        workload_metadata,
        results,
    )
    print(str(output))
    return 1 if any(result["status"] in ("failed", "invalid_input") for result in results) else 0


def _unavailable_dataset_metadata(args: argparse.Namespace, status: str) -> Dict[str, Any]:
    return {
        "path": str(args.dataset.resolve()),
        "reader": args.reader,
        "key_field": args.key,
        "status": status,
        "source_sha256": None,
        "logical_sha256": None,
        "source_records": None,
        "unique_keys": None,
        "duplicate_records": None,
    }


def _unavailable_workload_metadata(args: argparse.Namespace, status: str) -> Dict[str, Any]:
    return {
        "schema": "static-uniform-point/v1",
        "source": str(args.workload.resolve()) if args.workload else None,
        "status": status,
        "actual_operations": None,
        "controlled_misses": None,
        "repeats": None,
    }


def _invalid_input_report(
    args: argparse.Namespace,
    output: Path,
    manifest_entries: List[Tuple[Optional[AdapterManifest], Optional[Dict[str, Any]]]],
    stage: str,
    reason: str,
    dataset_metadata: Optional[Dict[str, Any]] = None,
    workload_metadata: Optional[Dict[str, Any]] = None,
) -> int:
    if not _create_output(output):
        print("error: output directory already exists; refusing to overwrite", file=sys.stderr)
        return 2
    results = []  # type: List[Dict[str, Any]]
    for ordinal, (manifest, invalid_result) in enumerate(manifest_entries, 1):
        if manifest is not None:
            results.append(
                _status_result(manifest, "invalid_input", stage, reason)
            )
        else:
            requested_path = args.implementation[ordinal - 1]
            result = _invalid_manifest_result(requested_path, ordinal, reason)
            result["stage"] = stage
            results.append(result)
    write_reports(
        output,
        _config_metadata(args, output),
        dataset_metadata
        if dataset_metadata is not None
        else _unavailable_dataset_metadata(args, "invalid_input"),
        workload_metadata
        if workload_metadata is not None
        else _unavailable_workload_metadata(args, "invalid_input"),
        results,
    )
    print("error: {}".format(reason), file=sys.stderr)
    print(str(output))
    return 2


def _run_implementation(
    manifest: AdapterManifest,
    ordinal: int,
    output: Path,
    rows_path: Path,
    queries_path: Path,
    repeats: int,
    latency_samples: int,
    build_timeout: int,
    run_timeout: int,
    jobs: int,
) -> Dict[str, Any]:
    build_root = output / "builds" / "{:03d}-{}".format(ordinal, manifest.adapter_id)
    build_root.parent.mkdir(parents=True, exist_ok=True)
    build_root.mkdir(exist_ok=False)
    try:
        executable, build_identity = build_runner(
            manifest, ROOT, build_root, build_timeout, jobs
        )
    except BuildFailure as error:
        return _status_result(
            manifest, "failed", "build", str(error), error.build_identity
        )

    run_root = output / "runs" / "{:03d}-{}".format(ordinal, manifest.adapter_id)
    run_root.parent.mkdir(parents=True, exist_ok=True)
    try:
        runner_result = invoke_runner(
            executable,
            rows_path,
            queries_path,
            run_root,
            repeats,
            latency_samples,
            run_timeout,
        )
    except RunFailure as error:
        return _status_result(
            manifest, "failed", "run", str(error), build_identity
        )
    result = _status_result(
        manifest,
        runner_result["status"],
        runner_result.get("stage", "complete"),
        runner_result.get("reason", ""),
        build_identity,
    )
    result["runner_result"] = runner_result
    return result


def main(argv: Optional[List[str]] = None) -> int:
    args = parser().parse_args(argv)
    output = args.output.resolve() if args.output else _default_output()
    if output.exists():
        print("error: output directory already exists; refusing to overwrite", file=sys.stderr)
        return 2

    memory_budget_bytes = args.memory_budget_mib * 1024 * 1024
    manifest_entries = _validated_manifests(args.implementation)
    try:
        workload = load_workload(args.workload)
    except InputError as error:
        return _invalid_input_report(
            args, output, manifest_entries, "workload", str(error)
        )
    try:
        dataset = prepare_dataset(
            args.dataset,
            args.reader,
            args.key,
            memory_budget_bytes,
        )
    except ResourceBudget as error:
        return _resource_budget_report(
            args, output, manifest_entries, workload, str(error), None
        )
    except InputError as error:
        workload_metadata = workload.metadata(0, 0)
        workload_metadata["actual_operations"] = None
        workload_metadata["controlled_misses"] = None
        return _invalid_input_report(
            args,
            output,
            manifest_entries,
            "dataset",
            str(error),
            workload_metadata=workload_metadata,
        )

    try:
        remaining_budget = memory_budget_bytes - dataset.estimated_memory_bytes
        if remaining_budget <= 0:
            raise ResourceBudget("no memory budget remains for workload generation")
        queries, miss_count = generate_queries(dataset.keys, workload, remaining_budget)
    except ResourceBudget as error:
        return _resource_budget_report(
            args, output, manifest_entries, workload, str(error), dataset.metadata()
        )
    except InputError as error:
        return _invalid_input_report(
            args,
            output,
            manifest_entries,
            "workload",
            str(error),
            dataset_metadata=dataset.metadata(),
        )

    if not _create_output(output):
        print("error: output directory already exists; refusing to overwrite", file=sys.stderr)
        return 2
    interchange = output / "interchange"
    interchange.mkdir()
    rows_path = interchange / "rows.bin"
    queries_path = interchange / "queries.bin"
    rows_sha256 = write_rows(rows_path, dataset.keys)
    queries_sha256 = write_queries(queries_path, queries)

    results = []  # type: List[Dict[str, Any]]
    for ordinal, (manifest, invalid_result) in enumerate(manifest_entries, 1):
        if invalid_result is not None:
            results.append(invalid_result)
            continue
        assert manifest is not None
        if not dataset.keys:
            results.append(
                _status_result(
                    manifest,
                    "skipped",
                    "workload",
                    "empty dataset has no keys for the configured static workload",
                )
            )
            continue
        results.append(
            _run_implementation(
                manifest,
                ordinal,
                output,
                rows_path,
                queries_path,
                workload.repeats,
                workload.latency_samples,
                args.build_timeout_seconds,
                args.run_timeout_seconds,
                args.jobs,
            )
        )

    workload_metadata = workload.metadata(len(queries), miss_count)
    workload_metadata["queries_sha256"] = queries_sha256
    config = _config_metadata(args, output)
    dataset_metadata = dataset.metadata()
    dataset_metadata["rows_sha256"] = rows_sha256
    write_reports(output, config, dataset_metadata, workload_metadata, results)
    print(str(output))
    return 1 if any(result["status"] in ("failed", "invalid_input") for result in results) else 0


if __name__ == "__main__":
    sys.exit(main())
