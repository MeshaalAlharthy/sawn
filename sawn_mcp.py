# /// script
# requires-python = ">=3.11"
# dependencies = ["mcp>=2.3"]
# ///
"""Nakhla Retail's privacy policies as an MCP server (read-only).

    uv run sawn/sawn_mcp.py                 # stdio: the agent launches it as a subprocess
    uv run sawn/sawn_mcp.py --http 8766     # Streamable HTTP on http://127.0.0.1:8766/mcp

Exposes:
- tools: `search_policies` (keyword search over SW-01 … SW-05) and `read_policy` (one full policy);
- resources: the index `sawn://policies` and each policy as `policy://SW-03`.

The server holds no customer data and has no write tools. Customer tools stay in the agent's process (sawn.py),
where each run gets its own database copy and every write passes the harness's approval gate.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(1, str(HERE.parent))

import sawn  # noqa: E402 - standard library only
from mcp.server.mcpserver import MCPServer  # noqa: E402
from mcp.server.mcpserver.exceptions import ResourceNotFoundError, ToolError  # noqa: E402


def build() -> MCPServer:
    server = MCPServer(
        "sawn-policies",
        title="Nakhla Retail privacy policies",
        instructions="Privacy policies of Nakhla Retail (fictional): data-subject rights, identity verification, "
                     "retention, request handling and escalation. Cite the policy ID (SW-01 … SW-05).",
        version="1.0.0",
        log_level="WARNING",
    )

    @server.tool()
    def search_policies(query: str, k: int = 3) -> dict:
        """Search Nakhla's privacy policies (rights, verification, retention, request steps, escalation).

        Works with Arabic or English queries. Returns the best-matching sections with their policy ID.

        Args:
            query: what to look for, e.g. "how long are invoices kept" or "طلب نيابة عن شخص آخر".
            k: how many sections to return (1-5).
        """
        hits = sawn.search_policy_text(query, k=max(1, min(int(k), 5)))
        if not hits:
            return {"results": [], "note": "No section matched. Try other words, or read_policy (SW-01 … SW-05)."}
        return {"results": hits}

    @server.tool()
    def read_policy(policy_id: str) -> str:
        """Read one full policy by ID.

        Args:
            policy_id: SW-01 rights, SW-02 verification, SW-03 retention, SW-04 handling, SW-05 scope and escalation.
        """
        for p in sawn.load_policies():
            if p["id"].lower() == policy_id.strip().lower():
                return p["text"]
        raise ToolError(f"No policy {policy_id!r}. IDs: SW-01 … SW-05.")

    @server.resource("sawn://policies", mime_type="text/markdown")
    def policy_index() -> str:
        """The list of policies with their IDs and titles."""
        return "\n".join(f"- {p['id']}: {p['title']} ({p['title_ar']})" for p in sawn.load_policies())

    @server.resource("policy://{policy_id}", mime_type="text/markdown")
    def policy(policy_id: str) -> str:
        """One policy document, e.g. policy://SW-03."""
        for p in sawn.load_policies():
            if p["id"].lower() == policy_id.lower():
                return p["text"]
        raise ResourceNotFoundError(f"No policy {policy_id!r}. Read sawn://policies for the list.")

    return server


def stdio_transport(log_path: Path | None = None):
    """Client-side transport that launches this server with `uv run` over stdio.

    The server's stderr is written to a log file instead of the client's sys.stderr: notebook kernels (Colab) replace
    sys.stderr with an object that has no file descriptor, and a subprocess cannot write to it.
    """
    import shutil

    from mcp import StdioServerParameters
    from mcp.client.stdio import stdio_client

    uv = shutil.which("uv") or shutil.which("uv", path=str(Path(sys.executable).parent)) or "uv"
    log_path = Path(log_path or HERE / "logs" / "mcp_server.log")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    params = StdioServerParameters(command=uv, args=["run", "--quiet", str(Path(__file__).resolve())])
    return stdio_client(params, errlog=log_path.open("a", encoding="utf-8"))


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--http", type=int, metavar="PORT", help="serve Streamable HTTP on this port instead of stdio")
    a = ap.parse_args()
    srv = build()
    if a.http:
        srv.run(transport="streamable-http", host="127.0.0.1", port=a.http)
    else:
        srv.run()
