"""The eval suite itself: task file, grader validation with oracles, the runner and the report."""

import json
from collections import Counter

import pytest

import sawn_eval as E
from offline import SCRIPTS, ScriptedShape, null_agent, script_for
from sawn import Sawn

TASKS = E.load_tasks()
KNOWN = {"contains_any", "not_contains", "tool_called", "tool_not_called", "db", "judge", "tool_called_any",
         "max_calls", "lang"}


def test_task_file():
    ids = [t["id"] for t in TASKS]
    assert len(ids) >= 15 and len(ids) == len(set(ids))
    assert len({t["category"] for t in TASKS}) >= 6
    assert Counter(t["lang"] for t in TASKS)["ar"] >= 6
    for t in TASKS:
        assert t["graders"], t["id"]
        for g in t["graders"]:
            assert g["type"] in KNOWN and g.get("why") in E.FIXES, (t["id"], g)
            if g["type"] == "db":
                Sawn().sql(g["sql"])  # the query runs on the template
    assert set(SCRIPTS) == set(ids), "every task needs an oracle"


@pytest.fixture(scope="module")
def oracle_and_null(mcp_tools, tmp_path_factory):
    out = tmp_path_factory.mktemp("results")
    good = E.run_suite("B", TASKS, tools=mcp_tools, out_dir=out, verbose=False,
                       shape_factory=lambda t: ScriptedShape(script_for(task_id=t["id"])))
    bad = E.run_suite("A", TASKS, tools=mcp_tools, out_dir=out, verbose=False,
                      shape_factory=lambda t: ScriptedShape(null_agent))
    return out, good, bad


def test_every_task_passes_with_its_oracle(oracle_and_null):
    _, good, _ = oracle_and_null
    failing = good[~good.passed][["id", "failed_graders"]].values.tolist()
    assert not failing, failing


def test_every_task_fails_with_a_do_nothing_agent(oracle_and_null):
    _, _, bad = oracle_and_null
    assert not bad.passed.any(), bad[bad.passed].id.tolist()


def test_runner_resumes_and_report_is_written(oracle_and_null, mcp_tools, tmp_path):
    out, good, _ = oracle_and_null
    again = E.run_suite("B", TASKS, tools=mcp_tools, out_dir=out, verbose=False,
                        shape_factory=lambda t: (_ for _ in ()).throw(AssertionError("should be skipped")))
    assert len(again) == len(good)
    readme = tmp_path / "README.md"
    readme.write_text("before\n<!-- EVAL:START -->\nold\n<!-- EVAL:END -->\nafter\n", encoding="utf-8")
    text = E.report(out_dir=out, readme=readme)
    assert "## Error analysis" in text and (out / "REPORT.md").exists()
    patched = readme.read_text(encoding="utf-8")
    assert "old" not in patched and "Flipped A → B" in patched and patched.startswith("before")
    trace = json.loads((out / "traces" / "B" / "deletion-fahad__0.json").read_text(encoding="utf-8"))
    assert trace["grades"] and trace["events"][0]["kind"] == "request"


def test_custom_graders():
    task = {"prompt": "x", "graders": [{"type": "tool_called_any", "names": ["a", "b"]},
                                       {"type": "max_calls", "name": "d", "max": 1}, {"type": "lang", "value": "ar"}]}
    ok = E.grade(task, "تم الحذف", [("b", {}), ("d", {})], Sawn())
    assert all(g["pass"] for g in ok)
    bad = E.grade(task, "Done", [("d", {}), ("d", {})], Sawn())
    assert [g["pass"] for g in bad] == [False, False, False]


@pytest.mark.parametrize("message, kind", [
    ("RateLimitError: Rate limit exceeded: free-models-per-day", "daily_limit"),
    ("RateLimitError: 429 free-models-per-min", "rate_limit"),
    ("APIConnectionError: Connection error.", "transient"),
    ("AuthenticationError: 401 No auth credentials found", "fatal"),
])
def test_provider_errors_are_classified(message, kind):
    assert E._classify(Exception(message)) == kind
