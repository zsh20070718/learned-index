"""Frozen binary interchange writers."""

import hashlib
import struct
from pathlib import Path
from typing import Optional, Sequence

from .workload import Operation, Query


ROWS_MAGIC = b"SIBROW1\x00"
QUERIES_MAGIC = b"SIBQRY1\x00"
OPERATIONS_MAGIC = b"SIBOPS1\x00"
UINT64_MAX = (1 << 64) - 1


def write_rows(
    path: Path, keys: Sequence[bytes], values: Optional[Sequence[int]] = None
) -> str:
    if values is not None and len(values) != len(keys):
        raise ValueError("row keys and values must have the same length")
    row_values = range(len(keys)) if values is None else values
    digest = hashlib.sha256()
    with path.open("xb") as stream:
        def emit(data: bytes) -> None:
            stream.write(data)
            digest.update(data)

        emit(ROWS_MAGIC)
        emit(struct.pack("<Q", len(keys)))
        for key, value in zip(keys, row_values):
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= UINT64_MAX:
                raise ValueError("row value must be a uint64")
            emit(struct.pack("<Q", len(key)))
            emit(key)
            emit(struct.pack("<Q", value))
    return digest.hexdigest()


def write_queries(path: Path, queries: Sequence[Query]) -> str:
    digest = hashlib.sha256()
    with path.open("xb") as stream:
        def emit(data: bytes) -> None:
            stream.write(data)
            digest.update(data)

        emit(QUERIES_MAGIC)
        emit(struct.pack("<Q", len(queries)))
        for key, present, expected_value in queries:
            emit(struct.pack("<BQQ", 1 if present else 0, expected_value, len(key)))
            emit(key)
    return digest.hexdigest()


def write_operations(path: Path, operations: Sequence[Operation]) -> str:
    digest = hashlib.sha256()
    with path.open("xb") as stream:
        def emit(data: bytes) -> None:
            stream.write(data)
            digest.update(data)

        emit(OPERATIONS_MAGIC)
        emit(struct.pack("<Q", len(operations)))
        for operation in operations:
            if not isinstance(operation, tuple) or len(operation) != 4:
                raise ValueError("operation must be a kind/key/value/limit tuple")
            kind, key, value, limit = operation
            if isinstance(kind, bool) or not isinstance(kind, int) or kind not in range(1, 6):
                raise ValueError("operation kind is invalid")
            if not isinstance(key, bytes):
                raise ValueError("operation key must be bytes")
            for item, name in ((value, "value"), (limit, "limit")):
                if isinstance(item, bool) or not isinstance(item, int) or not 0 <= item <= UINT64_MAX:
                    raise ValueError("operation {} must be a uint64".format(name))
            if (kind not in (2, 3) and value != 0) or (kind != 5 and limit != 0):
                raise ValueError("operation has a nonzero unused field")
            emit(struct.pack("<BQQQ", kind, value, limit, len(key)))
            emit(key)
    return digest.hexdigest()
