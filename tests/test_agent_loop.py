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

    def test_single_element_error_list_logs_warning(self, caplog):
        # Agent B's list-tools failure convention is a SINGLE-element
        # [{"error": ...}] list; multi-row lists are data (see
        # TestReviewFixes.test_multi_row_list_with_error_column_not_flagged).
        executor = MagicMock(return_value=[{"error": "partial failure"}])

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


class TestReviewFixes:
    """Review fixes: data-row false positives, email masking, isError surfacing."""

    def teardown_method(self):
        tracing.set_correlation_id(None)

    def test_multi_row_list_with_error_column_not_flagged(self, caplog):
        # A successful query whose rows happen to carry an "error" DATA column
        # (e.g. a log table) must not fire the failure WARNING.
        rows = [
            {"id": 1, "error": "customer complaint text"},
            {"id": 2, "error": ""},
        ]
        executor = MagicMock(return_value=rows)

        with caplog.at_level(logging.INFO, logger="nlp"):
            _run_with([_tool_use_response(), _end_turn_response()], executor)

        assert not [r for r in caplog.records if r.levelno == logging.WARNING]

    def test_single_element_error_list_flagged(self, caplog):
        # Agent B's list-tools failure convention: [{"error": ...}].
        executor = MagicMock(return_value=[{"error": "Bad Request: syntax error near LIMIT"}])

        with caplog.at_level(logging.INFO, logger="nlp"):
            _run_with([_tool_use_response(), _end_turn_response()], executor)

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert warnings and "syntax error" in warnings[0].getMessage()

    def test_warning_masks_emails_in_error_and_args(self, caplog):
        # For lookup tools the args ARE the guest's email; the ungated WARNING
        # must mask local parts in both the error text and the args.
        block = _tool_use_block(
            name="lookup_guest_by_email", arguments={"email": "jane.doe@guest.com"}
        )
        executor = MagicMock(
            return_value={"error": "no guest found for jane.doe@guest.com"}
        )

        with caplog.at_level(logging.INFO, logger="nlp"):
            _run_with(
                [_response([block], "tool_use"), _end_turn_response()], executor
            )

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert warnings
        msg = warnings[0].getMessage()
        assert "jane.doe@" not in msg
        assert "j***@guest.com" in msg

    def test_diagnostics_failure_never_masks_success(self, caplog):
        # If error-detection itself blows up, the successful result must still
        # reach the model untouched (the detection block is defensive, outside
        # the executor try).
        executor = MagicMock(return_value={"rows": [1, 2, 3]})

        with patch("nlp._extract_tool_error", side_effect=RuntimeError("boom")):
            with caplog.at_level(logging.DEBUG, logger="nlp"):
                result, client = _run_with(
                    [_tool_use_response(), _end_turn_response("Done.")], executor
                )

        assert result.text == "Done."
        # The tool_result content passed to Claude is the original payload, not
        # a synthesized failure.
        second_call = client.messages.create.call_args_list[1]
        tool_results = second_call.kwargs["messages"][-1]["content"]
        assert "failed" not in tool_results[0]["content"]
        assert "rows" in tool_results[0]["content"]


class TestMcpIsError:
    def test_is_error_plain_text_becomes_error_dict(self):
        import mcp_client

        result = {
            "content": [{"type": "text", "text": "Error: record not found"}],
            "isError": True,
        }
        assert mcp_client._extract_content(result) == {"error": "Error: record not found"}

    def test_is_error_existing_error_dict_passes_through(self):
        import mcp_client

        result = {
            "content": [{"type": "text", "text": json.dumps({"error": "nope"})}],
            "isError": True,
        }
        assert mcp_client._extract_content(result) == {"error": "nope"}

    def test_normal_result_unaffected(self):
        import mcp_client

        result = {"content": [{"type": "text", "text": json.dumps([{"id": 1}])}]}
        assert mcp_client._extract_content(result) == [{"id": 1}]
