#!/usr/bin/env python3
"""Exercise TLI string generation, validation and throughput in an isolated run directory."""
import argparse
import csv
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile

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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-dir", type=Path, default=ROOT / "build")
    args = parser.parse_args()
    build = args.build_dir.resolve()
    for name in ("string_benchmark", "generate"):
        if not (build / name).is_file():
            parser.error(f"Missing {name}; run scripts/setup.py first")
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
    # 10k keys, 2k operations per workload; two key shapes, single thread.
    for shape in ("random", "prefix"):
        data = f"data/smoke_{shape}_string"
        run([sys.executable, "-B", ROOT / "scripts/make_dataset.py", data, "--shape", shape],
            f"{shape}-dataset.log")
        for label, scan, insert, pattern in WORKLOADS:
            tag = f"{shape}-{label}"
            before = set((run_dir / "data").iterdir())
            run([build / "generate", data, "2000", "--scan-ratio", scan, "--insert-ratio", insert,
                 "--insert-pattern", pattern, "--hotspot-ratio", "0.5", "--mix"], f"{tag}-generate.log")
            candidates = [p for p in set((run_dir / "data").iterdir()) - before if not p.name.endswith("_bulkload")]
            if len(candidates) != 1:
                raise RuntimeError(f"Expected one generated workload, got {candidates}")
            ops = str(candidates[0].relative_to(run_dir))
            for index in ("ART", "StdMap"):
                base = [build / "string_benchmark", data, ops, "--only", index]
                verified = run([*base, "--verify"], f"{tag}-{index}-verify.log")
                if f"RESULT: {index}," not in verified:
                    raise RuntimeError(f"{tag}/{index} was skipped; see verification log")
                through = run([*base, "--through", "--csv"], f"{tag}-{index}-through.log")
                result = next(line for line in through.splitlines() if line.startswith(f"RESULT: {index},"))
                fields = result.removeprefix("RESULT: ").split(",")
                if len(fields) != 4 or float(fields[3]) <= 0:
                    raise RuntimeError(f"Invalid throughput result: {result}")
                rows.append([shape, label, index, *fields[1:], "passed"])
            print(f"PASS {tag}: ART + StdMap", flush=True)
    with (run_dir / "summary.csv").open("w", newline="") as out:
        writer = csv.writer(out)
        writer.writerow(["key_shape", "workload", "index", "build_us", "reported_index_bytes",
                         "throughput_mops_per_s", "lookup_range_verification"])
        writer.writerows(rows)
    manifest = {
        "scope": "setup smoke only; not a publication performance experiment",
        "keys_per_shape": 10000, "operations_per_workload": 2000, "threads": 1,
        "cases": len(rows), "upstreams": json.loads((ROOT / "upstream.lock.json").read_text()),
        "binaries_sha256": {name: hashlib.sha256((build / name).read_bytes()).hexdigest()
                            for name in ("generate", "string_benchmark")},
        "limits": ["Pure insertion has no lookup/range assertions in upstream --verify",
                   "Reported index bytes do not include all string storage or process RSS"],
    }
    (run_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    (ROOT / "results/latest-smoke.txt").write_text(str(run_dir.relative_to(ROOT)) + "\n")
    print(f"PASS: {len(rows)} cases; {run_dir / 'summary.csv'}")


if __name__ == "__main__":
    main()
