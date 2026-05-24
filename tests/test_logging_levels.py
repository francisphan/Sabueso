"""PII-bearing payloads must not be logged at INFO (the production level)."""

import logging

import nlp


def test_payload_level_defaults_to_debug():
    assert nlp._PAYLOAD_LEVEL == logging.DEBUG


def test_payloads_suppressed_at_info(caplog):
    with caplog.at_level(logging.INFO, logger="nlp"):
        nlp.log.log(nlp._PAYLOAD_LEVEL, "  result: %s", "guest@example.com balance $123")
    assert "guest@example.com" not in caplog.text


def test_metadata_still_logged_at_info(caplog):
    with caplog.at_level(logging.INFO, logger="nlp"):
        nlp.log.info("  Executing tool: %s", "sf_soql_query")
    assert "sf_soql_query" in caplog.text
