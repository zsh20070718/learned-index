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
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .errors import BuildFailure, InputError, RunFailure


MANIFEST_NAME = "benchmark-adapter.json"
CONTRACT = "string-index/v1"
RUN_SCHEMA = "string-index-static-run/v1"
PARALLEL_RUN_SCHEMA = "string-index-static-run/v2"
CURRENT_RUN_SCHEMA = "string-index-static-run/v3"
MIXED_RUN_SCHEMA = "string-index-mixed-run/v1"
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
_PARAMETER_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,127}$")


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


def _source_git_identity(source_dir: Path) -> Dict[str, Any]:
    """Best-effort tracked-source identity, without refreshing another checkout."""
    result = {
        "commit": None,
        "tracked_dirty": None,
        "scope": "tracked files below adapter source_dir; excludes untracked files and external dependencies",
    }  # type: Dict[str, Any]
    git = shutil.which("git")
    if git is None:
        return result
    command = [git, "--no-optional-locks", "-c", "core.fsmonitor=false", "-C", str(source_dir)]
    try:
        revision = subprocess.run(
            command + ["rev-parse", "--verify", "HEAD"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, timeout=2, check=False,
        )
        commit = revision.stdout.strip()
        if revision.returncode != 0 or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit):
            return result
        result["commit"] = commit
        changed = subprocess.run(
            command + ["diff", "--quiet", "HEAD", "--", "."],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=2, check=False,
        )
        if changed.returncode in (0, 1):
            result["tracked_dirty"] = changed.returncode == 1
    except (OSError, subprocess.TimeoutExpired):
        pass
    return result


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
    runner_sources = {}  # type: Dict[str, Optional[str]]
    unified = repository_root / "src/unified"
    try:
        paths = sorted(
            path for path in unified.iterdir()
            if path.is_file() and path.suffix in (".cpp", ".cc", ".cxx", ".h", ".hpp")
        )
    except OSError:
        paths = []
    for path in paths:
        runner_sources[path.relative_to(repository_root).as_posix()] = _sha256_file(path)
    return {
        "schema": "string-index-build-identity/v1",
        "adapter_manifest_sha256": _sha256_file(manifest_path),
        "runner_harness_sha256": _sha256_file(repository_root / "src/unified/runner.cpp"),
        "runner_header_sha256": _sha256_file(repository_root / "src/unified/runner.h"),
        "runner_sources_sha256": runner_sources,
        "index_contract_sha256": _sha256_file(repository_root / "src/index_contract.h"),
        "adapter_declared_source_identity": declared_identity,
        "external_source_git": _source_git_identity(manifest.source_dir) if manifest else None,
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
    runner_sources = sorted((repository_root / "src/unified").glob("*.cpp"))
    runner = " ".join(_cmake_bracket(str(path)) for path in runner_sources)
    include = _cmake_bracket(str(repository_root / "src"))
    return """cmake_minimum_required(VERSION 3.16)
project(string_index_static_adapter LANGUAGES CXX)
set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)
set(FETCHCONTENT_FULLY_DISCONNECTED ON CACHE BOOL \"\" FORCE)
set(FETCHCONTENT_UPDATES_DISCONNECTED ON CACHE BOOL \"\" FORCE)
set(STRING_INDEX_CONTRACT_INCLUDE_DIR {include} CACHE PATH \"\" FORCE)
find_package(Threads REQUIRED)
add_subdirectory({source} adapter-build)
if(NOT TARGET {target})
  message(FATAL_ERROR \"adapter did not define its declared target\")
endif()
add_executable(string_index_static_runner {runner})
target_include_directories(string_index_static_runner PRIVATE {include})
target_compile_definitions(string_index_static_runner PRIVATE
  SIB_ADAPTER_FACTORY_HEADER=\\\"{header}\\\"
  SIB_ADAPTER_FACTORY_SYMBOL={symbol})
target_link_libraries(string_index_static_runner PRIVATE {target} Threads::Threads)
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


def _validate_parameter_pairs(parameters: Sequence[Tuple[str, str]]) -> List[Tuple[str, str]]:
    result = []  # type: List[Tuple[str, str]]
    seen = set()
    for item in parameters:
        if not isinstance(item, tuple) or len(item) != 2:
            raise RunFailure("runner parameters are malformed")
        name, value = item
        if not isinstance(name, str) or _PARAMETER_NAME.fullmatch(name) is None:
            raise RunFailure("runner parameter name is malformed")
        if not isinstance(value, str):
            raise RunFailure("runner parameter value is malformed")
        if name in seen:
            raise RunFailure("runner parameter name is duplicated")
        seen.add(name)
        result.append((name, value))
    return result


def _validate_parameters(value: Any, expected: Sequence[Tuple[str, str]]) -> None:
    if not isinstance(value, list):
        raise RunFailure("runner result parameters must be an array")
    actual = []  # type: List[Tuple[str, str]]
    seen = set()
    for item in value:
        parameter = _exact_object(item, {"name", "value"}, "parameter")
        name, parameter_value = parameter["name"], parameter["value"]
        if (
            not isinstance(name, str)
            or _PARAMETER_NAME.fullmatch(name) is None
            or not isinstance(parameter_value, str)
            or name in seen
        ):
            raise RunFailure("runner result has malformed parameters")
        seen.add(name)
        actual.append((name, parameter_value))
    if actual != list(expected):
        raise RunFailure("runner result parameters do not match the invocation")


def _validate_descriptor(value: Any, completed: bool, parallel_schema: bool = False) -> None:
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
        ({"concurrent_find"} if parallel_schema else set()) | {
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
    if parallel_schema and not isinstance(capabilities["concurrent_find"], bool):
        raise RunFailure("runner result concurrent_find capability is not boolean")
    supports = []
    for name in ("bulk_load", "find", "insert", "update", "erase", "scan", "range_sum"):
        if not isinstance(capabilities[name], str):
            raise RunFailure("runner result descriptor support has an invalid type")
        supports.append(capabilities[name])
    if any(value not in SUPPORT_VALUES for value in supports):
        raise RunFailure("runner result has invalid support evidence")
    if maximum is not None and minimum > maximum:
        raise RunFailure("runner result has an invalid key domain")
    if fixed is not None and (fixed < minimum or (maximum is not None and fixed > maximum)):
        raise RunFailure("runner result has an invalid fixed key domain")
    if "adapted" in supports and not descriptor["adaptation_notes"]:
        raise RunFailure("runner result lacks adaptation notes")
    if completed:
        if capabilities["bulk_load"] == "unsupported" or capabilities["find"] == "unsupported":
            raise RunFailure("passed runner result lacks required capabilities")


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


def _parse_static_runner_result(
    path: Path,
    expected_repeats: Optional[int] = None,
    expected_operations: Optional[int] = None,
    expected_latency_samples: Optional[int] = None,
    expected_rows: Optional[int] = None,
    returncode: Optional[int] = None,
    expected_threads: int = 1,
    expected_parameters: Sequence[Tuple[str, str]] = (),
) -> Dict[str, Any]:
    try:
        raw = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_object_without_duplicates,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        raise RunFailure("runner result is missing or malformed")
    schema = raw.get("schema") if isinstance(raw, dict) else None
    parallel_schema = schema in (PARALLEL_RUN_SCHEMA, CURRENT_RUN_SCHEMA)
    parameter_schema = schema == CURRENT_RUN_SCHEMA
    raw = _exact_object(
        raw,
        ({"execution"} if parallel_schema else set())
        | ({"parameters"} if parameter_schema else set()) | {
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
    if raw["schema"] not in (RUN_SCHEMA, PARALLEL_RUN_SCHEMA, CURRENT_RUN_SCHEMA):
        raise RunFailure("runner result has an unsupported schema")
    if parameter_schema:
        _validate_parameters(raw["parameters"], expected_parameters)
    elif expected_parameters:
        raise RunFailure("historical runner result cannot certify parameters")
    if parallel_schema:
        execution = _exact_object(
            raw["execution"], {"threads", "mode", "partition", "latency_mode"}, "execution"
        )
        threads = _uint(execution["threads"], "execution threads", positive=True)
        if threads != expected_threads or threads > 256:
            raise RunFailure("runner result has an incorrect thread count")
        if (
            execution["mode"] != ("serial" if threads == 1 else "shared-index-read-only")
            or execution["partition"] != "contiguous-balanced"
            or execution["latency_mode"] != "separate-single-thread"
        ):
            raise RunFailure("runner result has inconsistent execution semantics")
    elif expected_threads != 1:
        raise RunFailure("v1 runner cannot certify parallel execution")
    status = raw["status"]
    if not isinstance(status, str) or status not in RUN_STATUSES:
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

    _validate_descriptor(raw["descriptor"], completed, parallel_schema)
    _validate_memory(raw["memory"], completed)
    validation = _exact_object(
        raw["validation"],
        ({"concurrent_find_checked", "concurrent_queries_checked"} if parallel_schema else set()) | {
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
    if parallel_schema:
        if not isinstance(validation["concurrent_find_checked"], bool):
            raise RunFailure("runner result concurrent validation flag is not boolean")
        _uint(validation["concurrent_queries_checked"], "concurrent_queries_checked")

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
    if parallel_schema:
        parallel = expected_threads > 1
        if expected_threads > expected_operations:
            raise RunFailure("passed runner result has more workers than operations")
        if (
            validation["concurrent_find_checked"] != parallel
            or validation["concurrent_queries_checked"] != (expected_operations if parallel else 0)
            or (parallel and not raw["descriptor"]["capabilities"]["concurrent_find"])
        ):
            raise RunFailure("passed runner result lacks concurrent correctness evidence")
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
            ({"workers"} if parallel_schema else set()) | {
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
        if parallel_schema:
            workers = sample["workers"]
            if not isinstance(workers, list) or len(workers) != expected_threads:
                raise RunFailure("passed runner result has an incorrect worker count")
            quotient, remainder = divmod(expected_operations, expected_threads)
            for worker_index, worker_value in enumerate(workers):
                worker = _exact_object(
                    worker_value, {"worker", "operations", "lookup_ns", "checksum"}, "worker"
                )
                if (
                    _uint(worker["worker"], "worker index") != worker_index
                    or _uint(worker["operations"], "worker operations", positive=True)
                    != quotient + (worker_index < remainder)
                ):
                    raise RunFailure("passed runner result has an incorrect worker partition")
                worker_ns = _uint(worker["lookup_ns"], "worker lookup_ns")
                if worker_ns > lookup_ns:
                    raise RunFailure("worker duration exceeds total wall time")
                _uint(worker["checksum"], "worker checksum")

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


def _parse_mixed_runner_result(
    path: Path,
    expected_repeats: Optional[int] = None,
    expected_operations: Optional[int] = None,
    expected_latency_samples: Optional[int] = None,
    expected_rows: Optional[int] = None,
    returncode: Optional[int] = None,
    expected_threads: int = 1,
    expected_parameters: Sequence[Tuple[str, str]] = (),
) -> Dict[str, Any]:
    try:
        raw = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_object_without_duplicates,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        raise RunFailure("runner result is missing or malformed")
    raw = _exact_object(raw, {
        "schema", "status", "stage", "reason", "parameters", "execution",
        "descriptor", "validation", "memory", "configured_operations", "outcomes",
        "state", "samples", "latency_samples", "checksum",
    }, "top-level object")
    if raw["schema"] != MIXED_RUN_SCHEMA:
        raise RunFailure("runner result has an unsupported schema")
    _validate_parameters(raw["parameters"], expected_parameters)

    status = raw["status"]
    if not isinstance(status, str) or status not in RUN_STATUSES:
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

    execution = _exact_object(
        raw["execution"], {"threads", "mode", "partition", "latency_mode"}, "execution"
    )
    threads = _uint(execution["threads"], "execution threads", positive=True)
    expected_mode = "serial" if threads == 1 else "unsupported-mixed-parallel"
    if (
        threads != expected_threads
        or threads > 256
        or execution["mode"] != expected_mode
        or execution["partition"] != "serial-trace"
        or execution["latency_mode"] != "disabled"
    ):
        raise RunFailure("runner result has inconsistent mixed execution semantics")
    if completed and threads != 1:
        raise RunFailure("passed mixed result claims parallel execution")

    _validate_descriptor(raw["descriptor"], completed, parallel_schema=True)
    _validate_memory(raw["memory"], completed)
    validation = _exact_object(raw["validation"], {
        "descriptor_checked", "capability_domain_checked", "empty_build_checked",
        "represented_boundaries_checked", "trace_replay_checked", "affected_keys_checked",
        "final_state_checked", "initial_rows_checked", "operations_checked",
        "touched_keys_checked",
    }, "validation evidence")
    flag_names = (
        "descriptor_checked", "capability_domain_checked", "empty_build_checked",
        "represented_boundaries_checked", "trace_replay_checked", "affected_keys_checked",
        "final_state_checked",
    )
    for name in flag_names:
        if not isinstance(validation[name], bool):
            raise RunFailure("runner result validation evidence has an invalid flag")
    initial_rows_checked = _uint(validation["initial_rows_checked"], "initial_rows_checked")
    operations_checked = _uint(validation["operations_checked"], "operations_checked")
    touched_keys_checked = _uint(validation["touched_keys_checked"], "touched_keys_checked")

    configured = _exact_object(
        raw["configured_operations"], {"find", "insert", "update", "erase", "scan", "total"},
        "configured operations",
    )
    configured_values = {
        name: _uint(configured[name], "configured operation {}".format(name))
        for name in ("find", "insert", "update", "erase", "scan", "total")
    }
    if sum(configured_values[name] for name in ("find", "insert", "update", "erase", "scan")) != configured_values["total"]:
        raise RunFailure("runner result has inconsistent configured operation counts")
    if expected_operations is not None and configured_values["total"] != expected_operations:
        raise RunFailure("runner result has an incorrect configured operation count")

    outcome_names = (
        "find_hits", "find_misses", "insert_inserted", "insert_already_exists",
        "update_updated", "update_not_found", "erase_erased", "erase_not_found",
        "scan_calls", "scan_rows",
    )
    outcomes = _exact_object(raw["outcomes"], set(outcome_names), "outcomes")
    outcome_values = {name: _uint(outcomes[name], "outcome {}".format(name)) for name in outcome_names}
    state = _exact_object(
        raw["state"], {"initial_key_count", "final_key_count", "touched_keys"}, "state"
    )
    initial_key_count = _uint(state["initial_key_count"], "initial key count")
    final_key_count = _optional_uint(state["final_key_count"], "final key count")
    touched_keys = _uint(state["touched_keys"], "touched keys")
    if not isinstance(raw["samples"], list) or not isinstance(raw["latency_samples"], list):
        raise RunFailure("runner result measurement fields must be arrays")
    checksum = _uint(raw["checksum"], "checksum")
    if expected_rows is not None and initial_key_count != expected_rows:
        raise RunFailure("runner result has an incorrect initial key count")

    if not completed:
        if raw["samples"] or raw["latency_samples"] or checksum != 0:
            raise RunFailure("non-passed runner result carries completed measurements")
        if any(outcome_values.values()) or final_key_count is not None:
            raise RunFailure("non-passed runner result carries fabricated mixed outcomes")
        return raw

    if None in (expected_repeats, expected_operations, expected_latency_samples, expected_rows):
        raise RunFailure("passed runner result cannot be validated without expected run context")
    assert expected_repeats is not None
    assert expected_operations is not None
    assert expected_latency_samples is not None
    assert expected_rows is not None
    if expected_latency_samples != 0 or raw["latency_samples"]:
        raise RunFailure("mixed runner result carries latency measurements")
    if not all(validation[name] for name in flag_names):
        raise RunFailure("passed runner result has incomplete validation evidence")
    if (
        initial_rows_checked != expected_rows
        or operations_checked != expected_operations
        or initial_key_count != expected_rows
        or touched_keys_checked != touched_keys
    ):
        raise RunFailure("passed runner result has incorrect validation counts")
    if (
        outcome_values["find_hits"] + outcome_values["find_misses"] != configured_values["find"]
        or outcome_values["insert_inserted"] + outcome_values["insert_already_exists"] != configured_values["insert"]
        or outcome_values["update_updated"] + outcome_values["update_not_found"] != configured_values["update"]
        or outcome_values["erase_erased"] + outcome_values["erase_not_found"] != configured_values["erase"]
        or outcome_values["scan_calls"] != configured_values["scan"]
    ):
        raise RunFailure("passed runner result has fabricated mixed outcome counts")
    expected_final = initial_key_count + outcome_values["insert_inserted"] - outcome_values["erase_erased"]
    if final_key_count != expected_final:
        raise RunFailure("passed runner result has an inconsistent final key count")
    if len(raw["samples"]) != expected_repeats:
        raise RunFailure("passed runner result has an incorrect repeat count")
    for repeat, sample_value in enumerate(raw["samples"]):
        sample = _exact_object(sample_value, {
            "repeat", "build_ns", "latency_build_ns", "lookup_ns", "operations",
            "throughput_ops_per_second", "workers",
        }, "throughput sample")
        if _uint(sample["repeat"], "sample repeat") != repeat:
            raise RunFailure("passed runner result has an incorrect repeat index")
        _uint(sample["build_ns"], "sample build_ns")
        if sample["latency_build_ns"] is not None:
            raise RunFailure("mixed runner result carries a fabricated latency build")
        lookup_ns = _uint(sample["lookup_ns"], "sample lookup_ns", positive=True)
        operations = _uint(sample["operations"], "sample operations", positive=True)
        if operations != expected_operations:
            raise RunFailure("passed runner result has an incorrect operation count")
        throughput = _finite_positive(sample["throughput_ops_per_second"], "sample throughput")
        if not math.isclose(throughput, operations * 1.0e9 / lookup_ns, rel_tol=1e-12, abs_tol=0.0):
            raise RunFailure("passed runner result has inconsistent throughput")
        workers = sample["workers"]
        if not isinstance(workers, list) or len(workers) != 1:
            raise RunFailure("passed mixed result has an incorrect worker count")
        worker = _exact_object(workers[0], {"worker", "operations", "lookup_ns", "checksum"}, "worker")
        if (
            _uint(worker["worker"], "worker index") != 0
            or _uint(worker["operations"], "worker operations", positive=True) != expected_operations
            or _uint(worker["lookup_ns"], "worker lookup_ns") > lookup_ns
        ):
            raise RunFailure("passed mixed result has inconsistent worker evidence")
        _uint(worker["checksum"], "worker checksum")
    return raw


def parse_runner_result(
    path: Path,
    expected_repeats: Optional[int] = None,
    expected_operations: Optional[int] = None,
    expected_latency_samples: Optional[int] = None,
    expected_rows: Optional[int] = None,
    returncode: Optional[int] = None,
    expected_threads: int = 1,
    expected_parameters: Sequence[Tuple[str, str]] = (),
) -> Dict[str, Any]:
    try:
        schema = json.loads(path.read_text(encoding="utf-8")).get("schema")
    except (OSError, UnicodeError, json.JSONDecodeError, AttributeError):
        schema = None
    parser = _parse_mixed_runner_result if schema == MIXED_RUN_SCHEMA else _parse_static_runner_result
    return parser(
        path, expected_repeats, expected_operations, expected_latency_samples,
        expected_rows, returncode, expected_threads, expected_parameters,
    )


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
    workload_path: Path,
    run_dir: Path,
    repeats: int,
    latency_samples: int,
    run_timeout: int,
    threads: int = 1,
    parameters: Sequence[Tuple[str, str]] = (),
    mixed: bool = False,
) -> Dict[str, Any]:
    parameter_pairs = _validate_parameter_pairs(parameters)
    expected_rows = _interchange_count(rows_path, b"SIBROW1\0", "rows")
    expected_operations = _interchange_count(
        workload_path,
        b"SIBOPS1\0" if mixed else b"SIBQRY1\0",
        "operations" if mixed else "queries",
    )
    run_dir.mkdir(parents=True, exist_ok=False)
    result_path = run_dir / "result.json"
    samples_path = run_dir / "runner-samples.csv"
    command = [
        str(executable),
        "--rows",
        str(rows_path),
        "--operations" if mixed else "--queries",
        str(workload_path),
        "--result",
        str(result_path),
        "--samples",
        str(samples_path),
        "--repeats",
        str(repeats),
        "--latency-samples",
        str(latency_samples),
        "--threads",
        str(threads),
    ]
    for name, value in parameter_pairs:
        command.extend(["--parameter", "{}={}".format(name, value)])
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
        expected_threads=threads,
        expected_parameters=parameter_pairs,
    )
