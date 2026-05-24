"""Tests that result/failure formatting escapes Slack mrkdwn from SF-derived text."""

import handlers


class TestResultFormattingEscapesMrkdwn:
    def test_created_opportunity_escapes_person_name(self):
        out = handlers._format_intent_result(
            "sf_create_opportunity_for_person",
            {"status": "ok", "person_name": "<!channel>", "touches": []},
        )
        assert "<!channel>" not in out
        assert "&lt;!channel&gt;" in out

    def test_ambiguous_person_escapes_candidate_fields(self):
        out = handlers._format_failure(
            "ambiguous_person",
            {"candidates": [{"name": "<https://evil.com|Salesforce>",
                             "email": "<!here>"}]},
            "create the opportunity",
        )
        assert "<https://evil.com|Salesforce>" not in out
        assert "<!here>" not in out
        assert "&lt;" in out

    def test_create_failed_escapes_error(self):
        out = handlers._format_failure(
            "create_failed", {"error": "boom <!channel>"}, "create the opportunity"
        )
        assert "<!channel>" not in out

    def test_ambiguous_opp_escapes_fields(self):
        out = handlers._format_failure(
            "ambiguous_opp",
            {"candidates": [{"name": "<@U123>", "stage": "Prospecting"}]},
            "log the touch",
        )
        assert "<@U123>" not in out
        assert "Prospecting" in out
