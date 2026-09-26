"""Canonical dataset preparation and hashing."""

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from .errors import InputError, ResourceBudget
from .readers import iter_keys, resolve_reader


LOGICAL_DOMAIN = b"string-dataset/v1\x00"


def encode_uleb128(value: int) -> bytes:
    if value < 0:
        raise ValueError("ULEB128 cannot encode a negative integer")
    encoded = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            encoded.append(byte | 0x80)
        else:
            encoded.append(byte)
            return bytes(encoded)


def logical_hash(keys: Iterable[bytes]) -> str:
    materialized = list(keys)
    digest = hashlib.sha256()
    digest.update(LOGICAL_DOMAIN)
    digest.update(encode_uleb128(len(materialized)))
    for key in materialized:
        digest.update(encode_uleb128(len(key)))
        digest.update(key)
    return digest.hexdigest()


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            block = stream.read(1024 * 1024)
            if not block:
                return digest.hexdigest()
            digest.update(block)


def _fingerprint(path: Path) -> Any:
    stat = path.stat()
    return (
        stat.st_dev,
        stat.st_ino,
        stat.st_size,
        stat.st_mtime_ns,
        stat.st_ctime_ns,
    )


@dataclass(frozen=True)
class PreparedDataset:
    path: Path
    reader: str
    key_field: Optional[str]
    keys: List[bytes]
    source_records: int
    duplicate_records: int
    source_sha256: str
    logical_sha256: str
    total_unique_key_bytes: int
    empty_keys: int
    nul_keys: int
    high_bit_keys: int
    prefix_adjacent_pairs: int
    min_key_bytes: Optional[int]
    max_key_bytes: Optional[int]
    estimated_memory_bytes: int

    def metadata(self) -> Dict[str, Any]:
        return {
            "path": str(self.path),
            "reader": self.reader,
            "key_field": self.key_field,
            "source_sha256": self.source_sha256,
            "logical_sha256": self.logical_sha256,
            "source_records": self.source_records,
            "unique_keys": len(self.keys),
            "duplicate_records": self.duplicate_records,
            "total_unique_key_bytes": self.total_unique_key_bytes,
            "empty_keys": self.empty_keys,
            "nul_keys": self.nul_keys,
            "high_bit_keys": self.high_bit_keys,
            "prefix_adjacent_pairs": self.prefix_adjacent_pairs,
            "min_key_bytes": self.min_key_bytes,
            "max_key_bytes": self.max_key_bytes,
            "normalization": "unsigned-byte lexicographic sort and exact deduplication",
            "logical_hash_format": "string-dataset/v1 with unsigned LEB128 lengths",
            "estimated_preparation_memory_bytes": self.estimated_memory_bytes,
        }


def prepare_dataset(
    path: Path,
    reader: str,
    key_field: Optional[str],
    memory_budget_bytes: int,
) -> PreparedDataset:
    dataset = Path(path)
    selected_reader = resolve_reader(dataset, reader)
    try:
        initial_fingerprint = _fingerprint(dataset)
    except OSError as error:
        raise InputError("dataset metadata read failed: {}".format(error.strerror or "I/O error"))
    raw = []  # type: List[bytes]
    estimated = 0

    def remaining_bytes() -> int:
        return memory_budget_bytes - estimated

    for key in iter_keys(
        dataset, selected_reader, key_field, remaining_bytes=remaining_bytes
    ):
        # A conservative per-object/list-reference allowance. It is an
        # execution budget, not a restriction on valid keys or datasets.
        estimated += len(key) + 80
        if estimated > memory_budget_bytes:
            raise ResourceBudget("dataset preparation exceeds --memory-budget-mib")
        raw.append(key)

    try:
        after_read_fingerprint = _fingerprint(dataset)
        source_sha256 = file_hash(dataset)
        after_hash_fingerprint = _fingerprint(dataset)
    except OSError as error:
        raise InputError("dataset provenance read failed: {}".format(error.strerror or "I/O error"))
    if not (
        initial_fingerprint == after_read_fingerprint == after_hash_fingerprint
    ):
        raise InputError("dataset changed while it was being read")

    raw.sort()
    unique = []  # type: List[bytes]
    previous = None  # type: Optional[bytes]
    for key in raw:
        if previous is None or key != previous:
            unique.append(key)
            previous = key

    # The additional list and query/build preparation headroom is charged here.
    estimated += len(unique) * 8
    if estimated > memory_budget_bytes:
        raise ResourceBudget("canonical dataset exceeds --memory-budget-mib")

    total_bytes = sum(len(key) for key in unique)
    empty_keys = sum(1 for key in unique if not key)
    nul_keys = sum(1 for key in unique if b"\x00" in key)
    high_bit_keys = sum(1 for key in unique if any(byte >= 0x80 for byte in key))
    prefix_pairs = sum(
        1
        for left, right in zip(unique, unique[1:])
        if len(left) < len(right) and right.startswith(left)
    )
    lengths = [len(key) for key in unique]
    return PreparedDataset(
        path=dataset.resolve(),
        reader=selected_reader,
        key_field=key_field,
        keys=unique,
        source_records=len(raw),
        duplicate_records=len(raw) - len(unique),
        source_sha256=source_sha256,
        logical_sha256=logical_hash(unique),
        total_unique_key_bytes=total_bytes,
        empty_keys=empty_keys,
        nul_keys=nul_keys,
        high_bit_keys=high_bit_keys,
        prefix_adjacent_pairs=prefix_pairs,
        min_key_bytes=min(lengths) if lengths else None,
        max_key_bytes=max(lengths) if lengths else None,
        estimated_memory_bytes=estimated,
    )
