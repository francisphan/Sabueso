"""Tests for wine-owner detection + public wine research (pure logic)."""

import wine_enrichment as we


class TestCleanEmail:
    def test_lowercases_and_strips(self):
        assert we._clean_email("  Bob@Example.COM ") == "bob@example.com"

    def test_rejects_missing_at(self):
        assert we._clean_email("not-an-email") is None

    def test_rejects_quotes_and_spaces(self):
        # SuiteQL has no binding on this path — quotes/spaces must be refused.
        assert we._clean_email("a'b@x.com") is None
        assert we._clean_email("a b@x.com") is None

    def test_rejects_empty(self):
        assert we._clean_email("") is None
        assert we._clean_email(None) is None


class TestWinemakerQuery:
    def test_matches_primary_and_secondary_email(self):
        q = we._winemaker_query("bob@example.com")
        assert "FROM customer" in q
        assert "custentity_vom_winebrandname IS NOT NULL" in q
        assert "LOWER(email) = 'bob@example.com'" in q
        assert "LOWER(custentity_secondaryemail) = 'bob@example.com'" in q


class TestRowsFromResult:
    def test_bare_list(self):
        assert we._rows_from_result([{"id": 1}]) == [{"id": 1}]

    def test_result_wrapper(self):
        assert we._rows_from_result({"result": [{"id": 2}]}) == [{"id": 2}]

    def test_items_wrapper(self):
        assert we._rows_from_result({"items": [{"id": 3}]}) == [{"id": 3}]

    def test_error_dict_yields_nothing(self):
        assert we._rows_from_result({"error": "boom"}) == []

    def test_filters_error_rows(self):
        assert we._rows_from_result([{"id": 1}, {"error": "x"}]) == [{"id": 1}]

    def test_unexpected_type(self):
        assert we._rows_from_result("nope") == []


class TestIsResearchableBrand:
    def test_real_brand(self):
        assert we._is_researchable_brand("SERCA") is True
        assert we._is_researchable_brand("Dos Abogados") is True

    def test_placeholder_initials_rejected(self):
        # Reinaldo Assunção's brand field is the placeholder "RA".
        assert we._is_researchable_brand("RA") is False

    def test_too_short_rejected(self):
        assert we._is_researchable_brand("Q4") is False

    def test_placeholder_words_rejected(self):
        for v in ("None", "various", "TBC", "n/a", ""):
            assert we._is_researchable_brand(v) is False

    def test_pending_and_notes_markers_rejected(self):
        assert we._is_researchable_brand("VIII (pending approval)") is False
        assert we._is_researchable_brand("Doupé, David NO USAR") is False

    def test_long_notes_paragraph_rejected(self):
        para = "they don't have a brand yet but will use Los Tres Caballeros for the rose"
        assert we._is_researchable_brand(para) is False

    def test_none_input(self):
        assert we._is_researchable_brand(None) is False


class TestParseWineResearch:
    def test_parses_full_payload(self):
        text = (
            '{"found": true, "summary": "A Malbec blend.", "style": "Bordeaux blend", '
            '"blend": "64% Malbec", "tasting_notes": "dark berry", '
            '"producer_background": "Uco Valley", "food_pairing": "steak", '
            '"ratings": [{"source": "Vivino", "score": "4.3", "count": 274}], '
            '"confidence": 7}'
        )
        out = we._parse_wine_research(text)
        assert out["found"] is True
        assert out["summary"] == "A Malbec blend."
        assert out["ratings"][0]["source"] == "Vivino"
        assert out["confidence"] == 7

    def test_strips_markdown_fences(self):
        text = '```json\n{"found": false, "summary": "none"}\n```'
        out = we._parse_wine_research(text)
        assert out["found"] is False
        assert out["summary"] == "none"

    def test_defaults_missing_fields(self):
        out = we._parse_wine_research('{"summary": "x"}')
        assert out["found"] is False
        assert out["style"] == ""
        assert out["ratings"] == []
        assert out["confidence"] == 0

    def test_non_list_ratings_coerced(self):
        out = we._parse_wine_research('{"ratings": "oops"}')
        assert out["ratings"] == []


class TestFindWinemaker:
    def test_returns_owner_on_match(self, monkeypatch):
        captured = {}

        def fake_call(tool, args):
            captured["tool"] = tool
            captured["args"] = args
            return [{
                "id": "123", "companyname": "Weitzman, Sergio",
                "entityid": "Weitzman, Sergio", "brand": "SERCA", "owner_code": "WEIS",
            }]

        monkeypatch.setattr(we, "call_tool", fake_call)
        out = we.find_winemaker("sergio@example.com")
        assert captured["tool"] == "ns_suiteql_query"
        assert out == {
            "customer_id": "123",
            "customer_name": "Weitzman, Sergio",
            "brand": "SERCA",
            "owner_code": "WEIS",
        }

    def test_returns_none_when_no_rows(self, monkeypatch):
        monkeypatch.setattr(we, "call_tool", lambda *a, **k: [])
        assert we.find_winemaker("nobody@example.com") is None

    def test_returns_none_on_bad_email_without_calling(self, monkeypatch):
        def boom(*a, **k):
            raise AssertionError("should not query on an invalid email")

        monkeypatch.setattr(we, "call_tool", boom)
        assert we.find_winemaker("not-an-email") is None

    def test_swallows_mcp_errors(self, monkeypatch):
        def boom(*a, **k):
            raise ConnectionError("MCP down")

        monkeypatch.setattr(we, "call_tool", boom)
        assert we.find_winemaker("sergio@example.com") is None

    def test_disabled_short_circuits(self, monkeypatch):
        monkeypatch.setattr(we, "ENABLED", False)
        monkeypatch.setattr(we, "call_tool",
                            lambda *a, **k: (_ for _ in ()).throw(AssertionError("called")))
        assert we.find_winemaker("sergio@example.com") is None
