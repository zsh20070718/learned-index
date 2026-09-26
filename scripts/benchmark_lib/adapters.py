"""Adapter manifest validation, isolated builds, and runner invocation."""

import json
import hashlib
import math
import re
import shutil
import struct
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .errors import BuildFailure, InputError, RunFailure


MANIFEST_NAME = "benchmark-adapter.json"
CONTRACT = "string-index/v1"
RUN_SCHEMA = "string-index-static-run/v1"
RUN_STATUSES = {"passed", "skipped", "unsupported", "invalid_input", "failed"}
SUCCESS_STATUSES = {"passed", "skipped", "unsupported"}
FAILURE_STATUSES = {"invalid_input", "failed"}
SUPPORT_VALUES = {"unsupported", "native", "adapted"}
UINT64_MAX = (1 << 64) - 1
_REQUIRED = {
    "contract",
    "id",
    "source_dir",
    "cmake_target",
    "factory_header",
    "factory_symbol",
}
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_TARGET = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_.+-]*(?:::[A-Za-z_][A-Za-z0-9_.+-]*)*$"
)
_SYMBOL = re.compile(r"^(?:::)?[A-Za-z_][A-Za-z0-9_]*(?:::[A-Za-z_][A-Za-z0-9_]*)*$")
_HEADER_PART = re.compile(r"^[A-Za-z0-9_.+-]+$")


def _object_without_duplicates(pairs: List[Tuple[str, Any]]) -> Dict[str, Any]:
    result = {}  # type: Dict[str, Any]
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate member")
        result[key] = value
    return result


@dataclass(frozen=True)
class AdapterManifest:
    requested_path: Path
    manifest_path: Path
    adapter_id: str
    source_dir: Path
    cmake_target: str
    factory_header: str
    factory_symbol: str
    raw: Dict[str, Any]


def _sha256_file(path: Path) -> Optional[str]:
    try:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            while True:
                block = stream.read(1024 * 1024)
                if not block:
                    return digest.hexdigest()
                digest.update(block)
    except (OSError, ValueError):
        return None


def initial_build_identity(
    manifest: Optional[AdapterManifest], repository_root: Path, requested: Optional[Path] = None
) -> Dict[str, Any]:
    if manifest is not None:
        manifest_path = manifest.manifest_path
        declared_identity = manifest.raw.get("source_identity")
    else:
        requested_path = Path(requested) if requested is not None else Path("")
        try:
            is_directory = requested_path.is_dir()
        except (OSError, ValueError):
            is_directory = False
        manifest_path = requested_path / MANIFEST_NAME if is_directory else requested_path
        declared_identity = None
    return {
        "schema": "string-index-build-identity/v1",
        "adapter_manifest_sha256": _sha256_file(manifest_path),
        "runner_harness_sha256": _sha256_file(repository_root / "src/unified/runner.cpp"),
        "adapter_declared_source_identity": declared_identity,
        "external_source_tree_sha256": None,
        "external_source_identity_note": (
            "External source trees are not recursively hashed; provide immutable "
            "source_identity in the adapter manifest when available."
        ),
        "compiler": {"path": None, "id": None, "version": None},
        "configuration": {
            "generator": None,
            "build_type": "Release",
            "cxx_standard": "17",
            "cxx_extensions": False,
            "cxx_flags": None,
            "cxx_release_flags": None,
            "exe_linker_flags": None,
            "cmake_arguments": ["-DCMAKE_BUILD_TYPE=Release"],
        },
        "runner_binary_sha256": None,
    }


def _cache_values(path: Path) -> Dict[str, str]:
    values = {}  # type: Dict[str, str]
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line or line.startswith(("#", "//")) or "=" not in line:
                continue
            typed_name, value = line.split("=", 1)
            name = typed_name.split(":", 1)[0]
            values[name] = value
    except OSError:
        pass
    return values


def _compiler_metadata(binary_dir: Path) -> Dict[str, Optional[str]]:
    cache = _cache_values(binary_dir / "CMakeCache.txt")
    result = {
        "path": cache.get("CMAKE_CXX_COMPILER"),
        "id": None,
        "version": None,
    }  # type: Dict[str, Optional[str]]
    compiler_files = sorted((binary_dir / "CMakeFiles").glob("*/CMakeCXXCompiler.cmake"))
    if compiler_files:
        text = compiler_files[-1].read_text(encoding="utf-8", errors="replace")
        for field, key in (
            ("CMAKE_CXX_COMPILER_ID", "id"),
            ("CMAKE_CXX_COMPILER_VERSION", "version"),
        ):
            match = re.search(
                r'^set\(' + re.escape(field) + r' "([^"]*)"\)', text, re.MULTILINE
            )
            if match:
                result[key] = match.group(1)
    return result


def _configured_identity(identity: Dict[str, Any], binary_dir: Path) -> None:
    cache = _cache_values(binary_dir / "CMakeCache.txt")
    identity["compiler"] = _compiler_metadata(binary_dir)
    configuration = identity["configuration"]
    configuration.update(
        {
            "generator": cache.get("CMAKE_GENERATOR"),
            "build_type": cache.get("CMAKE_BUILD_TYPE", "Release"),
            "cxx_flags": cache.get("CMAKE_CXX_FLAGS"),
            "cxx_release_flags": cache.get("CMAKE_CXX_FLAGS_RELEASE"),
            "exe_linker_flags": cache.get("CMAKE_EXE_LINKER_FLAGS"),
        }
    )


def _manifest_path(requested: Path) -> Path:
    path = Path(requested)
    if path.is_dir():
        return path / MANIFEST_NAME
    return path


def load_manifest(requested: Path) -> AdapterManifest:
    try:
        manifest_path = _manifest_path(Path(requested))
    except (OSError, ValueError, RuntimeError):
        raise InputError("adapter manifest path is invalid or unreadable")
    try:
        raw = json.loads(
            manifest_path.read_text(encoding="utf-8"),
            object_pairs_hook=_object_without_duplicates,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        raise InputError("adapter manifest is malformed or unreadable")
    if not isinstance(raw, dict):
        raise InputError("adapter manifest must be one object")
    if not _REQUIRED.issubset(raw):
        raise InputError("adapter manifest is missing required fields")
    for name in _REQUIRED:
        if not isinstance(raw[name], str) or not raw[name]:
            raise InputError("adapter manifest field {} must be a non-empty string".format(name))
    if raw["contract"] != CONTRACT:
        raise InputError("adapter contract must be {}".format(CONTRACT))
    if not _ID.fullmatch(raw["id"]):
        raise InputError("adapter id is invalid")
    if not _TARGET.fullmatch(raw["cmake_target"]):
        raise InputError("adapter cmake_target is invalid")
    if not _SYMBOL.fullmatch(raw["factory_symbol"]):
        raise InputError("adapter factory_symbol is not a C++ qualified identifier")

    header = raw["factory_header"]
    header_parts = header.split("/")
    if (
        header.startswith("/")
        or "\\" in header
        or any(part in ("", ".", "..") or not _HEADER_PART.fullmatch(part) for part in header_parts)
    ):
        raise InputError("adapter factory_header is invalid")

    source_text = raw["source_dir"]
    try:
        source_path = Path(source_text)
        if source_path.is_absolute():
            raise InputError("adapter source_dir must be relative to the manifest")
        source_dir = (manifest_path.parent / source_path).resolve()
        manifest_resolved = manifest_path.resolve()
        if not source_dir.is_dir() or not (source_dir / "CMakeLists.txt").is_file():
            raise InputError("adapter source_dir is not a CMake project")
    except InputError:
        raise
    except (OSError, ValueError, RuntimeError):
        raise InputError("adapter source_dir is invalid or unreadable")
    return AdapterManifest(
        requested_path=Path(requested),
        manifest_path=manifest_resolved,
        adapter_id=raw["id"],
        source_dir=source_dir,
        cmake_target=raw["cmake_target"],
        factory_header=header,
        factory_symbol=raw["factory_symbol"],
        raw=raw,
    )


def _cmake_bracket(value: str) -> str:
    equals = ""
    while "]{}]".format(equals) in value:
        equals += "="
    return "[{}[{}]{}]".format(equals, value, equals)


def cmake_project(manifest: AdapterManifest, repository_root: Path) -> str:
    source = _cmake_bracket(str(manifest.source_dir))
    runner = _cmake_bracket(str(repository_root / "src/unified/runner.cpp"))
    include = _cmake_bracket(str(repository_root / "src"))
    return """cmake_minimum_required(VERSION 3.16)
project(string_index_static_adapter LANGUAGES CXX)
set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)
set(FETCHCONTENT_FULLY_DISCONNECTED ON CACHE BOOL \"\" FORCE)
set(FETCHCONTENT_UPDATES_DISCONNECTED ON CACHE BOOL \"\" FORCE)
set(STRING_INDEX_CONTRACT_INCLUDE_DIR {include} CACHE PATH \"\" FORCE)
add_subdirectory({source} adapter-build)
if(NOT TARGET {target})
  message(FATAL_ERROR \"adapter did not define its declared target\")
endif()
add_executable(string_index_static_runner {runner})
target_include_directories(string_index_static_runner PRIVATE {include})
target_compile_definitions(string_index_static_runner PRIVATE
  SIB_ADAPTER_FACTORY_HEADER=\\\"{header}\\\"
  SIB_ADAPTER_FACTORY_SYMBOL={symbol})
target_link_libraries(string_index_static_runner PRIVATE {target})
""".format(
        source=source,
        target=manifest.cmake_target,
        runner=runner,
        include=include,
        header=manifest.factory_header,
        symbol=manifest.factory_symbol,
    )


def _run_command(command: List[str], timeout: int, cwd: Path) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            command,
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            check=False,
            text=True,
            errors="replace",
        )
    except subprocess.TimeoutExpired:
        raise BuildFailure("command timed out")
    except OSError as error:
        raise BuildFailure("could not start command: {}".format(error.strerror or "OS error"))


def build_runner(
    manifest: AdapterManifest,
    repository_root: Path,
    build_root: Path,
    build_timeout: int,
    jobs: int,
) -> Tuple[Path, Dict[str, Any]]:
    identity = initial_build_identity(manifest, repository_root)
    cmake = shutil.which("cmake")
    if not cmake:
        raise BuildFailure("cmake executable is unavailable", identity)
    source_dir = build_root / "source"
    binary_dir = build_root / "build"
    source_dir.mkdir(parents=True, exist_ok=False)
    (source_dir / "CMakeLists.txt").write_text(
        cmake_project(manifest, repository_root), encoding="utf-8"
    )
    try:
        configure = _run_command(
            [
                cmake,
                "-S",
                str(source_dir),
                "-B",
                str(binary_dir),
                "-DCMAKE_BUILD_TYPE=Release",
            ],
            build_timeout,
            source_dir,
        )
    except BuildFailure as error:
        error.build_identity = identity
        raise
    _configured_identity(identity, binary_dir)
    (build_root / "configure.log").write_text(configure.stdout, encoding="utf-8")
    if configure.returncode != 0:
        raise BuildFailure(
            "CMake configure failed with exit code {}".format(configure.returncode),
            identity,
        )
    try:
        built = _run_command(
            [
                cmake,
                "--build",
                str(binary_dir),
                "--target",
                "string_index_static_runner",
                "--parallel",
                str(jobs),
            ],
            build_timeout,
            source_dir,
        )
    except BuildFailure as error:
        error.build_identity = identity
        raise
    (build_root / "build.log").write_text(built.stdout, encoding="utf-8")
    if built.returncode != 0:
        raise BuildFailure(
            "adapter build failed with exit code {}".format(built.returncode), identity
        )
    candidates = [
        binary_dir / "string_index_static_runner",
        binary_dir / "Release/string_index_static_runner",
        binary_dir / "string_index_static_runner.exe",
        binary_dir / "Release/string_index_static_runner.exe",
    ]
    for candidate in candidates:
        if candidate.is_file():
            identity["runner_binary_sha256"] = _sha256_file(candidate)
            return candidate, identity
    raise BuildFailure("adapter build did not produce the runner executable", identity)


def _exact_object(value: Any, fields: set, location: str) -> Dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise RunFailure("runner result {} has a malformed schema".format(location))
    return value


def _uint(value: Any, location: str, positive: bool = False) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < (1 if positive else 0)
        or value > UINT64_MAX
    ):
        raise RunFailure("runner result {} has an invalid integer".format(location))
    return value


def _optional_uint(value: Any, location: str) -> Optional[int]:
    return None if value is None else _uint(value, location)


def _finite_positive(value: Any, location: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RunFailure("runner result {} has an invalid number".format(location))
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise RunFailure("runner result {} has an invalid number".format(location))
    return result


def _validate_descriptor(value: Any, completed: bool) -> None:
    if value is None:
        if completed:
            raise RunFailure("passed runner result is missing descriptor evidence")
        return
    descriptor = _exact_object(
        value,
        {
            "implementation",
            "implementation_version",
            "adapter_version",
            "adaptation_notes",
            "capabilities",
        },
        "descriptor",
    )
    for name in (
        "implementation",
        "implementation_version",
        "adapter_version",
        "adaptation_notes",
    ):
        if not isinstance(descriptor[name], str):
            raise RunFailure("runner result descriptor has an invalid field")
    if completed and any(not descriptor[name] for name in (
        "implementation", "implementation_version", "adapter_version"
    )):
        raise RunFailure("passed runner result has incomplete descriptor evidence")
    capabilities = _exact_object(
        descriptor["capabilities"],
        {
            "keys",
            "max_value",
            "bulk_load",
            "find",
            "insert",
            "update",
            "erase",
            "scan",
            "range_sum",
        },
        "descriptor capabilities",
    )
    keys = _exact_object(
        capabilities["keys"],
        {
            "min_bytes",
            "max_bytes",
            "fixed_bytes",
            "allows_nul",
            "allows_high_bit",
            "allows_prefix_pairs",
        },
        "descriptor key domain",
    )
    minimum = _uint(keys["min_bytes"], "descriptor keys.min_bytes")
    maximum = _optional_uint(keys["max_bytes"], "descriptor keys.max_bytes")
    fixed = _optional_uint(keys["fixed_bytes"], "descriptor keys.fixed_bytes")
    for name in ("allows_nul", "allows_high_bit", "allows_prefix_pairs"):
        if not isinstance(keys[name], bool):
            raise RunFailure("runner result descriptor key domain has an invalid flag")
    _uint(capabilities["max_value"], "descriptor max_value")
    supports = []
    for name in ("bulk_load", "find", "insert", "update", "erase", "scan", "range_sum"):
        if not isinstance(capabilities[name], str):
            raise RunFailure("runner result descriptor support has an invalid type")
        supports.append(capabilities[name])
    if completed:
        if any(value not in SUPPORT_VALUES for value in supports):
            raise RunFailure("passed runner result has invalid support evidence")
        if capabilities["bulk_load"] == "unsupported" or capabilities["find"] == "unsupported":
            raise RunFailure("passed runner result lacks required capabilities")
        if maximum is not None and minimum > maximum:
            raise RunFailure("passed runner result has an invalid key domain")
        if fixed is not None and (fixed < minimum or (maximum is not None and fixed > maximum)):
            raise RunFailure("passed runner result has an invalid fixed key domain")
        if "adapted" in supports and not descriptor["adaptation_notes"]:
            raise RunFailure("passed runner result lacks adaptation notes")


def _validate_memory(value: Any, completed: bool) -> None:
    if value is None:
        if completed:
            raise RunFailure("passed runner result is missing memory evidence")
        return
    memory = _exact_object(
        value,
        {
            "index_owned_bytes",
            "adapter_owned_bytes",
            "retained_input_bytes",
            "accounting_notes",
        },
        "memory",
    )
    for name in ("index_owned_bytes", "adapter_owned_bytes", "retained_input_bytes"):
        _optional_uint(memory[name], "memory {}".format(name))
    if not isinstance(memory["accounting_notes"], str) or not memory["accounting_notes"]:
        raise RunFailure("runner result memory evidence has invalid accounting notes")


def parse_runner_result(
    path: Path,
    expected_repeats: Optional[int] = None,
    expected_operations: Optional[int] = None,
    expected_latency_samples: Optional[int] = None,
    expected_rows: Optional[int] = None,
    returncode: Optional[int] = None,
) -> Dict[str, Any]:
    try:
        raw = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_object_without_duplicates,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        raise RunFailure("runner result is missing or malformed")
    raw = _exact_object(
        raw,
        {
            "schema",
            "status",
            "stage",
            "reason",
            "descriptor",
            "validation",
            "memory",
            "samples",
            "latency_samples",
            "checksum",
        },
        "top-level object",
    )
    if raw["schema"] != RUN_SCHEMA:
        raise RunFailure("runner result has an unsupported schema")
    status = raw["status"]
    if status not in RUN_STATUSES:
        raise RunFailure("runner result has an invalid status")
    if not isinstance(raw["stage"], str) or not raw["stage"]:
        raise RunFailure("runner result stage has an invalid type")
    if not isinstance(raw["reason"], str):
        raise RunFailure("runner result reason has an invalid type")
    completed = status == "passed"
    if completed and (raw["stage"] != "complete" or raw["reason"]):
        raise RunFailure("passed runner result has inconsistent completion fields")
    if not completed and not raw["reason"]:
        raise RunFailure("non-passed runner result is missing a reason")
    if returncode is not None:
        expected_statuses = SUCCESS_STATUSES if returncode == 0 else FAILURE_STATUSES
        if returncode not in (0, 1) or status not in expected_statuses:
            raise RunFailure("runner exit code and structured status are inconsistent")

    _validate_descriptor(raw["descriptor"], completed)
    _validate_memory(raw["memory"], completed)
    validation = _exact_object(
        raw["validation"],
        {
            "descriptor_checked",
            "capability_domain_checked",
            "empty_build_checked",
            "represented_boundaries_checked",
            "absent_key_checked",
            "finite_domain_full",
            "rows_checked",
            "queries_checked",
        },
        "validation evidence",
    )
    for name in (
        "descriptor_checked",
        "capability_domain_checked",
        "empty_build_checked",
        "represented_boundaries_checked",
        "absent_key_checked",
        "finite_domain_full",
    ):
        if not isinstance(validation[name], bool):
            raise RunFailure("runner result validation evidence has an invalid flag")
    rows_checked = _uint(validation["rows_checked"], "validation rows_checked")
    queries_checked = _uint(validation["queries_checked"], "validation queries_checked")

    if not isinstance(raw["samples"], list) or not isinstance(raw["latency_samples"], list):
        raise RunFailure("runner result measurement fields must be arrays")
    checksum = _uint(raw["checksum"], "checksum")
    if not completed:
        if raw["samples"] or raw["latency_samples"] or checksum != 0:
            raise RunFailure("non-passed runner result carries completed measurements")
        return raw

    if None in (expected_repeats, expected_operations, expected_latency_samples, expected_rows):
        raise RunFailure("passed runner result cannot be validated without expected run context")
    assert expected_repeats is not None
    assert expected_operations is not None
    assert expected_latency_samples is not None
    assert expected_rows is not None
    if not all(validation[name] for name in (
        "descriptor_checked",
        "capability_domain_checked",
        "empty_build_checked",
        "represented_boundaries_checked",
    )):
        raise RunFailure("passed runner result has incomplete validation evidence")
    if validation["absent_key_checked"] == validation["finite_domain_full"]:
        raise RunFailure("passed runner result lacks exact absent-key evidence")
    if rows_checked != expected_rows or queries_checked != expected_operations:
        raise RunFailure("passed runner result has incorrect validation counts")
    if len(raw["samples"]) != expected_repeats:
        raise RunFailure("passed runner result has an incorrect repeat count")

    per_repeat_latency = min(expected_latency_samples, expected_operations)
    for repeat, sample_value in enumerate(raw["samples"]):
        sample = _exact_object(
            sample_value,
            {
                "repeat",
                "build_ns",
                "latency_build_ns",
                "lookup_ns",
                "operations",
                "throughput_ops_per_second",
            },
            "throughput sample",
        )
        if _uint(sample["repeat"], "sample repeat") != repeat:
            raise RunFailure("passed runner result has an incorrect repeat index")
        _uint(sample["build_ns"], "sample build_ns")
        if per_repeat_latency == 0:
            if sample["latency_build_ns"] is not None:
                raise RunFailure("zero-latency run reports a fabricated latency build")
        else:
            _uint(sample["latency_build_ns"], "sample latency_build_ns")
        lookup_ns = _uint(sample["lookup_ns"], "sample lookup_ns", positive=True)
        operations = _uint(sample["operations"], "sample operations", positive=True)
        if operations != expected_operations:
            raise RunFailure("passed runner result has an incorrect operation count")
        throughput = _finite_positive(
            sample["throughput_ops_per_second"], "sample throughput"
        )
        expected_throughput = operations * 1.0e9 / lookup_ns
        if not math.isclose(throughput, expected_throughput, rel_tol=1e-12, abs_tol=0.0):
            raise RunFailure("passed runner result has inconsistent throughput")

    expected_latency_count = expected_repeats * per_repeat_latency
    if len(raw["latency_samples"]) != expected_latency_count:
        raise RunFailure("passed runner result has an incorrect latency count")
    for ordinal, sample_value in enumerate(raw["latency_samples"]):
        sample = _exact_object(
            sample_value,
            {"repeat", "sample", "query_index", "latency_ns"},
            "latency sample",
        )
        repeat, sample_index = divmod(ordinal, per_repeat_latency)
        if _uint(sample["repeat"], "latency repeat") != repeat:
            raise RunFailure("passed runner result has an incorrect latency repeat")
        if _uint(sample["sample"], "latency sample index") != sample_index:
            raise RunFailure("passed runner result has an incorrect latency sample index")
        query_index = _uint(sample["query_index"], "latency query index")
        quotient, remainder = divmod(expected_operations, per_repeat_latency)
        expected_query_index = sample_index * quotient + min(sample_index, remainder)
        if query_index != expected_query_index:
            raise RunFailure("passed runner result has an incorrect latency query index")
        _uint(sample["latency_ns"], "latency_ns")
    return raw


def _interchange_count(path: Path, expected_magic: bytes, kind: str) -> int:
    try:
        with path.open("rb") as stream:
            header = stream.read(16)
    except (OSError, ValueError):
        raise RunFailure("could not inspect {} interchange".format(kind))
    if len(header) != 16 or header[:8] != expected_magic:
        raise RunFailure("{} interchange header is malformed".format(kind))
    return struct.unpack("<Q", header[8:])[0]


def invoke_runner(
    executable: Path,
    rows_path: Path,
    queries_path: Path,
    run_dir: Path,
    repeats: int,
    latency_samples: int,
    run_timeout: int,
) -> Dict[str, Any]:
    expected_rows = _interchange_count(rows_path, b"SIBROW1\0", "rows")
    expected_operations = _interchange_count(
        queries_path, b"SIBQRY1\0", "queries"
    )
    run_dir.mkdir(parents=True, exist_ok=False)
    result_path = run_dir / "result.json"
    samples_path = run_dir / "runner-samples.csv"
    command = [
        str(executable),
        "--rows",
        str(rows_path),
        "--queries",
        str(queries_path),
        "--result",
        str(result_path),
        "--samples",
        str(samples_path),
        "--repeats",
        str(repeats),
        "--latency-samples",
        str(latency_samples),
    ]
    try:
        completed = subprocess.run(
            command,
            cwd=str(run_dir),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=run_timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise RunFailure("runner timed out")
    except OSError as error:
        raise RunFailure("could not start runner: {}".format(error.strerror or "OS error"))
    if completed.returncode not in (0, 1):
        raise RunFailure("runner exited with code {}".format(completed.returncode))
    return parse_runner_result(
        result_path,
        expected_repeats=repeats,
        expected_operations=expected_operations,
        expected_latency_samples=latency_samples,
        expected_rows=expected_rows,
        returncode=completed.returncode,
    )
