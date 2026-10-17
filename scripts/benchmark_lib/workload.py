"""Versioned deterministic static point workloads, including skewed access."""

import bisect
import hashlib
import json
import random
import struct
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, localcontext
from fractions import Fraction
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .errors import InputError, ResourceBudget


SCHEMA = "static-uniform-point/v1"
POINT_SCHEMA = "static-point/v2"
MIXED_SCHEMA = "mixed-operations/v1"
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
Operation = Tuple[int, bytes, int, int]
OPERATION_NAMES = ("find", "insert", "update", "erase", "scan")
OPERATION_KINDS = {name: index + 1 for index, name in enumerate(OPERATION_NAMES)}
UINT64_MAX = (1 << 64) - 1


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


@dataclass(frozen=True)
class MixedWorkloadConfig:
    seed: int = DEFAULT_SEED
    operations: int = DEFAULT_OPERATIONS
    initial_load_fraction: Fraction = Fraction(1, 2)
    operation_weights: Dict[str, Fraction] = field(default_factory=lambda: {
        "find": Fraction(4, 5),
        "insert": Fraction(1, 5),
        "update": Fraction(0),
        "erase": Fraction(0),
        "scan": Fraction(0),
    })
    read_miss_ratio: Fraction = DEFAULT_MISS_RATIO
    scan_length: int = 16
    repeats: int = DEFAULT_REPEATS
    latency_samples: int = 0
    source: Optional[Path] = None
    schema: str = MIXED_SCHEMA

    def configured_counts(self) -> Dict[str, int]:
        if set(self.operation_weights) - set(OPERATION_NAMES):
            raise InputError("workload operation_weights contains unsupported operations")
        weights = [self.operation_weights.get(name, Fraction(0)) for name in OPERATION_NAMES]
        if any(not isinstance(weight, Fraction) or weight < 0 or weight > 1 for weight in weights):
            raise InputError("workload operation_weights must contain exact fractions between zero and one")
        if sum(weights, Fraction(0)) != 1:
            raise InputError("workload operation_weights must sum exactly to one")
        floors = [
            (self.operations * weight.numerator) // weight.denominator
            for weight in weights
        ]
        remainders = [
            Fraction(self.operations * weight.numerator, weight.denominator) - floor
            for weight, floor in zip(weights, floors)
        ]
        remaining = self.operations - sum(floors)
        order = sorted(range(len(floors)), key=lambda index: (-remainders[index], index))
        for index in order[:remaining]:
            floors[index] += 1
        result = {name: count for name, count in zip(OPERATION_NAMES, floors)}
        result["total"] = sum(floors)
        return result

    def metadata(self, actual_operations: int = 0, _unused: int = 0) -> Dict[str, Any]:
        return {
            "schema": self.schema,
            "seed": self.seed,
            "operations": self.operations,
            "actual_operations": actual_operations,
            "initial_load_fraction_decimal": _decimal_text(self.initial_load_fraction),
            "operation_weights_decimal": {
                name: _decimal_text(self.operation_weights.get(name, Fraction(0)))
                for name in OPERATION_NAMES
            },
            "configured_operations": self.configured_counts(),
            "read_miss_ratio_decimal": _decimal_text(self.read_miss_ratio),
            "scan_length": self.scan_length,
            "repeats": self.repeats,
            "latency_samples": self.latency_samples,
            "source": str(self.source) if self.source else None,
        }


@dataclass
class PreparedMixedWorkload:
    initial_keys: List[bytes]
    initial_values: List[int]
    held_out_keys: List[bytes]
    operations: List[Operation]
    metadata: Dict[str, Any]


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


def load_workload(path: Optional[Path]) -> Any:
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
    if schema == MIXED_SCHEMA:
        allowed = {
            "schema", "seed", "operations", "initial_load_fraction",
            "operation_weights", "read_miss_ratio", "scan_length", "repeats",
            "latency_samples",
        }
        if set(raw) - allowed:
            raise InputError("workload JSON contains unsupported fields")
        weights_raw = raw.get("operation_weights")
        if weights_raw is None:
            weights = {
                "find": Fraction(4, 5), "insert": Fraction(1, 5),
                "update": Fraction(0), "erase": Fraction(0), "scan": Fraction(0),
            }
        else:
            if not isinstance(weights_raw, dict):
                raise InputError("workload operation_weights must be an object")
            if set(weights_raw) - set(OPERATION_NAMES):
                raise InputError("workload operation_weights contains unsupported operations")
            weights = dict(
                (name, _decimal_fraction(weights_raw.get(name, 0), "operation_weights.{}".format(name)))
                for name in OPERATION_NAMES
            )
        if sum(weights.values(), Fraction(0)) != 1:
            raise InputError("workload operation_weights must sum exactly to one")
        latency_samples = _integer(raw.get("latency_samples", 0), "latency_samples", 0, (1 << 31) - 1)
        if latency_samples != 0:
            raise InputError("mixed workload latency_samples must be zero")
        return MixedWorkloadConfig(
            seed=_integer(raw.get("seed", DEFAULT_SEED), "seed", 0, UINT64_MAX),
            operations=_integer(raw.get("operations", DEFAULT_OPERATIONS), "operations", 1, (1 << 63) - 1),
            initial_load_fraction=_decimal_fraction(raw.get("initial_load_fraction", "0.5"), "initial_load_fraction"),
            operation_weights=weights,
            read_miss_ratio=_decimal_fraction(raw.get("read_miss_ratio", "0.1"), "read_miss_ratio"),
            scan_length=_integer(raw.get("scan_length", 16), "scan_length", 0, (1 << 63) - 1),
            repeats=_integer(raw.get("repeats", DEFAULT_REPEATS), "repeats", 1, 1000),
            latency_samples=latency_samples,
            source=source.resolve(),
        )
    if schema not in (SCHEMA, POINT_SCHEMA):
        raise InputError("workload schema must be {}, {} or {}".format(SCHEMA, POINT_SCHEMA, MIXED_SCHEMA))
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


class _RandomPool:
    def __init__(self, values: Sequence[bytes] = ()) -> None:
        self.values = list(values)
        self.positions = {value: index for index, value in enumerate(self.values)}

    def __len__(self) -> int:
        return len(self.values)

    def choose(self, rng: random.Random) -> bytes:
        return self.values[rng.randrange(len(self.values))]

    def add(self, value: bytes) -> None:
        if value in self.positions:
            return
        self.positions[value] = len(self.values)
        self.values.append(value)

    def remove(self, value: bytes) -> None:
        index = self.positions.pop(value)
        tail = self.values.pop()
        if index < len(self.values):
            self.values[index] = tail
            self.positions[tail] = index

    def pop_random(self, rng: random.Random) -> bytes:
        value = self.choose(rng)
        self.remove(value)
        return value


def _stream_rng(seed: int, label: str) -> random.Random:
    digest = hashlib.sha256(seed.to_bytes(8, "little") + label.encode("ascii")).digest()
    return random.Random(int.from_bytes(digest, "big"))


def _partition_digest(keys: Sequence[bytes], values: Optional[Sequence[int]] = None) -> str:
    digest = hashlib.sha256()
    if values is None:
        iterator = ((key, None) for key in keys)
    else:
        iterator = zip(keys, values)
    for key, value in iterator:
        digest.update(struct.pack("<Q", len(key)))
        digest.update(key)
        if value is not None:
            digest.update(struct.pack("<Q", value))
    return digest.hexdigest()


def _budget_limit(budget: Any) -> int:
    if isinstance(budget, bool):
        raise ResourceBudget("workload generation exceeds --memory-budget-mib")
    if isinstance(budget, int):
        return budget
    for name in ("remaining_bytes", "memory_budget_bytes", "limit_bytes", "bytes"):
        value = getattr(budget, name, None)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    raise ResourceBudget("workload generation has no usable memory budget")


def generate_mixed_operations(
    keys: Sequence[bytes], config: MixedWorkloadConfig, budget: ResourceBudget
) -> PreparedMixedWorkload:
    full_keys = sorted(set(keys))
    total_key_bytes = sum(len(key) for key in full_keys)
    estimated = total_key_bytes * 3 + len(full_keys) * 480 + config.operations * 256
    limit = _budget_limit(budget)
    if estimated > limit:
        raise ResourceBudget("workload generation exceeds --memory-budget-mib")

    counts = config.configured_counts()
    order_rng = _stream_rng(config.seed, "operation-order")
    partition_rng = _stream_rng(config.seed, "initial-partition")
    target_rng = _stream_rng(config.seed, "operation-targets")
    value_rng = _stream_rng(config.seed, "operation-values")

    initial_count = (
        len(full_keys) * config.initial_load_fraction.numerator
        // config.initial_load_fraction.denominator
    )
    selected = set(partition_rng.sample(range(len(full_keys)), initial_count))
    initial_values = [index for index in range(len(full_keys)) if index in selected]
    initial_keys = [full_keys[index] for index in initial_values]
    held_out_keys = [key for index, key in enumerate(full_keys) if index not in selected]

    kinds = []  # type: List[int]
    for name in OPERATION_NAMES:
        kinds.extend([OPERATION_KINDS[name]] * counts[name])
    order_rng.shuffle(kinds)
    find_misses = (
        counts["find"] * config.read_miss_ratio.numerator
        // config.read_miss_ratio.denominator
    )
    miss_find_ordinals = set(target_rng.sample(range(counts["find"]), find_misses))

    original_keys = set(full_keys)
    known_keys = set(full_keys)
    live = _RandomPool(initial_keys)
    absent_original = _RandomPool(held_out_keys)
    absent_synthetic = _RandomPool()
    live_values = dict(zip(initial_keys, initial_values))
    ordered_live = list(initial_keys)
    operations = []  # type: List[Operation]
    outcomes = {
        "find_hits": 0, "find_misses": 0,
        "insert_inserted": 0, "insert_already_exists": 0,
        "update_updated": 0, "update_not_found": 0,
        "erase_erased": 0, "erase_not_found": 0,
        "scan_calls": 0, "scan_rows": 0,
    }
    fixture_counts = {
        "intentional_duplicate_inserts": 0,
        "intentional_missing_updates": 0,
        "intentional_missing_erases": 0,
    }
    insert_sources = {"original": 0, "synthetic": 0}
    ordinals = {name: 0 for name in OPERATION_NAMES}
    duplicate_insert_done = missing_update_done = missing_erase_done = False
    synthetic_ordinal = 0
    generated_value_ordinal = 0
    touched = set()
    peak_live = len(live)
    peak_absent = len(absent_original)

    def new_synthetic() -> bytes:
        nonlocal synthetic_ordinal, estimated
        while True:
            digest = hashlib.sha256(
                config.seed.to_bytes(8, "little")
                + b"synthetic-key"
                + synthetic_ordinal.to_bytes(8, "little")
            ).hexdigest()[:24].encode("ascii")
            synthetic_ordinal += 1
            candidate = b"~sib-mixed-" + digest
            if candidate not in known_keys:
                estimated += len(candidate) + 160
                if estimated > limit:
                    raise ResourceBudget("workload generation exceeds --memory-budget-mib")
                known_keys.add(candidate)
                return candidate

    def choose_absent() -> bytes:
        nonlocal peak_absent
        if len(absent_original):
            return absent_original.choose(target_rng)
        if len(absent_synthetic):
            return absent_synthetic.choose(target_rng)
        candidate = new_synthetic()
        absent_synthetic.add(candidate)
        peak_absent = max(peak_absent, len(absent_original) + len(absent_synthetic))
        return candidate

    def next_value(previous: Optional[int] = None) -> int:
        nonlocal generated_value_ordinal
        if generated_value_ordinal == 0:
            value = 0
        elif generated_value_ordinal == 1:
            value = UINT64_MAX
        else:
            value = value_rng.getrandbits(64)
        generated_value_ordinal += 1
        if previous is not None and value == previous:
            value = UINT64_MAX if previous != UINT64_MAX else 0
        return value

    for kind in kinds:
        name = OPERATION_NAMES[kind - 1]
        ordinal = ordinals[name]
        ordinals[name] += 1
        value = 0
        operation_limit = config.scan_length if name == "scan" else 0

        if name == "find":
            if ordinal in miss_find_ordinals or not len(live):
                key = choose_absent()
                outcomes["find_misses"] += 1
            else:
                key = live.choose(target_rng)
                outcomes["find_hits"] += 1
        elif name == "insert":
            if counts[name] >= 2 and not duplicate_insert_done and len(live):
                key = live.choose(target_rng)
                value = next_value()
                duplicate_insert_done = True
                fixture_counts["intentional_duplicate_inserts"] += 1
                outcomes["insert_already_exists"] += 1
            else:
                if len(absent_original):
                    key = absent_original.pop_random(target_rng)
                elif len(absent_synthetic):
                    key = absent_synthetic.pop_random(target_rng)
                else:
                    key = new_synthetic()
                value = next_value()
                live.add(key)
                live_values[key] = value
                bisect.insort(ordered_live, key)
                outcomes["insert_inserted"] += 1
            insert_sources["original" if key in original_keys else "synthetic"] += 1
        elif name == "update":
            if counts[name] >= 2 and not missing_update_done:
                key = choose_absent()
                value = next_value()
                missing_update_done = True
                fixture_counts["intentional_missing_updates"] += 1
                outcomes["update_not_found"] += 1
            elif len(live):
                key = live.choose(target_rng)
                value = next_value(live_values[key])
                live_values[key] = value
                outcomes["update_updated"] += 1
            else:
                key = choose_absent()
                value = next_value()
                outcomes["update_not_found"] += 1
        elif name == "erase":
            if counts[name] >= 2 and not missing_erase_done:
                key = choose_absent()
                missing_erase_done = True
                fixture_counts["intentional_missing_erases"] += 1
                outcomes["erase_not_found"] += 1
            elif len(live):
                key = live.choose(target_rng)
                live.remove(key)
                del live_values[key]
                ordered_live.pop(bisect.bisect_left(ordered_live, key))
                (absent_original if key in original_keys else absent_synthetic).add(key)
                outcomes["erase_erased"] += 1
            else:
                key = choose_absent()
                outcomes["erase_not_found"] += 1
        else:
            if len(live):
                key = live.choose(target_rng)
            else:
                key = choose_absent()
            begin = bisect.bisect_left(ordered_live, key)
            outcomes["scan_calls"] += 1
            outcomes["scan_rows"] += min(operation_limit, len(ordered_live) - begin)

        touched.add(key)
        operations.append((kind, key, value, operation_limit))
        peak_live = max(peak_live, len(live))
        peak_absent = max(peak_absent, len(absent_original) + len(absent_synthetic))

    metadata = config.metadata(len(operations))
    metadata.update({
        "actual_result_counts": outcomes,
        "initial_key_count": len(initial_keys),
        "held_out_key_count": len(held_out_keys),
        "initial_content_sha256": _partition_digest(initial_keys, initial_values),
        "held_out_content_sha256": _partition_digest(held_out_keys),
        "initial_value_policy": "full-sorted-unique-source-rank",
        "trace_value_policy": {
            "scope": "insert/update payloads only; unrelated to initial ranks",
            "stream": "operation-values",
            "sequence": "first generated value 0; second UINT64_MAX; remaining values seeded uint64",
            "update_collision": (
                "replace an unchanged candidate with UINT64_MAX, or 0 when previous is UINT64_MAX"
            ),
        },
        "insert_attempts": insert_sources,
        "original_insert_attempts": insert_sources["original"],
        "synthetic_insert_attempts": insert_sources["synthetic"],
        "fixture_attempts": fixture_counts,
        "final_key_count": len(live),
        "touched_key_count": len(touched),
        "actual_find_hits": outcomes["find_hits"],
        "actual_find_misses": outcomes["find_misses"],
        "peak_live_key_count": peak_live,
        "peak_absent_key_count": peak_absent,
        "estimated_memory_bytes": estimated,
        "rng": {
            "derivation": "SHA-256(seed uint64LE || ASCII stream label), full digest as Python Random seed",
            "streams": ["operation-order", "initial-partition", "operation-targets", "operation-values"],
        },
        "selection": {
            "initial": "seed-selected full-source ranks, then unsigned-byte order",
            "live_absent": "bounded swap-remove pools; byte-sorted live scan index",
            "find_miss_positions": "floor(find_count * read_miss_ratio), target-stream sample",
        },
    })
    return PreparedMixedWorkload(initial_keys, initial_values, held_out_keys, operations, metadata)
