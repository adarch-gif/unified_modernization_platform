from __future__ import annotations

from math import log2
from typing import Any

from pydantic import BaseModel, Field


class QueryJudgment(BaseModel):
    query_id: str
    graded_relevance: dict[str, float] = Field(default_factory=dict)

    @classmethod
    def from_relevant_ids(cls, query_id: str, relevant_ids: list[str]) -> "QueryJudgment":
        return cls(query_id=query_id, graded_relevance={doc_id: 1.0 for doc_id in relevant_ids})


class QueryEvaluationCase(BaseModel):
    query_id: str
    retrieved_ids: list[str]
    judgment: QueryJudgment


class QueryEvaluationMetrics(BaseModel):
    query_id: str
    ndcg_at_10: float
    mrr: float
    zero_results: bool


class CorpusEvaluationReport(BaseModel):
    query_count: int
    average_ndcg_at_10: float
    average_mrr: float
    zero_result_rate: float
    queries: list[QueryEvaluationMetrics]


class ShadowQualityEvent(BaseModel):
    severity: str
    code: str
    query_id: str | None = None
    metrics: dict[str, float | int | bool] = Field(default_factory=dict)
    message: str


class ShadowQualityDecision(BaseModel):
    live_comparison: dict[str, object]
    event: ShadowQualityEvent | None = None
    judged_metrics: dict[str, float | bool | str] = Field(default_factory=dict)


class ShadowQualityGate:
    def __init__(
        self,
        *,
        min_overlap_rate_at_k: float = 0.5,
        min_shadow_ndcg_at_10: float = 0.85,
        max_ndcg_drop: float = 0.10,
        min_shadow_mrr: float = 0.5,
    ) -> None:
        self._min_overlap_rate_at_k = min_overlap_rate_at_k
        self._min_shadow_ndcg_at_10 = min_shadow_ndcg_at_10
        self._max_ndcg_drop = max_ndcg_drop
        self._min_shadow_mrr = min_shadow_mrr

    def evaluate(
        self,
        *,
        live_comparison: dict[str, object],
        query_id: str | None = None,
        primary_metrics: QueryEvaluationMetrics | None = None,
        shadow_metrics: QueryEvaluationMetrics | None = None,
    ) -> ShadowQualityDecision:
        overlap_rate_raw = live_comparison.get("overlap_rate_at_k", 0.0)
        overlap_rate = float(overlap_rate_raw) if isinstance(overlap_rate_raw, (int, float)) else 0.0
        if primary_metrics is not None and shadow_metrics is not None:
            ndcg_drop = primary_metrics.ndcg_at_10 - shadow_metrics.ndcg_at_10
            if (
                shadow_metrics.ndcg_at_10 < self._min_shadow_ndcg_at_10
                or ndcg_drop > self._max_ndcg_drop
                or shadow_metrics.mrr < self._min_shadow_mrr
            ):
                event = ShadowQualityEvent(
                    severity="high",
                    code="shadow_relevance_regression",
                    query_id=query_id,
                    metrics={
                        "primary_ndcg_at_10": primary_metrics.ndcg_at_10,
                        "shadow_ndcg_at_10": shadow_metrics.ndcg_at_10,
                        "ndcg_drop": ndcg_drop,
                        "primary_mrr": primary_metrics.mrr,
                        "shadow_mrr": shadow_metrics.mrr,
                    },
                    message="shadow ranking quality is below threshold",
                )
                return ShadowQualityDecision(
                    live_comparison=live_comparison,
                    event=event,
                    judged_metrics={
                        "query_id": query_id or "",
                        "primary_ndcg_at_10": primary_metrics.ndcg_at_10,
                        "shadow_ndcg_at_10": shadow_metrics.ndcg_at_10,
                        "primary_mrr": primary_metrics.mrr,
                        "shadow_mrr": shadow_metrics.mrr,
                    },
                )
            return ShadowQualityDecision(
                live_comparison=live_comparison,
                judged_metrics={
                    "query_id": query_id or "",
                    "primary_ndcg_at_10": primary_metrics.ndcg_at_10,
                    "shadow_ndcg_at_10": shadow_metrics.ndcg_at_10,
                    "primary_mrr": primary_metrics.mrr,
                    "shadow_mrr": shadow_metrics.mrr,
                },
            )

        if overlap_rate < self._min_overlap_rate_at_k:
            event = ShadowQualityEvent(
                severity="medium",
                code="shadow_overlap_regression",
                query_id=query_id,
                metrics={"overlap_rate_at_k": overlap_rate},
                message="shadow overlap at K is below the operational threshold",
            )
            return ShadowQualityDecision(live_comparison=live_comparison, event=event)
        return ShadowQualityDecision(live_comparison=live_comparison)


class SearchEvaluationHarness:
    @staticmethod
    def extract_ids(response: dict[str, object], *, top_k: int | None = None) -> list[str]:
        raw_results: Any = response.get("results", [])
        results = raw_results if isinstance(raw_results, list) else []
        ids = [item.get("id") for item in results if isinstance(item, dict)]
        normalized = [str(item) for item in ids if item is not None]
        if top_k is not None:
            return normalized[:top_k]
        return normalized

    def compare_live(
        self,
        primary: dict[str, object],
        shadow: dict[str, object],
        top_k: int = 10,
    ) -> dict[str, object]:
        primary_top = self.extract_ids(primary, top_k=top_k)
        shadow_top = self.extract_ids(shadow, top_k=top_k)
        overlap = len(set(primary_top).intersection(shadow_top))
        denominator = max(len(primary_top), len(shadow_top), 1)
        return {
            "primary_count": len(self.extract_ids(primary)),
            "shadow_count": len(self.extract_ids(shadow)),
            "overlap_at_k": overlap,
            "overlap_rate_at_k": overlap / denominator,
            "identical_order": primary_top == shadow_top,
        }

    def evaluate_case(self, case: QueryEvaluationCase, k: int = 10) -> QueryEvaluationMetrics:
        retrieved = case.retrieved_ids[:k]
        grades = case.judgment.graded_relevance

        dcg = 0.0
        for rank, doc_id in enumerate(retrieved, start=1):
            gain = grades.get(doc_id, 0.0)
            if gain > 0:
                dcg += (2**gain - 1) / log2(rank + 1)

        ideal_grades = sorted(grades.values(), reverse=True)[:k]
        idcg = 0.0
        for rank, gain in enumerate(ideal_grades, start=1):
            idcg += (2**gain - 1) / log2(rank + 1)

        mrr = 0.0
        for rank, doc_id in enumerate(retrieved, start=1):
            if grades.get(doc_id, 0.0) > 0:
                mrr = 1.0 / rank
                break

        return QueryEvaluationMetrics(
            query_id=case.query_id,
            ndcg_at_10=0.0 if idcg == 0 else dcg / idcg,
            mrr=mrr,
            zero_results=not retrieved,
        )

    def evaluate_corpus(self, cases: list[QueryEvaluationCase], k: int = 10) -> CorpusEvaluationReport:
        metrics = [self.evaluate_case(case, k=k) for case in cases]
        if not metrics:
            return CorpusEvaluationReport(
                query_count=0,
                average_ndcg_at_10=0.0,
                average_mrr=0.0,
                zero_result_rate=0.0,
                queries=[],
            )
        return CorpusEvaluationReport(
            query_count=len(metrics),
            average_ndcg_at_10=sum(item.ndcg_at_10 for item in metrics) / len(metrics),
            average_mrr=sum(item.mrr for item in metrics) / len(metrics),
            zero_result_rate=sum(1 for item in metrics if item.zero_results) / len(metrics),
            queries=metrics,
        )

    def evaluate_response(
        self,
        *,
        query_id: str,
        response: dict[str, object],
        judgment: QueryJudgment,
        k: int = 10,
    ) -> QueryEvaluationMetrics:
        return self.evaluate_case(
            QueryEvaluationCase(
                query_id=query_id,
                retrieved_ids=self.extract_ids(response, top_k=k),
                judgment=judgment,
            ),
            k=k,
        )
