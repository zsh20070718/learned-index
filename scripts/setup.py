#!/usr/bin/env python3
"""Fetch locked references, prepare local Boost headers, build with bounded jobs."""
import argparse
import json
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def run(*argv, cwd=ROOT, timeout=300):
    print("+", " ".join(map(str, argv)), flush=True)
    subprocess.run(list(map(str, argv)), cwd=cwd, check=True, timeout=timeout)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jobs", type=int, choices=range(1, 5), default=2)
    parser.add_argument("--offline", action="store_true", help="Use existing sources and headers only")
    args = parser.parse_args()
    for command in ("git", "cmake", "ninja", "c++"):
        if not shutil.which(command):
            parser.error(f"Missing tool: {command}")
    available = next(int(line.split()[1]) * 1024 for line in
                     Path("/proc/meminfo").read_text().splitlines() if line.startswith("MemAvailable:"))
    if available < 4 * 1024**3 or shutil.disk_usage(ROOT).free < 1024**3:
        parser.error("Setup requires 4 GiB available RAM and 1 GiB free disk")
    print(f"Preflight: {available // 1024**3} GiB available RAM; jobs={args.jobs}")
    for name, spec in json.loads((ROOT / "upstream.lock.json").read_text()).items():
        dest = ROOT / "upstream" / name
        if not (dest / ".git").is_dir():
            if args.offline:
                parser.error(f"Offline source missing: {dest}")
            if dest.exists() and any(dest.iterdir()):
                parser.error(f"Refusing to overwrite nonempty directory: {dest}")
            dest.mkdir(parents=True, exist_ok=True)
            run("git", "init", dest)
            run("git", "remote", "add", "origin", spec["url"], cwd=dest)
            run("git", "fetch", "--depth", "1", "origin", spec["commit"], cwd=dest)
            run("git", "checkout", "--detach", "FETCH_HEAD", cwd=dest)
        head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=dest, text=True).strip()
        dirty = subprocess.check_output(["git", "status", "--porcelain"], cwd=dest, text=True).strip()
        if head != spec["commit"] or dirty:
            parser.error(f"{name} differs from the lock or has local changes; preserved without reset")

    local = ROOT / ".deps" / "boost"
    if not (local / "usr/include/boost/chrono.hpp").exists() and not Path("/usr/include/boost/chrono.hpp").exists():
        if args.offline:
            parser.error("Boost headers missing in offline mode")
        release = Path("/etc/os-release").read_text()
        if 'VERSION_ID="24.04"' not in release or not shutil.which("apt-get"):
            parser.error("Install your distribution's Boost development headers, then rerun; local bootstrap targets Ubuntu 24.04")
        debs = ROOT / ".deps" / "debs"
        debs.mkdir(parents=True, exist_ok=True)
        run("apt-get", "download", "libboost1.83-dev=1.83.0-2.1ubuntu3.2", cwd=debs, timeout=120)
        packages = list(debs.glob("libboost1.83-dev_1.83.0-2.1ubuntu3.2_*.deb"))
        if len(packages) != 1:
            parser.error("Expected exactly one downloaded Boost package")
        run("dpkg-deb", "-x", packages[0], local)
    run("cmake", "-S", ROOT, "-B", ROOT / "build", "-G", "Ninja", "-DCMAKE_BUILD_TYPE=Release")
    run("cmake", "--build", ROOT / "build", "--parallel", args.jobs)
    run(ROOT / "build/string_benchmark", "--help", timeout=10)
    run(ROOT / "build/generate", "--help", timeout=10)
    print("Build ready. Run: python3 -B scripts/smoke.py")


if __name__ == "__main__":
    main()
