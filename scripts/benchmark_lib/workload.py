"""Versioned deterministic static point workloads, including skewed access."""

import bisect
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
POINT_SCHEMA = "static-point/v2"
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
_DISTRIBUTION_FIELDS = {
    "uniform": set(),
    "hotspot": {"hotset_fraction", "hot_probability"},
    "zipf": {"zipf_exponent"},
}
Query = Tuple[bytes, bool, int]


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


def _decimal_fraction(value: Any, name: str, maximum: int = 1) -> Fraction:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise InputError("workload {} must be a decimal between 0 and {}".format(name, maximum))
    text = str(value)
    if len(text) > 64:
        raise InputError("workload {} has excessive precision".format(name))
    try:
        decimal = Decimal(text)
    except (InvalidOperation, ValueError):
        raise InputError("workload {} is not a valid decimal".format(name))
    if not decimal.is_finite() or decimal < 0 or decimal > maximum:
        raise InputError("workload {} must be between 0 and {}".format(name, maximum))
    decimal_tuple = decimal.as_tuple()
    if len(decimal_tuple.digits) > 32 or abs(decimal_tuple.exponent) > 32:
        raise InputError("workload {} has excessive precision".format(name))
    return Fraction(decimal)


def _decimal_text(value: Fraction) -> str:
    with localcontext() as context:
        context.prec = 80
        return str(Decimal(value.numerator) / Decimal(value.denominator))


def _hotset_count(key_count: int, fraction: Fraction) -> int:
    if key_count < 2:
        raise InputError("hotspot workload requires at least two unique keys")
    count = (key_count * fraction.numerator) // fraction.denominator
    return max(1, min(key_count - 1, count))


@dataclass(frozen=True)
class WorkloadConfig:
    seed: int = DEFAULT_SEED
    operations: int = DEFAULT_OPERATIONS
    miss_ratio: Fraction = DEFAULT_MISS_RATIO
    repeats: int = DEFAULT_REPEATS
    latency_samples: int = DEFAULT_LATENCY_SAMPLES
    source: Optional[Path] = None
    schema: str = SCHEMA
    distribution: str = "uniform"
    hotset_fraction: Fraction = Fraction(1, 10)
    hot_probability: Fraction = Fraction(9, 10)
    zipf_exponent: Fraction = Fraction(11, 10)

    def metadata(
        self, actual_operations: int, actual_misses: int,
        queries: Optional[Sequence[Query]] = None, key_count: int = 0,
    ) -> Dict[str, Any]:
        metadata = {
            "schema": self.schema,
            "seed": self.seed,
            "operations": self.operations,
            "actual_operations": actual_operations,
            "miss_ratio_decimal": _decimal_text(self.miss_ratio),
            "controlled_misses": actual_misses,
            "repeats": self.repeats,
            "latency_samples": self.latency_samples,
            "source": str(self.source) if self.source else None,
            "miss_key_strategy": "source key plus deterministic ASCII suffix and counter",
        }
        if self.schema == POINT_SCHEMA:
            parameters = {}  # type: Dict[str, Any]
            if self.distribution == "hotspot":
                parameters = {
                    "hotset_fraction_decimal": _decimal_text(self.hotset_fraction),
                    "hot_probability_decimal": _decimal_text(self.hot_probability),
                    "hotset_definition": "first clamped floor(N * fraction) byte-sorted keys; 1..N-1",
                }
            elif self.distribution == "zipf":
                parameters = {
                    "zipf_exponent_decimal": _decimal_text(self.zipf_exponent),
                    "rank_definition": "one-based rank in byte-sorted unique keys",
                    "sampling": "finite floating-point cumulative weights r^-exponent; zero uses uniform",
                }
            metadata.update({
                "distribution": self.distribution,
                "distribution_parameters": parameters,
                "access_model": "synthetic source-key popularity, not measured traffic",
                "observed": (
                    _observed_access(queries, key_count, self)
                    if queries is not None else None
                ),
            })
        return metadata


def _observed_access(
    queries: Sequence[Query], key_count: int, config: WorkloadConfig,
) -> Dict[str, Any]:
    # These source-index sets fit the storage released after query generation
    # (source_indices, miss_positions and existing-key set).
    hit_keys = set()
    hit_count = 0
    hot_hits = 0
    rank_one_hits = 0
    hot_count = (
        _hotset_count(key_count, config.hotset_fraction)
        if config.distribution == "hotspot" and key_count >= 2 else None
    )
    for _key, present, value in queries:
        if present:
            hit_count += 1
            hit_keys.add(value)
            if hot_count is not None and value < hot_count:
                hot_hits += 1
            if value == 0:
                rank_one_hits += 1
    observed = {
        "hit_queries": hit_count,
        "miss_queries": len(queries) - hit_count,
        "unique_hit_keys": len(hit_keys),
    }
    if config.distribution == "hotspot":
        observed.update({
            "hotset_key_count": hot_count,
            "hotset_hit_queries": hot_hits,
            "hotset_hit_fraction": hot_hits / hit_count if hit_count else None,
        })
    elif config.distribution == "zipf":
        observed.update({
            "rank_one_hit_queries": rank_one_hits,
            "rank_one_hit_fraction": rank_one_hits / hit_count if hit_count else None,
        })
    return observed


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
    schema = raw.get("schema", SCHEMA)
    if schema not in (SCHEMA, POINT_SCHEMA):
        raise InputError("workload schema must be {} or {}".format(SCHEMA, POINT_SCHEMA))
    distribution = raw.get("distribution", "uniform") if schema == POINT_SCHEMA else "uniform"
    if not isinstance(distribution, str) or distribution not in _DISTRIBUTION_FIELDS:
        raise InputError("workload distribution must be uniform, hotspot or zipf")
    allowed = _FIELDS
    if schema == POINT_SCHEMA:
        allowed = _FIELDS | {"distribution"} | _DISTRIBUTION_FIELDS[distribution]
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise InputError("workload JSON contains unsupported fields")
    hotset_fraction = _decimal_fraction(raw.get("hotset_fraction", "0.1"), "hotset_fraction")
    if not 0 < hotset_fraction < 1:
        raise InputError("workload hotset_fraction must be strictly between 0 and 1")
    return WorkloadConfig(
        seed=_integer(raw.get("seed", DEFAULT_SEED), "seed", 0, (1 << 64) - 1),
        operations=_integer(
            raw.get("operations", DEFAULT_OPERATIONS),
            "operations",
            1,
            (1 << 63) - 1,
        ),
        miss_ratio=_decimal_fraction(raw.get("miss_ratio", "0.1"), "miss_ratio"),
        repeats=_integer(raw.get("repeats", DEFAULT_REPEATS), "repeats", 1, 1000),
        latency_samples=_integer(
            raw.get("latency_samples", DEFAULT_LATENCY_SAMPLES),
            "latency_samples",
            0,
            (1 << 31) - 1,
        ),
        source=source.resolve(),
        schema=schema,
        distribution=distribution,
        hotset_fraction=hotset_fraction,
        hot_probability=_decimal_fraction(raw.get("hot_probability", "0.9"), "hot_probability"),
        zipf_exponent=_decimal_fraction(raw.get("zipf_exponent", "1.1"), "zipf_exponent", 3),
    )


def _source_indices(
    key_count: int, config: WorkloadConfig, rng: random.Random,
) -> List[int]:
    if config.distribution == "uniform" or (
        config.distribution == "zipf" and config.zipf_exponent == 0
    ):
        # Preserve v1's exact source stream, including Zipf's zero-skew limit.
        return [rng.randrange(key_count) for _ in range(config.operations)]
    if config.distribution == "hotspot":
        hot_count = _hotset_count(key_count, config.hotset_fraction)
        probability = config.hot_probability
        return [
            rng.randrange(hot_count)
            if rng.randrange(probability.denominator) < probability.numerator
            else rng.randrange(hot_count, key_count)
            for _ in range(config.operations)
        ]
    if config.distribution != "zipf":
        raise InputError("workload distribution must be uniform, hotspot or zipf")
    cumulative = []  # type: List[float]
    total = 0.0
    exponent = float(config.zipf_exponent)
    for rank in range(1, key_count + 1):
        total += rank ** -exponent
        cumulative.append(total)
    return [
        min(key_count - 1, bisect.bisect_right(cumulative, rng.random() * total))
        for _ in range(config.operations)
    ]


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
    if config.distribution == "hotspot":
        # Invalid experiment conditions remain input errors at any budget.
        _hotset_count(len(keys), config.hotset_fraction)
    operation_count = config.operations
    miss_count = (operation_count * config.miss_ratio.numerator) // config.miss_ratio.denominator
    # Account for source indices, the miss-position set, query tuples/list
    # references, the existing-key set, and generated miss byte strings.
    estimated = operation_count * 192 + len(keys) * 64
    if config.distribution == "zipf" and config.zipf_exponent != 0:
        # Python float objects, list references and list capacity for the CDF.
        estimated += len(keys) * 40
    if estimated > memory_budget_bytes:
        raise ResourceBudget("workload generation exceeds --memory-budget-mib")

    source_rng = random.Random(config.seed ^ 0x5349424849545352)
    position_rng = random.Random(config.seed ^ 0x5349424D49535350)
    source_indices = _source_indices(len(keys), config, source_rng)
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
