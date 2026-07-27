"""Retry policies for shrinking incomplete work after failures."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RetryPolicy:
    """Bounded retry policy with deterministic work-unit shrinking."""

    max_attempts: int = 4
    shrink_factor: float = 0.5
    minimum_chunk_size: int = 1

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        if not 0 < self.shrink_factor < 1:
            raise ValueError("shrink_factor must be between 0 and 1")
        if self.minimum_chunk_size < 1:
            raise ValueError("minimum_chunk_size must be positive")

    def next_chunk_size(self, current_chunk_size: int) -> int:
        """Shrink a failed unit while preserving a positive minimum."""
        if current_chunk_size < 1:
            raise ValueError("current_chunk_size must be positive")
        reduced = int(current_chunk_size * self.shrink_factor)
        return max(self.minimum_chunk_size, min(current_chunk_size - 1, reduced))

    def can_retry(self, completed_attempts: int) -> bool:
        if completed_attempts < 0:
            raise ValueError("completed_attempts must not be negative")
        return completed_attempts < self.max_attempts
