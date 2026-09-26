"""Complete JSON, CSV, and Chinese Markdown reports."""

import csv
import json
import math
import platform
import statistics
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


SUMMARY_FIELDS = [
    "implementation_id",
    "status",
    "stage",
    "reason",
    "implementation",
    "implementation_version",
    "adapter_version",
    "compiler_path",
    "compiler_id",
    "compiler_version",
    "build_type",
    "runner_binary_sha256",
    "repeats",
    "operations",
    "median_build_ns",
    "median_lookup_ns",
    "median_throughput_ops_per_second",
    "min_throughput_ops_per_second",
    "max_throughput_ops_per_second",
    "stdev_throughput_ops_per_second",
    "latency_p50_ns",
    "latency_p99_ns",
    "index_owned_bytes",
    "adapter_owned_bytes",
    "retained_input_bytes",
    "memory_notes",
]

SAMPLE_FIELDS = [
    "implementation_id",
    "status",
    "stage",
    "sample_type",
    "repeat",
    "sample_index",
    "build_ns",
    "latency_build_ns",
    "lookup_ns",
    "operations",
    "throughput_ops_per_second",
    "latency_ns",
]


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def environment_metadata() -> Dict[str, Any]:
    return {
        "platform": platform.platform(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "cpu_count": __import__("os").cpu_count(),
        "threading": "single-threaded benchmark runner",
    }


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _median(values: Iterable[Any]) -> Any:
    numbers = [number for value in values if (number := _number(value)) is not None]
    if not numbers:
        return ""
    result = statistics.median(numbers)
    return int(result) if result.is_integer() else result


def _percentile(values: Iterable[Any], fraction: float) -> Any:
    numbers = sorted(number for value in values if (number := _number(value)) is not None)
    if not numbers:
        return ""
    index = int(math.ceil(fraction * len(numbers))) - 1
    result = numbers[max(0, min(index, len(numbers) - 1))]
    return int(result) if result.is_integer() else result


def _minimum(values: Iterable[Any]) -> Any:
    numbers = [number for value in values if (number := _number(value)) is not None]
    return min(numbers) if numbers else ""


def _maximum(values: Iterable[Any]) -> Any:
    numbers = [number for value in values if (number := _number(value)) is not None]
    return max(numbers) if numbers else ""


def _stdev(values: Iterable[Any]) -> Any:
    numbers = [number for value in values if (number := _number(value)) is not None]
    return statistics.pstdev(numbers) if numbers else ""


def _latency_value(sample: Any) -> Any:
    if isinstance(sample, dict):
        for name in ("latency_ns", "ns", "value"):
            if name in sample:
                return sample[name]
        return None
    return sample


def summary_row(result: Dict[str, Any]) -> Dict[str, Any]:
    runner = result.get("runner_result") or {}
    descriptor = runner.get("descriptor") if isinstance(runner.get("descriptor"), dict) else {}
    memory = runner.get("memory") if isinstance(runner.get("memory"), dict) else {}
    samples = runner.get("samples") if isinstance(runner.get("samples"), list) else []
    valid_samples = [sample for sample in samples if isinstance(sample, dict)]
    latencies = runner.get("latency_samples")
    if not isinstance(latencies, list):
        latencies = []
    status = result.get("status", "failed")
    measured = status == "passed"
    throughputs = [sample.get("throughput_ops_per_second") for sample in valid_samples]
    build_identity = result.get("build_identity")
    if not isinstance(build_identity, dict):
        build_identity = {}
    compiler = build_identity.get("compiler")
    if not isinstance(compiler, dict):
        compiler = {}
    configuration = build_identity.get("configuration")
    if not isinstance(configuration, dict):
        configuration = {}
    return {
        "implementation_id": result.get("implementation_id", ""),
        "status": status,
        "stage": result.get("stage", ""),
        "reason": result.get("reason", ""),
        "implementation": descriptor.get("implementation", ""),
        "implementation_version": descriptor.get("implementation_version", ""),
        "adapter_version": descriptor.get("adapter_version", ""),
        "compiler_path": compiler.get("path", ""),
        "compiler_id": compiler.get("id", ""),
        "compiler_version": compiler.get("version", ""),
        "build_type": configuration.get("build_type", ""),
        "runner_binary_sha256": build_identity.get("runner_binary_sha256", ""),
        "repeats": len(valid_samples) if measured else "",
        "operations": (
            valid_samples[0].get("operations", "") if measured and valid_samples else ""
        ),
        "median_build_ns": (
            _median(sample.get("build_ns") for sample in valid_samples) if measured else ""
        ),
        "median_lookup_ns": (
            _median(sample.get("lookup_ns") for sample in valid_samples) if measured else ""
        ),
        "median_throughput_ops_per_second": (
            _median(throughputs)
            if measured
            else ""
        ),
        "min_throughput_ops_per_second": _minimum(throughputs) if measured else "",
        "max_throughput_ops_per_second": _maximum(throughputs) if measured else "",
        "stdev_throughput_ops_per_second": _stdev(throughputs) if measured else "",
        "latency_p50_ns": (
            _percentile((_latency_value(item) for item in latencies), 0.50) if measured else ""
        ),
        "latency_p99_ns": (
            _percentile((_latency_value(item) for item in latencies), 0.99) if measured else ""
        ),
        "index_owned_bytes": memory.get("index_owned_bytes", "") if measured else "",
        "adapter_owned_bytes": memory.get("adapter_owned_bytes", "") if measured else "",
        "retained_input_bytes": memory.get("retained_input_bytes", "") if measured else "",
        "memory_notes": memory.get("accounting_notes", "") if measured else "",
    }


def sample_rows(result: Dict[str, Any]) -> List[Dict[str, Any]]:
    base = {
        "implementation_id": result.get("implementation_id", ""),
        "status": result.get("status", "failed"),
        "stage": result.get("stage", ""),
    }
    runner = result.get("runner_result") or {}
    rows = []  # type: List[Dict[str, Any]]
    samples = runner.get("samples") if isinstance(runner, dict) else None
    if base["status"] == "passed" and isinstance(samples, list):
        for index, sample in enumerate(samples):
            if not isinstance(sample, dict):
                continue
            row = dict(base)
            row.update(
                {
                    "sample_type": "throughput",
                    "repeat": sample.get("repeat", index),
                    "sample_index": "",
                    "build_ns": sample.get("build_ns", ""),
                    "latency_build_ns": sample.get("latency_build_ns", ""),
                    "lookup_ns": sample.get("lookup_ns", ""),
                    "operations": sample.get("operations", ""),
                    "throughput_ops_per_second": sample.get(
                        "throughput_ops_per_second", ""
                    ),
                    "latency_ns": "",
                }
            )
            rows.append(row)
        latencies = runner.get("latency_samples")
        if isinstance(latencies, list):
            for index, sample in enumerate(latencies):
                row = dict(base)
                repeat = sample.get("repeat", "") if isinstance(sample, dict) else ""
                sample_index = (
                    sample.get("sample", sample.get("sample_index", index))
                    if isinstance(sample, dict)
                    else index
                )
                row.update(
                    {
                        "sample_type": "latency",
                        "repeat": repeat,
                        "sample_index": sample_index,
                        "build_ns": "",
                        "latency_build_ns": "",
                        "lookup_ns": "",
                        "operations": "",
                        "throughput_ops_per_second": "",
                        "latency_ns": _latency_value(sample),
                    }
                )
                rows.append(row)
    if not rows:
        row = dict(base)
        row.update({name: "" for name in SAMPLE_FIELDS if name not in row})
        row["sample_type"] = "status"
        rows.append(row)
    return rows


def _markdown_cell(value: Any) -> str:
    return str(value if value not in (None, "") else "不可用").replace("|", "\\|").replace("\n", " ")


def write_reports(
    output: Path,
    config: Dict[str, Any],
    dataset: Dict[str, Any],
    workload: Dict[str, Any],
    results: List[Dict[str, Any]],
) -> None:
    report = {
        "schema": "string-index-benchmark-report/v1",
        "generated_at": utc_timestamp(),
        "scope": "single-threaded static unique-key point lookup",
        "config": config,
        "dataset": dataset,
        "workload": workload,
        "environment": environment_metadata(),
        "results": results,
        "limitations": [
            "Only bulk_load and find are accredited in this first slice.",
            "Latency includes timer overhead; the process is not CPU-pinned.",
            "Memory fields retain the adapter's accounting scope and are not ranked.",
        ],
    }
    (output / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    summaries = [summary_row(result) for result in results]
    with (output / "summary.csv").open("x", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=SUMMARY_FIELDS)
        writer.writeheader()
        writer.writerows(summaries)
    with (output / "samples.csv").open("x", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=SAMPLE_FIELDS)
        writer.writeheader()
        for result in results:
            writer.writerows(sample_rows(result))

    lines = [
        "# 字符串索引统一评测报告",
        "",
        "本报告覆盖单线程、内存、唯一键 map 的静态点查询。首版只验收 `bulk_load` 和 `find`。",
        "",
        "## 数据与负载",
        "",
        "- Reader：{}".format(_markdown_cell(dataset.get("reader"))),
        "- 原始记录：{}；排序去重后键数：{}；重复记录：{}".format(
            _markdown_cell(dataset.get("source_records")),
            _markdown_cell(dataset.get("unique_keys")),
            _markdown_cell(dataset.get("duplicate_records")),
        ),
        "- 逻辑哈希（SHA-256）：`{}`".format(dataset.get("logical_sha256", "")),
        "- 负载：`{}`；请求数：{}；受控未命中：{}；重复：{}".format(
            workload.get("schema", ""),
            _markdown_cell(workload.get("actual_operations")),
            _markdown_cell(workload.get("controlled_misses")),
            _markdown_cell(workload.get("repeats")),
        ),
        "",
        "## 实现结果",
        "",
        "| 实现 ID | 状态 | 阶段 | 原因 | 构建中位数（ns） | 吞吐量中位数（ops/s） | 吞吐量范围（ops/s） | 吞吐量标准差 | P50 延迟（ns） | P99 延迟（ns） |",
        "| --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in summaries:
        lines.append(
            "| {} | {} | {} | {} | {} | {} | {}–{} | {} | {} | {} |".format(
                *(
                    _markdown_cell(row[name])
                    for name in (
                        "implementation_id",
                        "status",
                        "stage",
                        "reason",
                        "median_build_ns",
                        "median_throughput_ops_per_second",
                        "min_throughput_ops_per_second",
                        "max_throughput_ops_per_second",
                        "stdev_throughput_ops_per_second",
                        "latency_p50_ns",
                        "latency_p99_ns",
                    )
                )
            )
        )
    lines.extend(
        [
            "",
            "## 解读限制",
            "",
            "失败、跳过和不支持的实现保留在所有汇总文件中，性能列为空，不以零值代替。内存分项只有在适配器给出明确口径时才展示；本报告不据此生成内存排名。延迟包含计时器开销，进程未绑核，小规模结果不用于论文级排名。",
            "",
        ]
    )
    (output / "report.md").write_text("\n".join(lines), encoding="utf-8")
