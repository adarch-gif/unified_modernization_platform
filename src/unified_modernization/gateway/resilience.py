from __future__ import annotations

import asyncio
from enum import StrEnum
from time import monotonic
from typing import Any, Protocol


class TelemetrySink(Protocol):
    def emit(self, event: dict[str, Any]) -> None:
        raise NotImplementedError


class NoopTelemetrySink:
    def emit(self, event: dict[str, Any]) -> None:
        del event


class CircuitState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitOpenError(RuntimeError):
    pass


class ResilientSearchBackend:
    """Timeouts, retries, and circuit-breaking wrapper around a search backend."""

    def __init__(
        self,
        backend: Any,
        *,
        name: str,
        timeout_seconds: float = 1.0,
        max_retries: int = 1,
        retry_backoff_seconds: float = 0.05,
        failure_threshold: int = 3,
        recovery_timeout_seconds: float = 30.0,
        telemetry_sink: TelemetrySink | None = None,
    ) -> None:
        self._backend = backend
        self._name = name
        self._timeout_seconds = timeout_seconds
        self._max_retries = max_retries
        self._retry_backoff_seconds = retry_backoff_seconds
        self._failure_threshold = failure_threshold
        self._recovery_timeout_seconds = recovery_timeout_seconds
        self._telemetry_sink = telemetry_sink or NoopTelemetrySink()
        self._state = CircuitState.CLOSED
        self._consecutive_failures = 0
        self._opened_at = 0.0

    @property
    def state(self) -> CircuitState:
        return self._state

    async def query(self, request: dict[str, Any]) -> dict[str, Any]:
        self._before_request()
        last_error: Exception | None = None
        attempts = self._max_retries + 1

        for attempt in range(1, attempts + 1):
            try:
                response = await asyncio.wait_for(self._backend.query(request), timeout=self._timeout_seconds)
                self._record_success(attempt=attempt)
                return response
            except Exception as exc:  # pragma: no cover - failure behavior covered by tests
                last_error = exc
                self._record_failure(exc, attempt=attempt)
                if attempt >= attempts:
                    raise
                await asyncio.sleep(self._retry_backoff_seconds * attempt)

        assert last_error is not None
        raise last_error

    def _before_request(self) -> None:
        if self._state != CircuitState.OPEN:
            return
        if monotonic() - self._opened_at >= self._recovery_timeout_seconds:
            self._state = CircuitState.HALF_OPEN
            self._telemetry_sink.emit(
                {
                    "event_type": "circuit_half_open",
                    "backend": self._name,
                }
            )
            return
        raise CircuitOpenError(f"circuit open for backend {self._name}")

    def _record_success(self, *, attempt: int) -> None:
        previous_state = self._state
        self._state = CircuitState.CLOSED
        self._consecutive_failures = 0
        if previous_state != CircuitState.CLOSED:
            self._telemetry_sink.emit(
                {
                    "event_type": "circuit_closed",
                    "backend": self._name,
                    "attempt": attempt,
                }
            )

    def _record_failure(self, error: Exception, *, attempt: int) -> None:
        self._consecutive_failures += 1
        self._telemetry_sink.emit(
            {
                "event_type": "backend_failure",
                "backend": self._name,
                "attempt": attempt,
                "error_type": type(error).__name__,
                "state": self._state.value,
            }
        )
        if self._consecutive_failures >= self._failure_threshold or self._state == CircuitState.HALF_OPEN:
            self._state = CircuitState.OPEN
            self._opened_at = monotonic()
            self._telemetry_sink.emit(
                {
                    "event_type": "circuit_open",
                    "backend": self._name,
                    "consecutive_failures": self._consecutive_failures,
                }
            )
