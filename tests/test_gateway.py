from unified_modernization.gateway.odata import ODataTranslator


def test_translate_basic_search_and_filter() -> None:
    translator = ODataTranslator({"Status": "status"})
    query = translator.translate(
        {
            "$search": "gold customer",
            "$filter": "Status eq 'ACTIVE'",
            "$top": "10",
        }
    )

    assert query["size"] == 10
    assert query["query"]["bool"]["must"][0]["multi_match"]["query"] == "gold customer"
    assert query["query"]["bool"]["filter"][0] == {"term": {"status": "ACTIVE"}}


def test_translate_orderby_and_facets() -> None:
    translator = ODataTranslator({"Tier": "tier"})
    query = translator.translate(
        {
            "$orderby": "Tier desc",
            "$facets": "Tier",
        }
    )

    assert query["sort"] == [{"tier": {"order": "desc"}}]
    assert query["aggs"]["tier"]["terms"]["field"] == "tier"
