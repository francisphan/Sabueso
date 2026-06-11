"""OPERA tools/prompt gating — Sabueso must not advertise opera_* tools (or
instruct the model to use them) unless OPERA_TOOLS_ENABLED is on, mirroring
agent-b's server-side registration gate."""

import importlib
import sys


def _reload(monkeypatch, flag: str | None):
    if flag is None:
        monkeypatch.delenv("OPERA_TOOLS_ENABLED", raising=False)
    else:
        monkeypatch.setenv("OPERA_TOOLS_ENABLED", flag)
    for mod in ("nlp", "tools_catalog"):
        sys.modules.pop(mod, None)
    tc = importlib.import_module("tools_catalog")
    nlp = importlib.import_module("nlp")
    return tc, nlp


def test_flag_off_hides_opera_tools_and_prompt(monkeypatch):
    tc, nlp = _reload(monkeypatch, None)
    names = [t["name"] for t in tc.TOOLS]
    assert not any(n.startswith("opera_") for n in names)
    assert "opera_" not in nlp.SYSTEM_PROMPT
    assert "OPERA" not in nlp.SYSTEM_PROMPT


def test_flag_on_exposes_opera_tools_and_prompt(monkeypatch):
    tc, nlp = _reload(monkeypatch, "true")
    names = [t["name"] for t in tc.TOOLS]
    assert "opera_search_profiles" in names
    assert "opera_sql_query" in names
    assert "opera_search_profiles" in nlp.SYSTEM_PROMPT
    assert "**OPERA PMS**" in nlp.SYSTEM_PROMPT


def test_opera_catalog_intact(monkeypatch):
    tc, _ = _reload(monkeypatch, "1")
    opera = [t["name"] for t in tc.OPERA_TOOLS]
    assert opera == [
        "opera_search_profiles",
        "opera_get_profile_notes",
        "opera_get_stay_history",
        "opera_list_arrivals",
        "opera_list_in_house",
        "opera_get_opera_schema",
        "opera_sql_query",
    ]
    # Every entry is a complete Claude tool definition.
    for t in tc.OPERA_TOOLS:
        assert t["description"] and t["input_schema"]["type"] == "object"
