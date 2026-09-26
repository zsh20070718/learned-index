"""Lossless built-in dataset readers."""

import json
import os
import struct
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

from .errors import InputError, ResourceBudget


READER_NAMES = ("auto", "raw-lines", "jsonl", "legacy-tli")


def resolve_reader(path: Path, requested: str) -> str:
    if requested not in READER_NAMES:
        raise InputError("unknown reader; choose raw-lines, jsonl, or legacy-tli")
    if requested != "auto":
        return requested
    suffix = path.suffix.lower()
    if suffix in (".txt", ".keys", ".lines"):
        return "raw-lines"
    if suffix in (".jsonl", ".ndjson"):
        return "jsonl"
    if suffix == ".tli":
        return "legacy-tli"
    raise InputError("dataset format is ambiguous; specify --reader")


RemainingBytes = Optional[Callable[[], int]]


def _bounded_lines(path: Path, remaining_bytes: RemainingBytes) -> Iterator[bytes]:
    with path.open("rb") as stream:
        while True:
            first = stream.read(1)
            if not first:
                return
            stream.seek(-1, os.SEEK_CUR)
            if remaining_bytes is None:
                raw = stream.readline()
            else:
                available = remaining_bytes() - 80
                if available < 0:
                    raise ResourceBudget(
                        "dataset preparation exceeds --memory-budget-mib"
                    )
                # Read at most one key, its CRLF delimiter, and one detection
                # byte. This rejects an over-budget record before materializing
                # it while preserving arbitrarily long keys when the caller
                # supplies a sufficient budget.
                raw = stream.readline(available + 3)
            key = raw
            if key.endswith(b"\n"):
                key = key[:-1]
                if key.endswith(b"\r"):
                    key = key[:-1]
            if remaining_bytes is not None and len(key) + 80 > remaining_bytes():
                raise ResourceBudget(
                    "dataset preparation exceeds --memory-budget-mib"
                )
            yield key


def _raw_lines(path: Path, remaining_bytes: RemainingBytes) -> Iterator[bytes]:
    yield from _bounded_lines(path, remaining_bytes)


def _pairs_object(pairs: List[Tuple[str, Any]]) -> Dict[str, Any]:
    result = {}  # type: Dict[str, Any]
    for name, value in pairs:
        if name in result:
            raise ValueError("duplicate object member")
        result[name] = value
    return result


def _reject_constant(_value: str) -> Any:
    raise ValueError("non-finite JSON number")


def _json_lines(
    path: Path, key_field: Optional[str], remaining_bytes: RemainingBytes
) -> Iterator[bytes]:
    if not key_field:
        raise InputError("jsonl reader requires an explicit --key field")
    for line_number, raw in enumerate(_bounded_lines(path, remaining_bytes), 1):
        try:
            text = raw.decode("utf-8", errors="strict")
            value = json.loads(
                text,
                object_pairs_hook=_pairs_object,
                parse_constant=_reject_constant,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            raise InputError("jsonl record {} is malformed".format(line_number))
        if not isinstance(value, dict):
            raise InputError("jsonl record {} is not an object".format(line_number))
        if key_field not in value:
            raise InputError(
                "jsonl record {} is missing the selected field".format(line_number)
            )
        selected = value[key_field]
        if not isinstance(selected, str):
            raise InputError(
                "jsonl record {} selected field is not a string".format(line_number)
            )
        try:
            yield selected.encode("utf-8")
        except UnicodeEncodeError:
            raise InputError(
                "jsonl record {} selected field is not valid Unicode".format(
                    line_number
                )
            )


def _read_exact(stream: Any, size: int, description: str) -> bytes:
    data = stream.read(size)
    if len(data) != size:
        raise InputError("legacy TLI input is truncated at {}".format(description))
    return data


def _legacy_tli(path: Path, remaining_bytes: RemainingBytes) -> Iterator[bytes]:
    file_size = path.stat().st_size
    with path.open("rb") as stream:
        count = struct.unpack("<Q", _read_exact(stream, 8, "record count"))[0]
        # Even an empty key requires its uint32 length. This check prevents an
        # impossible count from causing a long loop before truncation is found.
        if count > max(0, file_size - 8) // 4:
            raise InputError("legacy TLI record count exceeds the file size")
        for index in range(count):
            length = struct.unpack(
                "<I", _read_exact(stream, 4, "record {} length".format(index))
            )[0]
            remaining = file_size - stream.tell()
            if length > remaining:
                raise InputError(
                    "legacy TLI input is truncated at record {} bytes".format(index)
                )
            if remaining_bytes is not None and length + 80 > remaining_bytes():
                raise ResourceBudget(
                    "dataset preparation exceeds --memory-budget-mib"
                )
            yield _read_exact(stream, length, "record {} bytes".format(index))
        if stream.read(1):
            raise InputError("legacy TLI input has trailing bytes")


def iter_keys(
    dataset: Path,
    reader: str = "auto",
    key_field: Optional[str] = None,
    remaining_bytes: RemainingBytes = None,
) -> Iterator[bytes]:
    path = Path(dataset)
    try:
        if not path.exists():
            raise InputError("dataset does not exist")
        if not path.is_file():
            raise InputError("the first-slice dataset must be one regular file")
        if not os.access(str(path), os.R_OK):
            raise InputError("dataset is not readable")
        selected = resolve_reader(path, reader)
        if key_field and selected != "jsonl":
            raise InputError("--key is only valid with the jsonl reader")
        if selected == "raw-lines":
            yield from _raw_lines(path, remaining_bytes)
        elif selected == "jsonl":
            yield from _json_lines(path, key_field, remaining_bytes)
        else:
            yield from _legacy_tli(path, remaining_bytes)
    except OSError as error:
        raise InputError("dataset read failed: {}".format(error.strerror or "I/O error"))
