"""Frozen binary interchange writers."""

import hashlib
import struct
from pathlib import Path
from typing import Sequence

from .workload import Query


ROWS_MAGIC = b"SIBROW1\x00"
QUERIES_MAGIC = b"SIBQRY1\x00"


def write_rows(path: Path, keys: Sequence[bytes]) -> str:
    digest = hashlib.sha256()
    with path.open("xb") as stream:
        def emit(data: bytes) -> None:
            stream.write(data)
            digest.update(data)

        emit(ROWS_MAGIC)
        emit(struct.pack("<Q", len(keys)))
        for value, key in enumerate(keys):
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
