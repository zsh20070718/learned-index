"""Expected, key-safe benchmark errors."""


class BenchmarkError(Exception):
    """Base class for failures that can be presented without a traceback."""


class InputError(BenchmarkError):
    """The user-provided dataset, workload, or adapter metadata is invalid."""


class ResourceBudget(BenchmarkError):
    """Valid input cannot be prepared under the configured resource budget."""


class BuildFailure(BenchmarkError):
    """An adapter could not be configured or built."""

    def __init__(self, message: str, build_identity=None) -> None:
        super().__init__(message)
        self.build_identity = build_identity


class RunFailure(BenchmarkError):
    """A runner invocation failed before producing a valid result."""
