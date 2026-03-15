"""Resilience patterns for external API calls."""

import asyncio
import functools
import logging
import time
from enum import StrEnum
from typing import Callable, Any

logger = logging.getLogger(__name__)


class CircuitState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitOpenError(Exception):
    """Raised when a circuit breaker is open and rejects a call."""

    def __init__(self, name: str, seconds_remaining: float):
        self.name = name
        self.seconds_remaining = seconds_remaining
        super().__init__(
            f"Circuit '{name}' is open. Resets in {seconds_remaining:.0f}s"
        )


class CircuitBreaker:
    """Circuit breaker for external API calls.

    Tracks consecutive failures. After failure_threshold failures, opens the
    circuit for recovery_timeout seconds. During that time, all calls fail
    fast with CircuitOpenError. After the timeout, allows half_open_max_calls
    probe requests. If a probe succeeds, the circuit closes. If it fails,
    the circuit reopens.

    Uses time.monotonic() (not time.time()) to avoid clock jumps.
    Excludes specified exception types from tripping the breaker
    (e.g., ValueError, KeyError are business logic errors, not infra failures).
    """

    def __init__(
        self,
        name: str = "default",
        failure_threshold: int = 5,
        recovery_timeout: float = 60.0,
        half_open_max_calls: int = 1,
        exclude: tuple[type[Exception], ...] = (KeyError, ValueError),
    ):
        self.name = name
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.half_open_max_calls = half_open_max_calls
        self.exclude = exclude

        self._failure_count = 0
        self._last_failure_time = 0.0
        self._state = CircuitState.CLOSED
        self._half_open_calls = 0

    @property
    def state(self) -> CircuitState:
        if self._state == CircuitState.OPEN:
            elapsed = time.monotonic() - self._last_failure_time
            if elapsed >= self.recovery_timeout:
                self._state = CircuitState.HALF_OPEN
                self._half_open_calls = 0
                logger.info(f"Circuit '{self.name}': OPEN -> HALF_OPEN (probing)")
        return self._state

    def _record_success(self) -> None:
        if self._state != CircuitState.CLOSED:
            logger.info(f"Circuit '{self.name}': {self._state.value} -> CLOSED (recovered)")
        self._failure_count = 0
        self._state = CircuitState.CLOSED

    def _record_failure(self) -> None:
        self._failure_count += 1
        self._last_failure_time = time.monotonic()
        if self._failure_count >= self.failure_threshold:
            if self._state != CircuitState.OPEN:
                logger.warning(
                    f"Circuit '{self.name}': -> OPEN after {self._failure_count} failures"
                )
            self._state = CircuitState.OPEN

    def _check_can_execute(self) -> None:
        current = self.state
        if current == CircuitState.OPEN:
            remaining = self.recovery_timeout - (time.monotonic() - self._last_failure_time)
            raise CircuitOpenError(self.name, max(0, remaining))
        if current == CircuitState.HALF_OPEN and self._half_open_calls >= self.half_open_max_calls:
            raise CircuitOpenError(self.name, self.recovery_timeout)

    def __call__(self, func: Callable) -> Callable:
        """Use as async decorator: @breaker"""

        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            self._check_can_execute()
            if self.state == CircuitState.HALF_OPEN:
                self._half_open_calls += 1
            try:
                result = await func(*args, **kwargs)
                self._record_success()
                return result
            except Exception as exc:
                if isinstance(exc, self.exclude):
                    raise
                self._record_failure()
                raise

        return wrapper

    async def call(self, func: Callable, *args: Any, **kwargs: Any) -> Any:
        """Call a function through the circuit breaker (non-decorator usage)."""
        self._check_can_execute()
        if self.state == CircuitState.HALF_OPEN:
            self._half_open_calls += 1
        try:
            result = await func(*args, **kwargs)
            self._record_success()
            return result
        except Exception as exc:
            if isinstance(exc, self.exclude):
                raise
            self._record_failure()
            raise
