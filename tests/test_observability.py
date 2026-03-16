import logging
from collections.abc import Mapping
from typing import Any

from unified_modernization.observability.opentelemetry import OpenTelemetryTelemetrySink
from unified_modernization.observability.telemetry import StructuredLoggerTelemetrySink, TelemetryEvent


class _FakeCounter:
    def __init__(self) -> None:
        self.calls: list[tuple[int, dict[str, str]]] = []

    def add(self, amount: int, attributes: Mapping[str, str] | None = None) -> None:
        self.calls.append((amount, dict(attributes or {})))


class _FakeHistogram:
    def __init__(self) -> None:
        self.calls: list[tuple[float, dict[str, str]]] = []

    def record(self, amount: float, attributes: Mapping[str, str] | None = None) -> None:
        self.calls.append((amount, dict(attributes or {})))


class _FakeSpan:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.exceptions: list[str] = []

    def add_event(self, name: str, attributes: Mapping[str, Any] | None = None) -> None:
        self.events.append((name, dict(attributes or {})))

    def set_attribute(self, key: str, value: Any) -> None:
        self.events.append(("set_attribute", {key: value}))

    def record_exception(self, exception: BaseException) -> None:
        self.exceptions.append(type(exception).__name__)


class _FakeSpanContextManager:
    def __init__(self, span: _FakeSpan) -> None:
        self._span = span

    def __enter__(self) -> _FakeSpan:
        return self._span

    def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
        del exc_type, exc, tb
        return False


class _FakeTracer:
    def __init__(self) -> None:
        self.spans: list[_FakeSpan] = []

    def start_as_current_span(
        self,
        name: str,
        *,
        attributes: Mapping[str, Any] | None = None,
    ) -> _FakeSpanContextManager:
        del name, attributes
        span = _FakeSpan()
        self.spans.append(span)
        return _FakeSpanContextManager(span)


class _FakeMeter:
    def __init__(self) -> None:
        self.counters: dict[str, _FakeCounter] = {}
        self.histograms: dict[str, _FakeHistogram] = {}

    def create_counter(self, name: str) -> _FakeCounter:
        counter = _FakeCounter()
        self.counters[name] = counter
        return counter

    def create_histogram(self, name: str, *, unit: str | None = None) -> _FakeHistogram:
        del unit
        histogram = _FakeHistogram()
        self.histograms[name] = histogram
        return histogram


def test_open_telemetry_sink_emits_metrics_and_span_events() -> None:
    tracer = _FakeTracer()
    meter = _FakeMeter()
    sink = OpenTelemetryTelemetrySink(tracer=tracer, meter=meter)

    with sink.start_span("search.gateway.request", trace_id="trace-1", attributes={"mode": "shadow"}):
        sink.increment("search.gateway.requests", tags={"mode": "shadow"})
        sink.record_timing("search.backend.latency_ms", 12.5, tags={"backend": "azure"})
        sink.emit(
            TelemetryEvent(
                event_type="shadow_quality_healthy",
                attributes={"entity_type": "customerDocument"},
            )
        )

    span = tracer.spans[0]
    event_names = [name for name, _ in span.events]
    assert "search.gateway.request.start" in event_names
    assert "search.gateway.request.finish" in event_names
    assert "shadow_quality_healthy" in event_names
    assert meter.counters["search.gateway.requests"].calls[0] == (1, {"mode": "shadow"})
    assert meter.histograms["search.backend.latency_ms"].calls[0] == (12.5, {"backend": "azure"})


def test_open_telemetry_sink_records_exceptions_on_span_failure() -> None:
    tracer = _FakeTracer()
    meter = _FakeMeter()
    sink = OpenTelemetryTelemetrySink(tracer=tracer, meter=meter)

    try:
        with sink.start_span("projection.publisher.publish", trace_id="trace-2"):
            raise RuntimeError("boom")
    except RuntimeError:
        pass

    assert tracer.spans[0].exceptions == ["RuntimeError"]


def test_structured_logger_sink_routes_events_by_severity() -> None:
    records: list[logging.LogRecord] = []

    class _CapturingHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    logger = logging.getLogger("unified_modernization.tests.telemetry")
    logger.handlers = []
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.addHandler(_CapturingHandler())

    sink = StructuredLoggerTelemetrySink(logger=logger)
    sink.emit(TelemetryEvent(event_type="projection_failed", severity="error"))
    sink.emit(TelemetryEvent(event_type="projection_warning", severity="warning"))

    assert [record.levelno for record in records] == [logging.ERROR, logging.WARNING]
