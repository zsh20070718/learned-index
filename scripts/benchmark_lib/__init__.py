"""Core helpers for the unified static string-index benchmark."""

from .adapters import AdapterManifest, load_manifest, parse_runner_result
from .dataset import PreparedDataset, logical_hash, prepare_dataset
from .errors import BenchmarkError, InputError, ResourceBudget
from .readers import iter_keys, resolve_reader
from .workload import WorkloadConfig, generate_queries, load_workload

__all__ = [
    "AdapterManifest",
    "BenchmarkError",
    "InputError",
    "PreparedDataset",
    "ResourceBudget",
    "WorkloadConfig",
    "generate_queries",
    "iter_keys",
    "load_manifest",
    "load_workload",
    "logical_hash",
    "parse_runner_result",
    "prepare_dataset",
    "resolve_reader",
]
