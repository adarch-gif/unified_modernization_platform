from __future__ import annotations

import re
from typing import Any


class ODataTranslator:
    def __init__(self, field_map: dict[str, str] | None = None) -> None:
        self._field_map = field_map or {}

    def translate(self, params: dict[str, str]) -> dict[str, Any]:
        query: dict[str, Any] = {"query": {"bool": {"must": [], "filter": [], "must_not": []}}}

        if search := params.get("$search"):
            query["query"]["bool"]["must"].append(
                {
                    "multi_match": {
                        "query": search,
                        "fields": ["title^4", "customerName^3", "content", "description"],
                    }
                }
            )

        if filt := params.get("$filter"):
            translated = self._translate_filter(filt.strip())
            query["query"]["bool"]["filter"].extend([translated] if isinstance(translated, dict) else translated)

        if orderby := params.get("$orderby"):
            query["sort"] = self._translate_orderby(orderby)

        if top := params.get("$top"):
            query["size"] = min(int(top), 1000)

        if skip := params.get("$skip"):
            query["from"] = int(skip)

        if facets := params.get("$facets") or params.get("facet"):
            query["aggs"] = self._translate_facets(facets)

        for clause in ["must", "filter", "must_not"]:
            if not query["query"]["bool"][clause]:
                del query["query"]["bool"][clause]
        if not query["query"]["bool"]:
            query["query"] = {"match_all": {}}
        return query

    def _map_field(self, field: str) -> str:
        return self._field_map.get(field, field[:1].lower() + field[1:] if field else field)

    def _translate_filter(self, expr: str) -> dict[str, Any] | list[dict[str, Any]]:
        if m := re.match(r"^(\w+)\s+eq\s+'(.+)'$", expr):
            return {"term": {self._map_field(m.group(1)): m.group(2)}}
        if m := re.match(r"^(\w+)\s+ne\s+'(.+)'$", expr):
            return {"bool": {"must_not": [{"term": {self._map_field(m.group(1)): m.group(2)}}]}}

        for op, es_op in [("gt", "gt"), ("ge", "gte"), ("lt", "lt"), ("le", "lte")]:
            if m := re.match(rf"^(\w+)\s+{op}\s+(\S+)$", expr):
                field = self._map_field(m.group(1))
                try:
                    value: float | str = float(m.group(2))
                except ValueError:
                    value = m.group(2).strip("'")
                return {"range": {field: {es_op: value}}}

        if m := re.match(r"^search\.in\((\w+),\s*'([^']+)'\)$", expr):
            field = self._map_field(m.group(1))
            values = [item.strip() for item in m.group(2).split(",")]
            return {"terms": {field: values}}

        raise ValueError(f"unsupported OData filter expression: {expr}")

    def _translate_orderby(self, orderby: str) -> list[dict[str, Any]]:
        sorts: list[dict[str, Any]] = []
        for clause in orderby.split(","):
            parts = clause.strip().split()
            field = self._map_field(parts[0])
            direction = parts[1].lower() if len(parts) > 1 else "asc"
            sorts.append({field: {"order": direction}})
        return sorts

    def _translate_facets(self, facets: str) -> dict[str, Any]:
        aggs: dict[str, Any] = {}
        for raw in facets.split(","):
            field = self._map_field(raw.strip())
            aggs[field] = {"terms": {"field": field}}
        return aggs
