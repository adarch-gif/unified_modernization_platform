from __future__ import annotations

from math import log2

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


class SearchEvaluationHarness:
    def compare_live(
        self,
        primary: dict[str, object],
        shadow: dict[str, object],
        top_k: int = 10,
    ) -> dict[str, object]:
        primary_ids = [item.get("id") for item in primary.get("results", []) if isinstance(item, dict)]
        shadow_ids = [item.get("id") for item in shadow.get("results", []) if isinstance(item, dict)]
        primary_top = primary_ids[:top_k]
        shadow_top = shadow_ids[:top_k]
        overlap = len(set(primary_top).intersection(shadow_top))
        denominator = max(len(primary_top), len(shadow_top), 1)
        return {
            "primary_count": len(primary_ids),
            "shadow_count": len(shadow_ids),
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
