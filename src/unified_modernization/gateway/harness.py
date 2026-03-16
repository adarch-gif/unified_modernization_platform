from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Sequence
from pathlib import Path
from statistics import mean
from time import monotonic
from typing import Any

from pydantic import BaseModel, Field

from unified_modernization.gateway.bootstrap import build_http_search_gateway_service_from_env
from unified_modernization.gateway.service import SearchGatewayService
from unified_modernization.observability.bootstrap import build_telemetry_sink, load_telemetry_runtime_config_from_env
from unified_modernization.observability.telemetry import InMemoryTelemetrySink, NoopTelemetrySink


class SearchHarnessCase(BaseModel):
    name: str
    consumer_id: str
    tenant_id: str
    entity_type: str
    raw_params: dict[str, str]
    weight: int = Field(default=1, ge=1)


class SearchHarnessConfig(BaseModel):
    concurrency: int = Field(default=8, ge=1)
    iterations: int = Field(default=1, ge=1)
    warmup_iterations: int = Field(default=0, ge=0)
    max_error_samples: int = Field(default=10, ge=1)


class SearchHarnessErrorSample(BaseModel):
    case_name: str
    error_type: str
    error_message: str


class SearchHarnessReport(BaseModel):
    total_requests: int
    successful_requests: int
    failed_requests: int
    average_latency_ms: float
    p50_latency_ms: float
    p95_latency_ms: float
    p99_latency_ms: float
    min_latency_ms: float
    max_latency_ms: float
    average_result_count: float
    shadow_backend_failures: int = 0
    shadow_regressions: int = 0
    canary_auto_disabled: int = 0
    canary_frozen: bool = False
    error_samples: list[SearchHarnessErrorSample] = Field(default_factory=list)


async def run_search_gateway_harness(
    service: SearchGatewayService,
    cases: Sequence[SearchHarnessCase],
    *,
    config: SearchHarnessConfig | None = None,
    telemetry_sink: InMemoryTelemetrySink | None = None,
) -> SearchHarnessReport:
    resolved_config = config or SearchHarnessConfig()
    expanded_warmup = _expand_cases(cases, iterations=resolved_config.warmup_iterations)
    if expanded_warmup:
        await _execute_requests(service, expanded_warmup, concurrency=resolved_config.concurrency)

    baseline_counters = dict(telemetry_sink.counters) if telemetry_sink is not None else {}
    expanded_cases = _expand_cases(cases, iterations=resolved_config.iterations)
    request_results = await _execute_requests(service, expanded_cases, concurrency=resolved_config.concurrency)
    latencies = sorted(result["latency_ms"] for result in request_results)
    successes = [result for result in request_results if result["ok"]]
    errors = [result for result in request_results if not result["ok"]]
    result_counts = [result["result_count"] for result in successes]

    shadow_backend_failures = 0
    shadow_regressions = 0
    canary_auto_disabled = 0
    if telemetry_sink is not None:
        counter_deltas = _counter_deltas(telemetry_sink.counters, baseline_counters)
        shadow_backend_failures = _sum_counter_values(counter_deltas, "search.shadow.backend_failure")
        shadow_regressions = _sum_counter_values(counter_deltas, "search.shadow.regression")
        canary_auto_disabled = _sum_counter_values(counter_deltas, "search.gateway.canary_auto_disabled")

    return SearchHarnessReport(
        total_requests=len(request_results),
        successful_requests=len(successes),
        failed_requests=len(errors),
        average_latency_ms=mean(latencies) if latencies else 0.0,
        p50_latency_ms=_percentile(latencies, 50),
        p95_latency_ms=_percentile(latencies, 95),
        p99_latency_ms=_percentile(latencies, 99),
        min_latency_ms=latencies[0] if latencies else 0.0,
        max_latency_ms=latencies[-1] if latencies else 0.0,
        average_result_count=mean(result_counts) if result_counts else 0.0,
        shadow_backend_failures=shadow_backend_failures,
        shadow_regressions=shadow_regressions,
        canary_auto_disabled=canary_auto_disabled,
        canary_frozen=service.canary_frozen,
        error_samples=[
            SearchHarnessErrorSample(
                case_name=result["case_name"],
                error_type=result["error_type"],
                error_message=result["error_message"],
            )
            for result in errors[: resolved_config.max_error_samples]
        ],
    )


async def run_smoke_test(
    service: SearchGatewayService,
    cases: Sequence[SearchHarnessCase],
    *,
    telemetry_sink: InMemoryTelemetrySink | None = None,
) -> SearchHarnessReport:
    return await run_search_gateway_harness(
        service,
        cases,
        config=SearchHarnessConfig(concurrency=1, iterations=1, warmup_iterations=0),
        telemetry_sink=telemetry_sink,
    )


def load_harness_cases(path: str | Path) -> list[SearchHarnessCase]:
    case_path = Path(path)
    text = case_path.read_text(encoding="utf-8")
    if case_path.suffix.lower() == ".jsonl":
        return [
            SearchHarnessCase.model_validate(json.loads(line))
            for line in text.splitlines()
            if line.strip()
        ]
    raw = json.loads(text)
    if not isinstance(raw, list):
        raise ValueError("harness case file must contain a JSON array or JSONL records")
    return [SearchHarnessCase.model_validate(item) for item in raw]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run smoke/load traffic against the unified modernization search gateway.")
    parser.add_argument("--cases-file", required=True, help="Path to JSON or JSONL search harness cases.")
    parser.add_argument("--concurrency", type=int, default=8, help="Concurrent in-flight requests.")
    parser.add_argument("--iterations", type=int, default=1, help="How many times to replay the case set.")
    parser.add_argument("--warmup-iterations", type=int, default=0, help="Warmup iterations excluded from the report.")
    parser.add_argument("--prefix", default="UMP_", help="Environment variable prefix for runtime config.")
    parser.add_argument("--output", help="Optional path to write the JSON report.")
    args = parser.parse_args(list(argv) if argv is not None else None)

    telemetry_config = load_telemetry_runtime_config_from_env(prefix=args.prefix)
    telemetry_sink = build_telemetry_sink(telemetry_config)
    if isinstance(telemetry_sink, NoopTelemetrySink):
        telemetry_sink = InMemoryTelemetrySink()

    service = build_http_search_gateway_service_from_env(
        telemetry_sink=telemetry_sink,
        prefix=args.prefix,
    )
    report = asyncio.run(
        run_search_gateway_harness(
            service,
            load_harness_cases(args.cases_file),
            config=SearchHarnessConfig(
                concurrency=args.concurrency,
                iterations=args.iterations,
                warmup_iterations=args.warmup_iterations,
            ),
            telemetry_sink=telemetry_sink if isinstance(telemetry_sink, InMemoryTelemetrySink) else None,
        )
    )
    payload = report.model_dump_json(indent=2)
    if args.output:
        Path(args.output).write_text(payload + "\n", encoding="utf-8")
    print(payload)
    return 0


async def _execute_requests(
    service: SearchGatewayService,
    cases: Sequence[SearchHarnessCase],
    *,
    concurrency: int,
) -> list[dict[str, Any]]:
    semaphore = asyncio.Semaphore(concurrency)

    async def run_case(case: SearchHarnessCase) -> dict[str, Any]:
        async with semaphore:
            started_at = monotonic()
            try:
                response = await service.search(
                    consumer_id=case.consumer_id,
                    tenant_id=case.tenant_id,
                    entity_type=case.entity_type,
                    raw_params=case.raw_params,
                )
                latency_ms = (monotonic() - started_at) * 1000
                raw_results = response.get("results", [])
                result_count = len(raw_results) if isinstance(raw_results, list) else 0
                return {
                    "case_name": case.name,
                    "ok": True,
                    "latency_ms": latency_ms,
                    "result_count": result_count,
                    "error_type": "",
                    "error_message": "",
                }
            except Exception as exc:
                latency_ms = (monotonic() - started_at) * 1000
                return {
                    "case_name": case.name,
                    "ok": False,
                    "latency_ms": latency_ms,
                    "result_count": 0,
                    "error_type": type(exc).__name__,
                    "error_message": str(exc),
                }

    return list(await asyncio.gather(*(run_case(case) for case in cases)))


def _expand_cases(cases: Sequence[SearchHarnessCase], *, iterations: int) -> list[SearchHarnessCase]:
    expanded: list[SearchHarnessCase] = []
    if iterations <= 0:
        return expanded
    for _ in range(iterations):
        for case in cases:
            expanded.extend([case] * case.weight)
    return expanded


def _counter_deltas(
    after: dict[tuple[str, tuple[tuple[str, str], ...]], int],
    before: dict[tuple[str, tuple[tuple[str, str], ...]], int],
) -> dict[tuple[str, tuple[tuple[str, str], ...]], int]:
    deltas: dict[tuple[str, tuple[tuple[str, str], ...]], int] = {}
    for key, value in after.items():
        delta = value - before.get(key, 0)
        if delta:
            deltas[key] = delta
    return deltas


def _sum_counter_values(
    counters: dict[tuple[str, tuple[tuple[str, str], ...]], int],
    name: str,
) -> int:
    return sum(value for (counter_name, _), value in counters.items() if counter_name == name)


def _percentile(values: Sequence[float], percentile: int) -> float:
    if not values:
        return 0.0
    if len(values) == 1:
        return values[0]
    index = max(0, min(len(values) - 1, round((percentile / 100) * (len(values) - 1))))
    return values[index]


if __name__ == "__main__":
    raise SystemExit(main())
