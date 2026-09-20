#!/usr/bin/env python3
"""Exercise TLI string generation, validation and throughput in an isolated run directory."""
import argparse
import csv
import hashlib
import json
import math
from pathlib import Path
import re
import shutil
import statistics
import subprocess
import sys
import tempfile

from make_lookup_workload import read_keys, sha256
from prepare_real_datasets import describe_keys

ROOT = Path(__file__).resolve().parents[1]
WORKLOADS = [
    ("lookup", 0.0, 0.0, "equality"),
    ("scan", 1.0, 0.0, "equality"),
    ("read50_insert50", 0.025, 0.5, "equality"),
    ("read80_insert20", 0.04, 0.2, "equality"),
    ("read20_insert80", 0.01, 0.8, "equality"),
    ("insert", 0.0, 1.0, "equality"),
    ("delta_insert", 0.025, 0.5, "delta"),
    ("hotspot_insert", 0.025, 0.5, "hotspot"),
]
INDEXES = ("ART", "StdMap")
LOOKUP_WORKLOADS = [
    ("lookup_uniform", "uniform", 0.0),
    ("lookup_miss50", "uniform", 0.5),
    ("lookup_miss100", "uniform", 1.0),
    ("lookup_hotspot", "hotspot", 0.0),
    ("lookup_hotspot_miss50", "hotspot", 0.5),
    ("lookup_zipf", "zipf", 0.0),
    ("lookup_zipf_miss50", "zipf", 0.5),
]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-dir", type=Path, default=ROOT / "build")
    parser.add_argument("--keys", type=int, default=10000)
    parser.add_argument("--operations", type=int, default=2000)
    parser.add_argument("--repeats", type=int, default=1, help="Independent index builds per throughput run")
    parser.add_argument("--dataset", type=Path, action="append", default=[],
                        help="Prepared real _string file with JSON sidecar; repeat to add datasets alongside synthetic controls")
    args = parser.parse_args()
    if not 1000 <= args.keys <= 100000:
        parser.error("keys must be in [1000, 100000]")
    if not 100 <= args.operations <= args.keys - 200:
        parser.error("operations must be in [100, keys - 200] to leave enough initial keys")
    if not 1 <= args.repeats <= 10:
        parser.error("repeats must be in [1, 10]")
    build = args.build_dir.resolve()
    for name in ("string_benchmark", "generate"):
        if not (build / name).is_file():
            parser.error(f"Missing {name}; run scripts/setup.py first")
    external = []
    labels = {"random", "prefix"}
    for path in args.dataset:
        path = path.resolve()
        label = path.name.removesuffix("_string")
        if not path.name.endswith("_string") or not re.fullmatch(r"[A-Za-z0-9_-]+", label) or label in labels:
            parser.error(f"Dataset needs a unique simple name ending in _string: {path}")
        try:
            keys = read_keys(path)
            metadata = json.loads(Path(str(path) + ".json").read_text())
            if len(keys) != args.keys or metadata["keys"] != args.keys:
                parser.error(f"{path}: dataset must have --keys={args.keys} keys for a matched comparison")
            if metadata["dataset_sha256"] != sha256(path) or metadata["kind"] != "real":
                parser.error(f"{path}: metadata kind/hash mismatch")
        except (OSError, ValueError, KeyError, TypeError) as error:
            parser.error(f"{path}: {error}")
        external.append((label, path, metadata))
        labels.add(label)
    (ROOT / "results").mkdir(exist_ok=True)
    run_dir = Path(tempfile.mkdtemp(prefix="smoke-", dir=ROOT / "results"))
    (run_dir / "data").mkdir()
    (run_dir / "results").mkdir()

    def run(argv, log_name):
        with (run_dir / log_name).open("w") as log:
            result = subprocess.run(list(map(str, argv)), cwd=run_dir, stdout=log,
                                    stderr=subprocess.STDOUT, timeout=30)
        text = (run_dir / log_name).read_text()
        if result.returncode:
            raise RuntimeError(f"Command failed ({result.returncode}); see {run_dir / log_name}\n{text[-1500:]}")
        return text

    print(f"Evidence: {run_dir}", flush=True)
    rows = []
    samples = []
    lookup_metadata = {}
    dataset_metadata = {}

    def cases(shape, data):
        for label, scan, insert, pattern in WORKLOADS:
            tag = f"{shape}-{label}"
            before = set((run_dir / "data").iterdir())
            run([build / "generate", data, args.operations, "--scan-ratio", scan, "--insert-ratio", insert,
                 "--insert-pattern", pattern, "--hotspot-ratio", "0.5", "--mix"], f"{tag}-generate.log")
            candidates = [p for p in set((run_dir / "data").iterdir()) - before if not p.name.endswith("_bulkload")]
            if len(candidates) != 1:
                raise RuntimeError(f"Expected one generated workload, got {candidates}")
            yield label, str(candidates[0].relative_to(run_dir))
        for label, distribution, miss_ratio in LOOKUP_WORKLOADS:
            tag = f"{shape}-{label}"
            generated = run([sys.executable, "-B", ROOT / "scripts/make_lookup_workload.py",
                             data, args.operations, "--distribution", distribution,
                             "--miss-ratio", miss_ratio], f"{tag}-generate.log")
            metadata = json.loads(generated)
            if metadata["miss_count"] != int(args.operations * miss_ratio):
                raise RuntimeError(f"Unexpected miss count for {tag}")
            lookup_metadata[tag] = metadata
            yield label, metadata["workload_file"]

    # Each index reuses the same data and operations; verification is untimed.
    datasets = [("random", None, None), ("prefix", None, None)] + external
    for shape, source, metadata in datasets:
        data = f"data/smoke_{shape}_string"
        if source is None:
            run([sys.executable, "-B", ROOT / "scripts/make_dataset.py", data, "--shape", shape,
                 "--count", args.keys], f"{shape}-dataset.log")
            metadata = {"kind": "synthetic", "shape": shape, "seed": 42,
                        "keys": args.keys, "dataset_sha256": sha256(run_dir / data)}
        else:
            shutil.copyfile(source, run_dir / data)
            if sha256(run_dir / data) != metadata["dataset_sha256"]:
                raise RuntimeError(f"Dataset changed during copy: {source}")
        metadata = {**metadata, "statistics": describe_keys(read_keys(run_dir / data))}
        dataset_metadata[shape] = metadata
        for label, ops in cases(shape, data):
            tag = f"{shape}-{label}"
            for index in INDEXES:
                base = [build / "string_benchmark", data, ops, "--only", index]
                verified = run([*base, "--verify"], f"{tag}-{index}-verify.log")
                if f"RESULT: {index}," not in verified:
                    raise RuntimeError(f"{tag}/{index} was skipped; see verification log")
                through = run([*base, "--through", "--csv", "--repeats", args.repeats],
                              f"{tag}-{index}-through.log")
                result = next(line for line in through.splitlines() if line.startswith(f"RESULT: {index},"))
                fields = result.removeprefix("RESULT: ").split(",")
                if len(fields) != 2 * args.repeats + 2:
                    raise RuntimeError(f"Invalid throughput result: {result}")
                builds = [int(x) for x in fields[1:1 + args.repeats]]
                memory = int(fields[1 + args.repeats])
                rates = [float(x) for x in fields[2 + args.repeats:]]
                if any(not math.isfinite(x) or x <= 0 for x in rates):
                    raise RuntimeError(f"Invalid throughput result: {result}")
                rows.append([shape, label, index, statistics.median(builds), memory,
                             statistics.median(rates), "passed", args.repeats,
                             min(rates), max(rates), statistics.stdev(rates) if len(rates) > 1 else 0])
                samples.extend([shape, label, index, repeat, build_us, rate]
                               for repeat, (build_us, rate) in enumerate(zip(builds, rates), 1))
            print(f"PASS {tag}: {' + '.join(INDEXES)}", flush=True)
    with (run_dir / "summary.csv").open("w", newline="") as out:
        writer = csv.writer(out)
        writer.writerow(["key_shape", "workload", "index", "build_us", "reported_index_bytes",
                         "throughput_mops_per_s", "lookup_range_verification", "repeats",
                         "throughput_min", "throughput_max", "throughput_stdev"])
        writer.writerows(rows)
    with (run_dir / "samples.csv").open("w", newline="") as out:
        writer = csv.writer(out)
        writer.writerow(["key_shape", "workload", "index", "repeat", "build_us", "throughput_mops_per_s"])
        writer.writerows(samples)
    manifest = {
        "scope": "setup smoke only; not a publication performance experiment",
        "keys_per_shape": args.keys, "operations_per_workload": args.operations, "threads": 1,
        "repeats": args.repeats, "seed": 42, "indexes": INDEXES,
        "datasets": dataset_metadata,
        "lookup_workloads": lookup_metadata,
        "verification": "independent map oracle, insert read-back and final-key read-back; separate from timing",
        "cases": len(rows), "upstreams": json.loads((ROOT / "upstream.lock.json").read_text()),
        "binaries_sha256": {name: hashlib.sha256((build / name).read_bytes()).hexdigest()
                            for name in ("generate", "string_benchmark")},
        "inputs_sha256": {str(path.relative_to(run_dir)): hashlib.sha256(path.read_bytes()).hexdigest()
                          for path in sorted((run_dir / "data").iterdir())},
        "limits": ["Small datasets; no CPU pinning or process isolation",
                   "Reported index bytes do not include all string storage or process RSS"],
    }
    (run_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    (ROOT / "results/latest-smoke.txt").write_text(str(run_dir.relative_to(ROOT)) + "\n")
    print(f"PASS: {len(rows)} cases; {run_dir / 'summary.csv'}")


if __name__ == "__main__":
    main()
