from __future__ import annotations

from collections.abc import Mapping
from contextvars import ContextVar, Token
from time import monotonic
from typing import Any, Literal, Protocol
from uuid import uuid4

from unified_modernization.observability.telemetry import InMemoryTelemetrySink, TelemetryEvent, TelemetrySpan


class OTelSpanProtocol(Protocol):
    def add_event(self, name: str, attributes: Mapping[str, Any] | None = None) -> None:
        raise NotImplementedError

    def set_attribute(self, key: str, value: Any) -> None:
        raise NotImplementedError

    def record_exception(self, exception: BaseException) -> None:
        raise NotImplementedError


class OTelSpanContextManagerProtocol(Protocol):
    def __enter__(self) -> OTelSpanProtocol:
        raise NotImplementedError

    def __exit__(self, exc_type: object, exc: object, tb: object) -> bool | None:
        raise NotImplementedError


class OTelTracerProtocol(Protocol):
    def start_as_current_span(
        self,
        name: str,
        *,
        attributes: Mapping[str, Any] | None = None,
    ) -> OTelSpanContextManagerProtocol:
        raise NotImplementedError


class OTelCounterProtocol(Protocol):
    def add(self, amount: int, attributes: Mapping[str, str] | None = None) -> None:
        raise NotImplementedError


class OTelHistogramProtocol(Protocol):
    def record(self, amount: float, attributes: Mapping[str, str] | None = None) -> None:
        raise NotImplementedError


class OTelMeterProtocol(Protocol):
    def create_counter(self, name: str) -> OTelCounterProtocol:
        raise NotImplementedError

    def create_histogram(self, name: str, *, unit: str | None = None) -> OTelHistogramProtocol:
        raise NotImplementedError


class OpenTelemetryTelemetrySink:
    def __init__(
        self,
        *,
        tracer: OTelTracerProtocol,
        meter: OTelMeterProtocol,
    ) -> None:
        self._tracer = tracer
        self._meter = meter
        self._current_span: ContextVar[OTelSpanProtocol | None] = ContextVar(
            "unified_modernization_current_otel_span",
            default=None,
        )
        self._counters: dict[str, OTelCounterProtocol] = {}
        self._histograms: dict[str, OTelHistogramProtocol] = {}

    def emit(self, event: TelemetryEvent | Mapping[str, Any]) -> None:
        normalized = InMemoryTelemetrySink._normalize_event(event)
        span = self._current_span.get()
        attributes = {
            "severity": normalized.severity,
            **{key: value for key, value in normalized.attributes.items()},
        }
        if span is not None:
            span.add_event(normalized.event_type, attributes=attributes)
        self.increment(
            "telemetry.events",
            tags={"event_type": normalized.event_type, "severity": normalized.severity},
        )

    def increment(self, name: str, value: int = 1, *, tags: Mapping[str, str] | None = None) -> None:
        counter = self._counters.get(name)
        if counter is None:
            counter = self._meter.create_counter(name)
            self._counters[name] = counter
        counter.add(value, attributes=dict(tags or {}))

    def record_timing(
        self,
        name: str,
        duration_ms: float,
        *,
        tags: Mapping[str, str] | None = None,
        trace_id: str | None = None,
    ) -> None:
        del trace_id
        histogram = self._histograms.get(name)
        if histogram is None:
            histogram = self._meter.create_histogram(name, unit="ms")
            self._histograms[name] = histogram
        histogram.record(duration_ms, attributes=dict(tags or {}))

    def start_span(
        self,
        name: str,
        *,
        trace_id: str | None = None,
        attributes: Mapping[str, Any] | None = None,
    ) -> TelemetrySpan:
        return OpenTelemetrySpan(
            sink=self,
            tracer=self._tracer,
            name=name,
            trace_id=trace_id,
            attributes=attributes,
        )

    def _set_current_span(self, span: OTelSpanProtocol) -> Token[OTelSpanProtocol | None]:
        return self._current_span.set(span)

    def _reset_current_span(self, token: Token[OTelSpanProtocol | None]) -> None:
        self._current_span.reset(token)

    @classmethod
    def from_otlp_http(
        cls,
        *,
        service_name: str,
        collector_endpoint: str,
        headers: Mapping[str, str] | None = None,
    ) -> "OpenTelemetryTelemetrySink":
        from opentelemetry import metrics, trace
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        base = collector_endpoint.rstrip("/")
        resource = Resource.create({"service.name": service_name})
        tracer_provider = TracerProvider(resource=resource)
        tracer_provider.add_span_processor(
            BatchSpanProcessor(
                OTLPSpanExporter(
                    endpoint=f"{base}/v1/traces",
                    headers=dict(headers or {}),
                )
            )
        )
        meter_provider = MeterProvider(
            metric_readers=[
                PeriodicExportingMetricReader(
                    OTLPMetricExporter(
                        endpoint=f"{base}/v1/metrics",
                        headers=dict(headers or {}),
                    )
                )
            ],
            resource=resource,
        )
        trace.set_tracer_provider(tracer_provider)
        metrics.set_meter_provider(meter_provider)
        return cls(
            tracer=trace.get_tracer(service_name),
            meter=metrics.get_meter(service_name),
        )


class OpenTelemetrySpan(TelemetrySpan):
    def __init__(
        self,
        *,
        sink: OpenTelemetryTelemetrySink,
        tracer: OTelTracerProtocol,
        name: str,
        trace_id: str | None = None,
        attributes: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(sink, name, trace_id=trace_id, attributes=attributes)
        self._otel_sink = sink
        self._tracer = tracer
        self._trace_id = trace_id or str(uuid4())
        self._attributes = dict(attributes or {})
        self._started_at = 0.0
        self._context_manager: OTelSpanContextManagerProtocol | None = None
        self._otel_span: OTelSpanProtocol | None = None
        self._token: Token[OTelSpanProtocol | None] | None = None

    def __enter__(self) -> "OpenTelemetrySpan":
        self._started_at = monotonic()
        self._context_manager = self._tracer.start_as_current_span(
            self._name,
            attributes={"trace_id": self._trace_id, **self._attributes},
        )
        self._otel_span = self._context_manager.__enter__()
        self._token = self._otel_sink._set_current_span(self._otel_span)
        self._otel_span.add_event(f"{self._name}.start", attributes=self._attributes)
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> Literal[False]:
        duration_ms = (monotonic() - self._started_at) * 1000
        tags = {key: str(value) for key, value in self._attributes.items()}
        self._otel_sink.record_timing(self._name, duration_ms, tags=tags, trace_id=self._trace_id)
        completion_attributes = dict(self._attributes)
        completion_attributes["outcome"] = "error" if exc is not None else "ok"
        if self._otel_span is not None:
            if exc is not None and isinstance(exc, BaseException):
                self._otel_span.record_exception(exc)
                completion_attributes["error_type"] = type(exc).__name__
            self._otel_span.add_event(f"{self._name}.finish", attributes=completion_attributes)
        if self._token is not None:
            self._otel_sink._reset_current_span(self._token)
        if self._context_manager is not None:
            self._context_manager.__exit__(exc_type, exc, tb)
        return False
