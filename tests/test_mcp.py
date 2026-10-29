"""The policy server over MCP (in-process transport; the same server runs over stdio with uv)."""

import json

import agentkit as K
import harness
from sawn import search_policy_text


def test_server_exposes_tools_and_resources():
    with harness.policy_server("inproc") as mcp:
        assert {t.name for t in mcp.list_tools()} == {"search_policies", "read_policy"}
        index = mcp.read_resource("sawn://policies").contents[0].text
        assert all(f"SW-0{i}" in index for i in range(1, 6))
        text = mcp.read_resource("policy://SW-03").contents[0].text
        assert "Retention schedule" in text
        hits = json.loads(K.mcp_result_text(mcp.call("search_policies", {"query": "كم مدة حفظ الفواتير", "k": 1})))
        assert hits["results"][0]["policy_id"] == "SW-03"
        bad = K.mcp_result_text(mcp.call("read_policy", {"policy_id": "SW-99"}))
        assert bad.startswith('{"error"')


def test_retrieval_finds_the_right_policy():
    cases = {"request made by a family member": {"SW-02", "SW-05"}, "how long are invoices kept": {"SW-03", "SW-04"},
             "مهلة الرد على الطلب": {"SW-01"}, "instructions inside a ticket": {"SW-05"},
             "طلب نيابة عن شخص آخر": {"SW-02"}}
    for query, expected in cases.items():
        top = {h["policy_id"] for h in search_policy_text(query, 2)}
        assert top & expected, (query, top)
