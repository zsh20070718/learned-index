"""Complete JSON, CSV, and Chinese Markdown reports."""

import csv
import json
import math
import os
import platform
import statistics
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


SUMMARY_FIELDS = [
    "implementation_id",
    "threads",
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
    "speedup_vs_1_thread",
    "parallel_efficiency",
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
    "threads",
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

WORKER_FIELDS = [
    "implementation_id", "threads", "repeat", "worker", "operations",
    "lookup_ns", "checksum",
]

SCALING_FIELDS = [
    "implementation_id", "threads", "status", "operations",
    "median_throughput_ops_per_second", "speedup_vs_1_thread", "parallel_efficiency",
]


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def environment_metadata() -> Dict[str, Any]:
    try:
        affinity = sorted(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        affinity = None
    cpu_model = platform.processor() or None
    try:
        with open("/proc/cpuinfo", encoding="utf-8") as stream:
            for line in stream:
                if line.startswith("model name"):
                    cpu_model = line.partition(":")[2].strip()
                    break
    except OSError:
        pass
    return {
        "platform": platform.platform(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "cpu_count": os.cpu_count(),
        "cpu_model": cpu_model,
        "cpu_affinity": affinity,
        "available_cpu_count": len(affinity) if affinity is not None else os.cpu_count(),
        "threading": "shared-index static reads; configurations run sequentially",
        "cpu_pinning": "not performed; inherited process affinity",
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
        "threads": result.get("threads", 1),
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
        "speedup_vs_1_thread": "",
        "parallel_efficiency": "",
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
        "threads": result.get("threads", 1),
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


def scaling_summaries(results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    summaries = [summary_row(result) for result in results]
    baselines = {
        row["implementation_id"]: row
        for row in summaries if row["threads"] == 1 and row["status"] == "passed"
    }
    for row in summaries:
        baseline = baselines.get(row["implementation_id"])
        if row["status"] != "passed" or baseline is None:
            continue
        throughput = _number(row["median_throughput_ops_per_second"])
        baseline_throughput = _number(baseline["median_throughput_ops_per_second"])
        if (throughput is not None and baseline_throughput is not None
                and baseline_throughput > 0 and row["operations"] == baseline["operations"]):
            speedup = throughput / baseline_throughput
            row["speedup_vs_1_thread"] = speedup
            row["parallel_efficiency"] = speedup / row["threads"]
    return summaries


def write_reports(
    output: Path,
    config: Dict[str, Any],
    dataset: Dict[str, Any],
    workload: Dict[str, Any],
    results: List[Dict[str, Any]],
) -> None:
    mixed = workload.get("schema") == "mixed-operations/v1"
    # Global preparation/build failures still have one row per requested pair.
    results = [
        dict(result, threads=threads)
        for result in results
        for threads in ([result["threads"]] if "threads" in result else config.get("threads", [1]))
    ]
    summaries = scaling_summaries(results)
    scaling = [{field: row[field] for field in SCALING_FIELDS} for row in summaries]
    environment = environment_metadata()
    if mixed:
        environment["threading"] = "serial mixed trace; threads greater than one are unsupported"
    report = {
        "schema": "string-index-benchmark-report/v2",
        "generated_at": utc_timestamp(),
        "scope": (
            "deterministic serial mixed string-index operations"
            if mixed else
            "static unique-key point lookup, serial and shared-index concurrent reads"
        ),
        "config": config,
        "dataset": dataset,
        "workload": workload,
        "environment": environment,
        "results": results,
        "scaling": scaling,
        "limitations": (
            [
                "Mixed operations are serial and aggregate throughput has no per-operation breakdown.",
                "Mixed latency sampling is disabled; scans consume rows but report aggregate timing only.",
                "The process is not CPU-pinned; memory fields retain adapter accounting scope.",
            ]
            if mixed else
            [
                "Only bulk_load and find are accredited; concurrent writes are not measured.",
                "Latency includes timer overhead; the process is not CPU-pinned.",
                "Latency is measured separately with one thread, not under parallel load.",
                "Total queries are fixed across threads; no warmup, no thread creation in lookup timing.",
                "Concurrent correctness checks do not prove absence of every possible data race.",
                "Memory fields retain the adapter's accounting scope and are not ranked.",
            ]
        ),
    }
    (output / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    with (output / "summary.csv").open("x", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=SUMMARY_FIELDS)
        writer.writeheader()
        writer.writerows(summaries)
    with (output / "samples.csv").open("x", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=SAMPLE_FIELDS)
        writer.writeheader()
        for result in results:
            writer.writerows(sample_rows(result))
    with (output / "scaling.csv").open("x", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=SCALING_FIELDS)
        writer.writeheader()
        writer.writerows(scaling)
    with (output / "workers.csv").open("x", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=WORKER_FIELDS)
        writer.writeheader()
        for result in results:
            if result["status"] != "passed":
                continue
            for sample in (result.get("runner_result") or {}).get("samples", []):
                for worker in sample.get("workers", []):
                    writer.writerow(dict(worker, implementation_id=result["implementation_id"],
                                         threads=result["threads"], repeat=sample["repeat"]))

    lines = [
        "# 字符串索引统一评测报告",
        "",
        (
            "本报告覆盖确定性单线程混合操作轨迹，按实际非零操作要求能力，并用独立 oracle 校验状态变化。"
            if mixed else
            "本报告覆盖内存、唯一键 map 的静态点查询，支持单线程和共享索引实例的并发只读查询。验收 `bulk_load` 和 `find`。"
        ),
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
        "- 负载：`{}`；请求数：{}；{}：{}；重复：{}".format(
            workload.get("schema", ""),
            _markdown_cell(workload.get("actual_operations")),
            "实际最终键数" if mixed else "受控未命中",
            _markdown_cell(workload.get("final_key_count") if mixed else workload.get("controlled_misses")),
            _markdown_cell(workload.get("repeats")),
        ),
        *_workload_lines(workload),
        "- 查询线程数：{}；各配置依次运行，每个适配器仅编译一次。".format(
            ", ".join(str(n) for n in config.get("threads", [1]))),
        (
            "- 每轮重放同一混合轨迹；线程数大于 1 的配置会结构化报告不支持。"
            if mixed else
            "- 每轮总请求数固定，连续均分给工作线程，不随线程数增加。"
        ),
        "",
        "## 实现结果",
        "",
        (
            "| 实现 ID | 线程数 | 状态 | 阶段 | 原因 | 构建中位数（ns） | 混合吞吐量中位数（ops/s） | 吞吐量范围（ops/s） | 吞吐量标准差 |"
            if mixed else
            "| 实现 ID | 线程数 | 状态 | 阶段 | 原因 | 构建中位数（ns） | 吞吐量中位数（ops/s） | 吞吐量范围（ops/s） | 吞吐量标准差 | 独立单线程 P50（ns） | 独立单线程 P99（ns） |"
        ),
        "| --- | ---: | --- | --- | --- | ---: | ---: | ---: | ---: |" + ("" if mixed else " ---: | ---: |"),
    ]
    for row in summaries:
        cells = [_markdown_cell(row[name]) for name in (
            "implementation_id", "threads", "status", "stage", "reason",
            "median_build_ns", "median_throughput_ops_per_second",
        )]
        cells.append("{}–{}".format(
            _markdown_cell(row["min_throughput_ops_per_second"]),
            _markdown_cell(row["max_throughput_ops_per_second"]),
        ))
        cells.append(_markdown_cell(row["stdev_throughput_ops_per_second"]))
        if not mixed:
            cells.extend(_markdown_cell(row[name]) for name in ("latency_p50_ns", "latency_p99_ns"))
        lines.append("| " + " | ".join(cells) + " |")
    if not mixed:
        lines.extend([
            "", "## 线程扩展性", "",
            "加速比 = 当前吞吐量中位数 / 同一实现的单线程吞吐量中位数；并行效率 = 加速比 / 线程数。未请求单线程、单线程失败或总操作数不同时留空。", "",
            "| 实现 ID | 线程数 | 状态 | 加速比 | 并行效率 |",
            "| --- | ---: | --- | ---: | ---: |",
        ])
        for row in summaries:
            lines.append("| {} | {} | {} | {} | {} |".format(*(
                _markdown_cell(row[name]) for name in (
                    "implementation_id", "threads", "status", "speedup_vs_1_thread", "parallel_efficiency"
                )
            )))
    lines.extend([
        "", "## 解读限制", "",
        "失败、跳过和不支持的实现保留在所有汇总文件中，性能列为空，不以零值代替。内存分项只有在适配器给出明确口径时才展示；本报告不据此生成内存排名。进程未绑核，小规模结果不用于论文级排名。",
    ])
    if mixed:
        lines.extend([
            "混合操作按单线程重放固定轨迹，每轮重新构造并加载初始状态；独立参照、修改后读回和最终状态检查在计时之外。一次扫描计一次操作，返回行数另列，结果物化与消费计入操作时间。",
            "本场景关闭延迟抽样，不提供 P50/P99 或各类操作的单独延迟，也不进行线程扩展测量。线程数大于 1 的组合明确报告不支持；scaling.csv 的公共格式不代表混合并发测量。",
            "`workers.csv` 保留每轮单个执行线程的操作数、耗时与校验和。运行环境和继承的 CPU 亲和性见 `report.json`。",
        ])
    else:
        lines.extend([
            "并行吞吐量计时从所有工作线程就绪后的统一释放开始，到全部完成为止；不含创建/回收线程及构建，包含调度和同步开销。延迟来自另建索引的独立单线程抽样，不是并发负载下的尾延迟；延迟包含计时器开销，默认不预热。并发校验通过只说明本次工作负载通过，不证明实现没有数据竞争。",
            "`workers.csv` 保留每轮各线程的操作数、耗时与校验和；`scaling.csv` 可直接绘制扩展性曲线。运行环境和继承的 CPU 亲和性见 `report.json`。",
        ])
    lines.append("")
    (output / "report.md").write_text("\n".join(lines), encoding="utf-8")


def _workload_lines(workload: Dict[str, Any]) -> List[str]:
    if workload.get("schema") == "mixed-operations/v1":
        configured = workload.get("configured_operations") or {}
        outcomes = workload.get("actual_result_counts") or {}
        return [
            "- 配置操作数：find {}；insert {}；update {}；erase {}；scan {}。".format(
                *(_markdown_cell(configured.get(name)) for name in ("find", "insert", "update", "erase", "scan"))
            ),
            "- 初始键 {}；留出键 {}；最终键 {}；实际 find 命中/未命中 {}/{}。".format(
                _markdown_cell(workload.get("initial_key_count")),
                _markdown_cell(workload.get("held_out_key_count")),
                _markdown_cell(workload.get("final_key_count")),
                _markdown_cell(outcomes.get("find_hits")),
                _markdown_cell(outcomes.get("find_misses")),
            ),
            "- 初始值策略：{}；操作轨迹哈希（SHA-256）：`{}`".format(
                _markdown_cell(workload.get("initial_value_policy")),
                workload.get("operations_sha256", ""),
            ),
        ]
    distribution = workload.get("distribution")
    if distribution is None and workload.get("schema") == "static-uniform-point/v1":
        distribution = "uniform"
    label = {
        "uniform": "均匀访问（uniform）",
        "hotspot": "热点访问（hotspot）",
        "zipf": "Zipf 偏斜访问（zipf）",
    }.get(distribution, "未确定")
    lines = ["- 访问分布：{}".format(label)]
    parameters = workload.get("distribution_parameters") or {}
    if distribution == "hotspot":
        lines.append(
            "- 热区参数：源键比例 {}；抽取热区的概率 {}（实际次数允许波动）。".format(
                _markdown_cell(parameters.get("hotset_fraction_decimal")),
                _markdown_cell(parameters.get("hot_probability_decimal")),
            )
        )
    elif distribution == "zipf":
        lines.append(
            "- Zipf 指数：{}；按字节排序名次 r 使用权重 r^(-指数)，指数 0 为均匀访问。".format(
                _markdown_cell(parameters.get("zipf_exponent_decimal")),
            )
        )
    observed = workload.get("observed")
    if isinstance(observed, dict):
        lines.append(
            "- 实际命中：{}；实际未命中：{}；命中访问覆盖 {} 个不同键。".format(
                _markdown_cell(observed.get("hit_queries")),
                _markdown_cell(observed.get("miss_queries")),
                _markdown_cell(observed.get("unique_hit_keys")),
            )
        )
        if distribution in ("hotspot", "zipf"):
            fraction = observed.get(
                "hotset_hit_fraction" if distribution == "hotspot" else "rank_one_hit_fraction"
            )
            share = "{:.2%}".format(fraction) if fraction is not None else "未测量（没有命中查询）"
            detail = (
                "热区含 {} 个键；命中查询访问热区的比例：{}".format(
                    _markdown_cell(observed.get("hotset_key_count")), share,
                )
                if distribution == "hotspot" else "命中查询访问第 1 名键的比例：{}".format(share)
            )
            lines.append("- {}。".format(detail))
    if distribution in ("hotspot", "zipf"):
        lines.append("- 热区和 Zipf 名次由字节排序决定，是合成访问模型，未从真实流量估计；未命中键由选中的源键变异生成。")
    if workload.get("queries_sha256"):
        lines.append("- 查询序列哈希（SHA-256）：`{}`".format(workload["queries_sha256"]))
    return lines
