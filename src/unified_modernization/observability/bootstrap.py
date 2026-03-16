from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from typing import Literal, cast

from pydantic import BaseModel, Field

from unified_modernization.observability.opentelemetry import OpenTelemetryTelemetrySink
from unified_modernization.observability.telemetry import (
    InMemoryTelemetrySink,
    NoopTelemetrySink,
    StructuredLoggerTelemetrySink,
    TelemetrySink,
)


class TelemetryRuntimeConfig(BaseModel):
    mode: Literal["noop", "memory", "logger", "otlp_http"] = "noop"
    service_name: str = "unified-modernization-platform"
    otlp_collector_endpoint: str | None = None
    otlp_headers: dict[str, str] = Field(default_factory=dict)


def build_telemetry_sink(
    config: TelemetryRuntimeConfig,
    *,
    logger: logging.Logger | None = None,
) -> TelemetrySink:
    if config.mode == "noop":
        return NoopTelemetrySink()
    if config.mode == "memory":
        return InMemoryTelemetrySink()
    if config.mode == "logger":
        return StructuredLoggerTelemetrySink(logger=logger)
    if config.mode == "otlp_http":
        if not config.otlp_collector_endpoint:
            raise ValueError("otlp_collector_endpoint is required when telemetry mode is otlp_http")
        return OpenTelemetryTelemetrySink.from_otlp_http(
            service_name=config.service_name,
            collector_endpoint=config.otlp_collector_endpoint,
            headers=config.otlp_headers,
            register_global_providers=True,
        )
    raise ValueError(f"unsupported telemetry mode {config.mode!r}")


def load_telemetry_runtime_config_from_env(
    env: Mapping[str, str] | None = None,
    *,
    prefix: str = "UMP_",
) -> TelemetryRuntimeConfig:
    source = env or os.environ
    mode = cast(
        Literal["noop", "memory", "logger", "otlp_http"],
        source.get(f"{prefix}TELEMETRY_MODE", "noop").strip().lower(),
    )
    return TelemetryRuntimeConfig(
        mode=mode,
        service_name=source.get(f"{prefix}TELEMETRY_SERVICE_NAME", "unified-modernization-platform").strip(),
        otlp_collector_endpoint=_optional_str(source.get(f"{prefix}OTLP_COLLECTOR_ENDPOINT")),
        otlp_headers=_parse_mapping(source.get(f"{prefix}OTLP_HEADERS")),
    )


def _parse_mapping(raw: str | None) -> dict[str, str]:
    if raw is None or not raw.strip():
        return {}
    pairs: dict[str, str] = {}
    for item in raw.split(","):
        if "=" not in item:
            continue
        key, value = item.split("=", 1)
        key = key.strip()
        if not key:
            continue
        pairs[key] = value.strip()
    return pairs


def _optional_str(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None
