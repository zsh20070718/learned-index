"""Versioned deterministic static uniform point workloads."""

import hashlib
import json
import random
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, localcontext
from fractions import Fraction
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .errors import InputError, ResourceBudget


SCHEMA = "static-uniform-point/v1"
DEFAULT_SEED = 42
DEFAULT_OPERATIONS = 10000
DEFAULT_MISS_RATIO = Fraction(1, 10)
DEFAULT_REPEATS = 3
DEFAULT_LATENCY_SAMPLES = 1000
_FIELDS = {
    "schema",
    "seed",
    "operations",
    "miss_ratio",
    "repeats",
    "latency_samples",
}


def _object_without_duplicates(pairs: List[Tuple[str, Any]]) -> Dict[str, Any]:
    result = {}  # type: Dict[str, Any]
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate member")
        result[key] = value
    return result


def _integer(value: Any, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise InputError("workload {} must be an integer".format(name))
    if value < minimum or value > maximum:
        raise InputError("workload {} is out of range".format(name))
    return value


def _ratio(value: Any) -> Fraction:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise InputError("workload miss_ratio must be a decimal between 0 and 1")
    text = str(value)
    if len(text) > 64:
        raise InputError("workload miss_ratio has excessive precision")
    try:
        decimal = Decimal(text)
    except (InvalidOperation, ValueError):
        raise InputError("workload miss_ratio is not a valid decimal")
    if not decimal.is_finite() or decimal < 0 or decimal > 1:
        raise InputError("workload miss_ratio must be between 0 and 1")
    decimal_tuple = decimal.as_tuple()
    if len(decimal_tuple.digits) > 32 or abs(decimal_tuple.exponent) > 32:
        raise InputError("workload miss_ratio has excessive precision")
    return Fraction(decimal)


@dataclass(frozen=True)
class WorkloadConfig:
    seed: int = DEFAULT_SEED
    operations: int = DEFAULT_OPERATIONS
    miss_ratio: Fraction = DEFAULT_MISS_RATIO
    repeats: int = DEFAULT_REPEATS
    latency_samples: int = DEFAULT_LATENCY_SAMPLES
    source: Optional[Path] = None

    def metadata(self, actual_operations: int, actual_misses: int) -> Dict[str, Any]:
        with localcontext() as context:
            context.prec = 80
            decimal_ratio = Decimal(self.miss_ratio.numerator) / Decimal(
                self.miss_ratio.denominator
            )
        return {
            "schema": SCHEMA,
            "seed": self.seed,
            "operations": self.operations,
            "actual_operations": actual_operations,
            "miss_ratio_decimal": str(decimal_ratio),
            "controlled_misses": actual_misses,
            "repeats": self.repeats,
            "latency_samples": self.latency_samples,
            "source": str(self.source) if self.source else None,
            "miss_key_strategy": "source key plus deterministic ASCII suffix and counter",
        }


def load_workload(path: Optional[Path]) -> WorkloadConfig:
    if path is None:
        return WorkloadConfig()
    source = Path(path)
    try:
        raw = json.loads(
            source.read_text(encoding="utf-8"),
            object_pairs_hook=_object_without_duplicates,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
            parse_float=str,
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        raise InputError("workload JSON is malformed or unreadable")
    if not isinstance(raw, dict):
        raise InputError("workload JSON must be one object")
    unknown = sorted(set(raw) - _FIELDS)
    if unknown:
        raise InputError("workload JSON contains unsupported fields")
    if raw.get("schema", SCHEMA) != SCHEMA:
        raise InputError("workload schema must be {}".format(SCHEMA))
    return WorkloadConfig(
        seed=_integer(raw.get("seed", DEFAULT_SEED), "seed", 0, (1 << 64) - 1),
        operations=_integer(
            raw.get("operations", DEFAULT_OPERATIONS),
            "operations",
            1,
            (1 << 63) - 1,
        ),
        miss_ratio=_ratio(raw.get("miss_ratio", "0.1")),
        repeats=_integer(raw.get("repeats", DEFAULT_REPEATS), "repeats", 1, 1000),
        latency_samples=_integer(
            raw.get("latency_samples", DEFAULT_LATENCY_SAMPLES),
            "latency_samples",
            0,
            (1 << 31) - 1,
        ),
        source=source.resolve(),
    )


Query = Tuple[bytes, bool, int]


def _missing_key(
    existing: set, source: bytes, seed: int, ordinal: int, remaining_bytes: int
) -> bytes:
    counter = 0
    while True:
        identity = hashlib.sha256(
            seed.to_bytes(8, "little") + ordinal.to_bytes(8, "little")
        ).hexdigest()[:16].encode("ascii")
        suffix = b"~sib-miss-" + identity + b"-" + str(counter).encode("ascii")
        if len(source) + len(suffix) + 80 > remaining_bytes:
            raise ResourceBudget(
                "workload generation exceeds --memory-budget-mib"
            )
        candidate = source + suffix
        if candidate not in existing:
            return candidate
        counter += 1


def generate_queries(
    keys: Sequence[bytes], config: WorkloadConfig, memory_budget_bytes: int
) -> Tuple[List[Query], int]:
    if not keys:
        return [], 0
    operation_count = config.operations
    miss_count = (operation_count * config.miss_ratio.numerator) // config.miss_ratio.denominator
    # Account for source indices, the miss-position set, query tuples/list
    # references, the existing-key set, and generated miss byte strings.
    estimated = operation_count * 192 + len(keys) * 64
    if estimated > memory_budget_bytes:
        raise ResourceBudget("workload generation exceeds --memory-budget-mib")

    source_rng = random.Random(config.seed ^ 0x5349424849545352)
    position_rng = random.Random(config.seed ^ 0x5349424D49535350)
    source_indices = [source_rng.randrange(len(keys)) for _ in range(operation_count)]
    miss_positions = set(position_rng.sample(range(operation_count), miss_count))
    existing = set(keys)
    queries = []  # type: List[Query]
    miss_ordinal = 0
    for position, source_index in enumerate(source_indices):
        if position in miss_positions:
            candidate = _missing_key(
                existing,
                keys[source_index],
                config.seed,
                miss_ordinal,
                memory_budget_bytes - estimated,
            )
            estimated += len(candidate) + 80
            queries.append(
                (
                    candidate,
                    False,
                    0,
                )
            )
            miss_ordinal += 1
        else:
            queries.append((keys[source_index], True, source_index))
    return queries, miss_count
