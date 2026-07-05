"""Tests for the agentic tool-use loop (run_agent) — error visibility and
log attribution (Sabueso#29, #30).

The loop is exercised with a mocked Anthropic client (nlp._get_client) and a
mocked tool_executor, so no network or MCP server is involved. Fake response
objects mimic anthropic.types.Message: a `.content` list of blocks (each with a
`.type` and the type-specific attrs), a `.stop_reason`, and a `.usage`.
"""

import json
import logging
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import nlp
import tracing


def _usage():
    return SimpleNamespace(
        input_tokens=10,
        output_tokens=5,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=0,
    )


def _text_block(text):
    return SimpleNamespace(type="text", text=text)


def _tool_use_block(name="sf_soql_query", arguments=None, block_id="tu_1"):
    return SimpleNamespace(
        type="tool_use",
        name=name,
        input=arguments or {"query": "SELECT Id FROM Account"},
        id=block_id,
    )


def _response(content, stop_reason):
    return SimpleNamespace(content=content, stop_reason=stop_reason, usage=_usage())


def _tool_use_response():
    return _response([_tool_use_block()], stop_reason="tool_use")


def _end_turn_response(text="Here is your answer."):
    return _response([_text_block(text)], stop_reason="end_turn")


def _run_with(create_side_effect, tool_executor):
    """Patch the Anthropic client so messages.create yields our fake responses."""
    mock_client = MagicMock()
    if callable(create_side_effect):
        mock_client.messages.create.side_effect = create_side_effect
    else:
        mock_client.messages.create.side_effect = list(create_side_effect)
    with patch("nlp._get_client", return_value=mock_client):
        result = nlp.run_agent("who owns this wine?", tool_executor)
    return result, mock_client


class TestErrorVisibility:
    def teardown_method(self):
        tracing.set_correlation_id(None)

    def test_error_dict_result_logs_warning_and_loop_continues(self, caplog):
        """(a) A tool returning {"error": ...} logs a WARNING and the loop keeps
        going to a final answer."""
        executor = MagicMock(return_value={"error": "ORA-00904 invalid identifier: BALANCE"})

        with caplog.at_level(logging.INFO, logger="nlp"):
            result, client = _run_with(
                [_tool_use_response(), _end_turn_response("Done.")],
                executor,
            )

        # Loop continued past the failing tool to the end_turn answer.
        assert result.text == "Done."
        assert client.messages.create.call_count == 2

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert any(
            "returned an error" in r.getMessage()
            and "sf_soql_query" in r.getMessage()
            and "ORA-00904" in r.getMessage()
            for r in warnings
        ), f"expected an error WARNING, got: {[r.getMessage() for r in warnings]}"

    def test_error_dict_warning_ungated_and_carries_corr(self, caplog):
        """The failure WARNING fires without SABUESO_DEBUG_PAYLOADS and is tagged
        with the correlation id."""
        executor = MagicMock(return_value={"error": "boom"})
        assert nlp._PAYLOAD_LEVEL == logging.DEBUG  # payload gating still off

        with caplog.at_level(logging.WARNING, logger="nlp"):
            result, _ = _run_with(
                [_tool_use_response(), _end_turn_response()],
                executor,
            )

        warn = next(r for r in caplog.records if "returned an error" in r.getMessage())
        assert "corr=" in warn.getMessage()
        assert result.correlation_id and result.correlation_id in warn.getMessage()

    def test_list_result_with_embedded_error_logs_warning(self, caplog):
        executor = MagicMock(return_value=[{"ok": 1}, {"error": "partial failure"}])

        with caplog.at_level(logging.WARNING, logger="nlp"):
            _run_with([_tool_use_response(), _end_turn_response()], executor)

        assert any(
            "partial failure" in r.getMessage()
            for r in caplog.records
            if r.levelno == logging.WARNING
        )

    def test_executor_exception_forwards_clipped_error_to_model(self):
        """(b) When the executor raises, the real (clipped) message is placed in
        the tool_result content sent on the next Claude call — not a generic
        'logged for investigation' string."""
        executor = MagicMock(side_effect=RuntimeError("kaboom detailed reason"))

        snapshots = []
        responses = [_tool_use_response(), _end_turn_response("recovered")]

        def fake_create(**kwargs):
            # Snapshot the messages list at call time (it is mutated in place).
            snapshots.append(json.dumps(kwargs["messages"], default=str))
            return responses.pop(0)

        result, client = _run_with(fake_create, executor)

        assert client.messages.create.call_count == 2
        assert result.text == "recovered"
        # The clipped real error reached the second Claude call's tool_result.
        assert "kaboom detailed reason" in snapshots[1]
        assert "logged for investigation" not in snapshots[1]

    def test_no_generic_investigation_string_on_exception(self):
        executor = MagicMock(side_effect=ValueError("MCP error -32600: bad request"))
        result, _ = _run_with(
            [_tool_use_response(), _end_turn_response("ok")], executor
        )
        assert result.text == "ok"


class TestExitLineClarity:
    def teardown_method(self):
        tracing.set_correlation_id(None)

    def test_no_tools_branch_logs_stop_reason(self, caplog):
        """(c) The (no tools) exit line records the actual stop_reason."""
        executor = MagicMock()
        with caplog.at_level(logging.INFO, logger="nlp"):
            result, _ = _run_with(
                [_response([_text_block("I can't help with that.")], stop_reason="max_tokens")],
                executor,
            )

        assert result.text == "I can't help with that."
        executor.assert_not_called()
        done = next(r for r in caplog.records if "AGENT DONE (no tools)" in r.getMessage())
        assert "stop_reason=max_tokens" in done.getMessage()

    def test_max_steps_warns_with_stop_reason_and_reaches_user(self, caplog):
        """(d) Exhausting MAX_STEPS logs a WARNING with stop_reason and returns
        the intended (non-dead) apology message to the user."""
        executor = MagicMock(return_value={"rows": []})

        # Always hand back a tool_use response so the loop never ends on its own.
        def always_tool_use(**kwargs):
            return _tool_use_response()

        with caplog.at_level(logging.INFO, logger="nlp"):
            result, client = _run_with(always_tool_use, executor)

        assert result.max_steps_hit is True
        assert result.steps == nlp.MAX_STEPS
        assert client.messages.create.call_count == nlp.MAX_STEPS

        # The fallback message actually reaches the user (the old `or` fallback
        # was dead code masked by _extract_text's placeholder).
        assert "I couldn't find anything to report." not in result.text
        assert "scent" in result.text.lower()

        warn = next(r for r in caplog.records if "HIT MAX STEPS" in r.getMessage())
        assert "stop_reason=" in warn.getMessage()
        assert warn.levelno == logging.WARNING

    def test_max_steps_prefers_model_text_when_present(self):
        """If the model emitted text alongside its final tool call, that text is
        preferred over the canned apology."""
        executor = MagicMock(return_value={"rows": []})

        def tool_use_with_text(**kwargs):
            return _response(
                [_text_block("Still working on it..."), _tool_use_block()],
                stop_reason="tool_use",
            )

        result, _ = _run_with(tool_use_with_text, executor)
        assert result.max_steps_hit is True
        assert result.text == "Still working on it..."
