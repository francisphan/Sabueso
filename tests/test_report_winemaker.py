"""Tests for the 'Wine Owner' section of the guest report card."""

import report_builder as rb


def _guest(**extra):
    g = {"first_name": "Sergio", "last_name": "Weitzman", "check_in": "", "check_out": ""}
    g.update(extra)
    return g


class TestWinemakerSection:
    def test_empty_for_non_winemaker(self):
        assert rb._winemaker_section(_guest()) == ""

    def test_shows_brand_and_owner_code(self):
        html = rb._winemaker_section(_guest(winemaker={"brand": "SERCA", "owner_code": "WEIS"}))
        assert "Wine Owner at The Vines" in html
        assert "SERCA" in html
        assert "WEIS" in html

    def test_renders_research_detail_and_ratings(self):
        wm = {
            "brand": "SERCA",
            "owner_code": "WEIS",
            "research": {
                "found": True,
                "summary": "A Malbec-led Bordeaux blend from the Uco Valley.",
                "style": "Bordeaux blend",
                "blend": "64% Malbec, 24% Merlot, 12% Cabernet Franc",
                "tasting_notes": "dark berry, vanilla",
                "producer_background": "Sergio + Carolina",
                "food_pairing": "steak",
                "ratings": [{"source": "Vivino", "score": "4.3", "count": 274}],
                "sources": ["https://example.com/serca"],
            },
        }
        html = rb._winemaker_section(_guest(winemaker=wm))
        assert "Malbec-led Bordeaux blend" in html
        assert "64% Malbec" in html
        assert "Vivino: 4.3 (274 ratings)" in html
        assert 'href="https://example.com/serca"' in html

    def test_no_research_shows_fallback(self):
        html = rb._winemaker_section(_guest(winemaker={"brand": "SERCA", "owner_code": "WEIS"}))
        assert "No public information found" in html

    def test_not_found_research_shows_fallback(self):
        wm = {"brand": "SERCA", "research": {"found": False, "summary": "nope"}}
        html = rb._winemaker_section(_guest(winemaker=wm))
        assert "No public information found" in html

    def test_escapes_research_html(self):
        wm = {"brand": "SERCA", "research": {"found": True, "summary": "<script>x</script>"}}
        html = rb._winemaker_section(_guest(winemaker=wm))
        assert "<script>" not in html
        assert "&lt;script&gt;" in html

    def test_hides_overlong_brand_value(self):
        # A notes-paragraph brand value must not be dumped as the label name.
        para = "x" * 80
        html = rb._winemaker_section(_guest(winemaker={"brand": para, "owner_code": "ABC"}))
        assert para not in html
        assert "ABC" in html  # owner code still shown

    def test_section_appears_in_full_card(self):
        card = rb._guest_card(_guest(winemaker={"brand": "SERCA", "owner_code": "WEIS"}))
        assert 'class="winemaker"' in card
