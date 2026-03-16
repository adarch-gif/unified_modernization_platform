from __future__ import annotations

import re
from typing import Any


_STRING_COMPARISON_PATTERN = re.compile(r"^(\w+)\s+(eq|ne)\s+'((?:[^']|'')*)'$", re.IGNORECASE)
_SCALAR_COMPARISON_PATTERN = re.compile(r"^(\w+)\s+(eq|ne)\s+(.+)$", re.IGNORECASE)
_SEARCH_IN_PATTERN = re.compile(r"^search\.in\((\w+),\s*'((?:[^']|'')*)'\)$", re.IGNORECASE)


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
            query["query"]["bool"]["filter"].append(translated)

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
        if field not in self._field_map:
            raise ValueError(f"unknown OData field {field!r}; allowed fields: {sorted(self._field_map)}")
        return self._field_map[field]

    def _translate_filter(self, expr: str) -> dict[str, Any]:
        normalized = expr.strip()
        if not normalized:
            raise ValueError("empty OData filter expression")

        stripped = self._strip_outer_parentheses(normalized)
        if stripped != normalized:
            return self._translate_filter(stripped)

        for operator, elastic_clause in [(" or ", "should"), (" and ", "must")]:
            parts = self._split_on_top_level_operator(normalized, operator)
            if len(parts) > 1:
                clauses = [self._translate_filter(part) for part in parts]
                bool_query: dict[str, Any] = {elastic_clause: clauses}
                if elastic_clause == "should":
                    bool_query["minimum_should_match"] = 1
                return {"bool": bool_query}

        if normalized.lower().startswith("not "):
            return {"bool": {"must_not": [self._translate_filter(normalized[4:])]}}

        if match := _SEARCH_IN_PATTERN.match(normalized):
            field = self._map_field(match.group(1))
            values = [self._unescape_string(item.strip()) for item in match.group(2).split(",") if item.strip()]
            return {"terms": {field: values}}

        if match := _STRING_COMPARISON_PATTERN.match(normalized):
            field = self._map_field(match.group(1))
            operator = match.group(2).lower()
            value = self._unescape_string(match.group(3))
            if operator == "eq":
                return {"term": {field: value}}
            return {"bool": {"must_not": [{"term": {field: value}}]}}

        for op, elastic_op in [("gt", "gt"), ("ge", "gte"), ("lt", "lt"), ("le", "lte")]:
            match = re.match(rf"^(\w+)\s+{op}\s+(.+)$", normalized, re.IGNORECASE)
            if match:
                field = self._map_field(match.group(1))
                range_value: float | int | bool | str = self._parse_scalar(match.group(2))
                return {"range": {field: {elastic_op: range_value}}}

        if match := _SCALAR_COMPARISON_PATTERN.match(normalized):
            field = self._map_field(match.group(1))
            operator = match.group(2).lower()
            scalar_value: float | int | bool | str = self._parse_scalar(match.group(3))
            if operator == "eq":
                return {"term": {field: scalar_value}}
            return {"bool": {"must_not": [{"term": {field: scalar_value}}]}}

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

    def _strip_outer_parentheses(self, expr: str) -> str:
        if not expr.startswith("("):
            return expr
        close_index = self._find_matching_close(expr, 0)
        if close_index == len(expr) - 1:
            return expr[1:-1].strip()
        return expr

    def _split_on_top_level_operator(self, expr: str, operator: str) -> list[str]:
        depth = 0
        in_string = False
        parts: list[str] = []
        start = 0
        index = 0
        lower_expr = expr.lower()
        operator_length = len(operator)

        while index < len(expr):
            char = expr[index]
            if char == "'" and (index + 1 >= len(expr) or expr[index + 1] != "'"):
                in_string = not in_string
            elif char == "'" and index + 1 < len(expr) and expr[index + 1] == "'":
                index += 2
                continue
            elif in_string:
                index += 1
                continue
            elif char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
            elif depth == 0 and lower_expr[index : index + operator_length] == operator:
                parts.append(expr[start:index].strip())
                start = index + operator_length
                index += operator_length
                continue
            index += 1

        if parts:
            parts.append(expr[start:].strip())
        return parts or [expr]

    def _find_matching_close(self, expr: str, start_index: int) -> int:
        depth = 0
        in_string = False
        index = start_index
        while index < len(expr):
            if expr[index] == "'" and index + 1 < len(expr) and expr[index + 1] == "'":
                index += 2
                continue
            if expr[index] == "'" and (index + 1 >= len(expr) or expr[index + 1] != "'"):
                in_string = not in_string
            elif in_string:
                index += 1
                continue
            elif expr[index] == "(":
                depth += 1
            elif expr[index] == ")":
                depth -= 1
                if depth == 0:
                    return index
            index += 1
        raise ValueError(f"unbalanced parentheses in OData filter: {expr}")

    @staticmethod
    def _unescape_string(value: str) -> str:
        return value.replace("''", "'")

    def _parse_scalar(self, raw: str) -> float | int | bool | str:
        value = raw.strip()
        if value.startswith("'") and value.endswith("'"):
            return self._unescape_string(value[1:-1])
        lowered = value.lower()
        if lowered == "true":
            return True
        if lowered == "false":
            return False
        try:
            if "." in value:
                return float(value)
            return int(value)
        except ValueError:
            return value
