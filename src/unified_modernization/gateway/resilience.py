from __future__ import annotations

import asyncio
from enum import StrEnum
from time import monotonic
from typing import Any, cast

from unified_modernization.observability.telemetry import NoopTelemetrySink, TelemetryEvent, TelemetrySink


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
        trace_id = None if request.get("trace_id") is None else str(request["trace_id"])
        self._before_request(trace_id=trace_id)
        attempts = self._max_retries + 1

        with self._telemetry_sink.start_span(
            "search.backend.query",
            trace_id=trace_id,
            attributes={"backend": self._name, "attempt_budget": attempts},
        ):
            for attempt in range(1, attempts + 1):
                started_at = monotonic()
                try:
                    response = cast(
                        dict[str, Any],
                        await asyncio.wait_for(
                        self._backend.query(request),
                        timeout=self._timeout_seconds,
                        ),
                    )
                    self._record_success(attempt=attempt, duration_ms=(monotonic() - started_at) * 1000)
                    return response
                except Exception as exc:  # pragma: no cover - failure behavior covered by tests
                    self._record_failure(
                        exc,
                        attempt=attempt,
                        duration_ms=(monotonic() - started_at) * 1000,
                    )
                    if attempt >= attempts:
                        raise
                    await asyncio.sleep(self._retry_backoff_seconds * attempt)

        raise RuntimeError("search backend query exited without a response or exception")

    def _before_request(self, *, trace_id: str | None) -> None:
        if self._state != CircuitState.OPEN:
            return
        if monotonic() - self._opened_at >= self._recovery_timeout_seconds:
            self._state = CircuitState.HALF_OPEN
            self._telemetry_sink.emit(
                TelemetryEvent(
                    event_type="circuit_half_open",
                    trace_id=trace_id,
                    attributes={"backend": self._name},
                )
            )
            return
        self._telemetry_sink.increment(
            "search.backend.circuit_rejected",
            tags={"backend": self._name},
        )
        raise CircuitOpenError(f"circuit open for backend {self._name}")

    def _record_success(self, *, attempt: int, duration_ms: float) -> None:
        previous_state = self._state
        self._state = CircuitState.CLOSED
        self._consecutive_failures = 0
        self._telemetry_sink.increment("search.backend.success", tags={"backend": self._name})
        self._telemetry_sink.record_timing(
            "search.backend.latency_ms",
            duration_ms,
            tags={"backend": self._name, "outcome": "success"},
        )
        if previous_state != CircuitState.CLOSED:
            self._telemetry_sink.emit(
                TelemetryEvent(
                    event_type="circuit_closed",
                    attributes={"backend": self._name, "attempt": attempt},
                )
            )

    def _record_failure(self, error: Exception, *, attempt: int, duration_ms: float) -> None:
        self._consecutive_failures += 1
        self._telemetry_sink.increment("search.backend.failure", tags={"backend": self._name})
        self._telemetry_sink.record_timing(
            "search.backend.latency_ms",
            duration_ms,
            tags={"backend": self._name, "outcome": "failure"},
        )
        self._telemetry_sink.emit(
            TelemetryEvent(
                event_type="backend_failure",
                severity="warning",
                attributes={
                    "backend": self._name,
                    "attempt": attempt,
                    "error_type": type(error).__name__,
                    "state": self._state.value,
                },
            )
        )
        if self._consecutive_failures >= self._failure_threshold or self._state == CircuitState.HALF_OPEN:
            self._state = CircuitState.OPEN
            self._opened_at = monotonic()
            self._telemetry_sink.increment("search.backend.circuit_opened", tags={"backend": self._name})
            self._telemetry_sink.emit(
                TelemetryEvent(
                    event_type="circuit_open",
                    severity="warning",
                    attributes={
                        "backend": self._name,
                        "consecutive_failures": self._consecutive_failures,
                    },
                )
            )
