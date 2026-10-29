# /// script
# requires-python = ">=3.11"
# dependencies = ["fastapi>=0.150", "uvicorn>=0.40", "openai>=3.30", "pydantic>=2.12", "python-dotenv>=1.1",
#                 "pandas>=2.2", "mcp>=2.3", "opentelemetry-sdk>=1.45"]
# ///
"""Sawn as an HTTP service, with a web console where a privacy officer runs requests and approves each action.

    uv run sawn/sawn_api.py                    # http://127.0.0.1:8000 (needs OPENROUTER_API_KEY)
    uv run sawn/sawn_api.py --offline          # scripted stand-in model for the sample requests, no API key

The console (GET /) and every endpoint under /api use the same API: requests sent with the X-API-Key header
(keys from SAWN_API_KEYS, comma-separated; default "officer-dev-key").

    GET  /healthz                          liveness: model, MCP tools, database
    GET  /api/inbox                        the sample requests, with the status of their latest run
    POST /api/runs                         start a request: {"text" | "inbox_id", "channel", "sender", "config"}
    GET  /api/runs/{id}?since=N            live state: events from N, pending approval with its preview, final reply
    POST /api/runs/{id}/decision           {"approve": true | false} for the pending approval
    POST /api/runs/{id}/message            a follow-up from the same requester (conversation memory, compacted)
    GET  /api/runs                         recent runs
    GET  /api/records/{table}              customers (masked) | cases | exports | outbox | escalations | audit
    POST /api/reset                        restore the demo database and clear the runs

Each request runs in a background thread. When the agent calls a write tool, the harness's approval hook blocks that
thread until the officer decides in the console (or SAWN_APPROVAL_TIMEOUT seconds pass, which counts as a denial).
OpenTelemetry spans (invoke_agent → chat / execute_tool, GenAI attributes) go to artifacts/traces/sawn_api.jsonl.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import uuid
from contextlib import ExitStack, asynccontextmanager
from pathlib import Path

HERE = Path(__file__).resolve().parent
LABS = HERE.parent
for p in (str(LABS), str(HERE)):
    if p not in sys.path:
        sys.path.insert(0, p)

from fastapi import Depends, FastAPI, Header, HTTPException, Query  # noqa: E402
from fastapi.responses import FileResponse  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

import agentkit as K  # noqa: E402
import harness as H  # noqa: E402
from sawn import DATA, Sawn, mask_id  # noqa: E402

API_KEYS = {k.strip() for k in os.getenv("SAWN_API_KEYS", "officer-dev-key").split(",") if k.strip()}
APPROVAL_TIMEOUT = float(os.getenv("SAWN_APPROVAL_TIMEOUT", "900"))
MAX_TURNS = 6  # follow-ups per run
RECORDS = {
    "customers": "SELECT customer_id, name_en, name_ar, email, mobile, national_id, city, status, created_at "
                 "FROM customers ORDER BY customer_id",
    "cases": "SELECT * FROM cases ORDER BY opened_at DESC, case_id",
    "exports": "SELECT * FROM exports ORDER BY export_id DESC",
    "outbox": "SELECT message_id, to_addr, subject, body, case_ids, created_at FROM outbox ORDER BY message_id DESC",
    "escalations": "SELECT * FROM escalations ORDER BY escalation_id DESC",
    "audit": "SELECT rowid AS n, ts, actor, action, detail FROM audit_log ORDER BY rowid DESC LIMIT 300",
}


# ---------------------------------------------------------------- traces
def tracer_for(path: Path):
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter, SpanExportResult

    class JsonlExporter(SpanExporter):
        def export(self, spans):
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as f:
                for s in spans:
                    f.write(json.dumps({"name": s.name, "trace_id": f"{s.context.trace_id:032x}",
                                        "span_id": f"{s.context.span_id:016x}",
                                        "parent_id": f"{s.parent.span_id:016x}" if s.parent else None,
                                        "ms": round((s.end_time - s.start_time) / 1e6, 1),
                                        "attributes": dict(s.attributes or {})}, ensure_ascii=False, default=str) + "\n")
            return SpanExportResult.SUCCESS

    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(JsonlExporter()))
    return provider.get_tracer("sawn")


# ---------------------------------------------------------------- runs
class Run:
    def __init__(self, request: str, channel: str, sender: str, inbox_id: str | None, config: str):
        self.id = uuid.uuid4().hex[:10]
        self.created = time.time()
        self.request, self.channel, self.sender, self.inbox_id, self.config = request, channel, sender, inbox_id, config
        self.status = "running"  # running | awaiting_approval | done | error
        self.events: list[dict] = []
        self.pending: dict | None = None
        self.decision = False
        self.decided = threading.Event()
        self.history: list = []
        self.turns: list[dict] = []
        self.lock = threading.Lock()

    def add(self, ev: dict) -> None:
        with self.lock:
            self.events.append({**ev, "i": len(self.events), "t": round(time.time() - self.created, 2)})

    def view(self, since: int = 0) -> dict:
        last = self.turns[-1] if self.turns else {}
        return {"id": self.id, "status": self.status, "created": self.created, "request": self.request,
                "channel": self.channel, "sender": self.sender, "inbox_id": self.inbox_id, "config": self.config,
                "events": self.events[since:], "event_count": len(self.events), "pending": self.pending,
                "turns": len(self.turns), **last}


class Console:
    """The officer's workspace: one live database copy, the MCP policy tools, and the runs."""

    def __init__(self, workdir: Path, offline: bool, model: str | None, tracer):
        self.sawn = Sawn(workdir, actor="sawn-api")
        self.offline, self.model, self.tracer = offline, model, tracer
        self.tools: list = []
        self.runs: dict[str, Run] = {}

    @property
    def model_label(self) -> str:
        from offline import OFFLINE_LABEL

        return OFFLINE_LABEL if self.offline else (self.model or K.MODEL)

    def start(self, run: Run) -> Run:
        self.runs[run.id] = run
        threading.Thread(target=self._turn, args=(run, run.request), daemon=True).start()
        return run

    def follow_up(self, run: Run, text: str) -> None:
        run.status = "running"
        threading.Thread(target=self._turn, args=(run, text), daemon=True).start()

    def decide(self, run: Run, approve: bool) -> None:
        run.decision = approve
        run.decided.set()

    def _approve(self, run: Run, name: str, args: dict) -> bool:
        p = H.preview(self.sawn, name, args) if name != "deliver_reply" else {"tool": name, "args": args}
        run.decided.clear()
        run.pending = {"tool": name, "args": args, "preview": p, "since": time.time()}
        run.status = "awaiting_approval"
        decided = run.decided.wait(APPROVAL_TIMEOUT)
        ok = run.decision if decided else False
        if not decided:
            run.add({"kind": "guard", "text": f"no decision within {APPROVAL_TIMEOUT:.0f}s: {name} denied"})
        run.pending, run.status = None, "running"
        return ok

    def _turn(self, run: Run, text: str) -> None:
        open_spans: dict[str, list] = {}

        def on_event(ev: dict) -> None:
            run.add(ev)
            tool = ev.get("tool") or (ev["text"] if ev["kind"] == "call" else None)
            if ev["kind"] == "call":
                span = self.tracer.start_span(f"execute_tool {tool}", attributes={
                    "gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": tool})
                open_spans.setdefault(tool, []).append(span)
            elif tool in open_spans and open_spans[tool]:
                if ev["kind"] == "approval":
                    open_spans[tool][0].set_attribute("sawn.approved", bool(ev.get("approved")))
                    if not ev.get("approved"):
                        open_spans[tool].pop(0).end()
                elif ev["kind"] in ("result", "error", "guard"):
                    span = open_spans[tool].pop(0)
                    if ev["kind"] != "result":
                        span.set_attribute("error.type", ev["kind"])
                    span.end()

        if self.offline:
            from offline import ScriptedShape, script_for

            shape = ScriptedShape(script_for(text, inbox_id=run.inbox_id if not run.turns else None))
        else:
            shape = H.MeteredShape()
        shape.tracer = self.tracer
        with self.tracer.start_as_current_span("invoke_agent Sawn") as span:
            span.set_attribute("gen_ai.operation.name", "invoke_agent")
            span.set_attribute("gen_ai.agent.name", "Sawn")
            span.set_attribute("gen_ai.conversation.id", run.id)
            span.set_attribute("sawn.config", run.config)
            o = H.handle(text, sawn=self.sawn, tools=self.tools, approve=lambda n, a: self._approve(run, n, a),
                         config=run.config, model=self.model, channel=run.channel, sender=run.sender,
                         history=H.compact(run.history) if run.history else None, shape=shape, on_event=on_event,
                         raise_errors=False)
            span.set_attribute("sawn.stopped", o.stopped)
            span.set_attribute("gen_ai.usage.input_tokens", o.tokens)
        for spans in open_spans.values():
            for s in spans:
                s.end()
        if o.history:
            run.history = o.history
        run.turns.append({"final": o.final, "raw_final": o.raw_final, "delivery": o.delivery, "cases": o.cases,
                          "redactions": o.redactions, "stopped": o.stopped, "error": o.error, "steps": o.steps,
                          "requests": o.requests, "tokens": o.tokens, "model": o.model, "seconds": o.seconds})
        run.status = "error" if o.stopped == "error" else "done"


# ---------------------------------------------------------------- the app
class RunIn(BaseModel):
    text: str = Field(default="", max_length=6000)
    inbox_id: str | None = Field(default=None, pattern=r"^REQ-\d{3}$")
    channel: str = Field(default="web_form", pattern=r"^[a-z_]{2,20}$")
    sender: str = Field(default="", max_length=200)
    config: str = Field(default=H.DEFAULT_CONFIG, pattern=r"^[AB]$")


class DecisionIn(BaseModel):
    approve: bool


class MessageIn(BaseModel):
    text: str = Field(min_length=1, max_length=6000)


def load_inbox() -> list[dict]:
    return [json.loads(line) for line in (DATA / "inbox.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]


def create_app(workdir: Path | None = None, offline: bool = False, mcp: str = "stdio", model: str | None = None,
               trace_file: Path | None = None) -> FastAPI:
    console = Console(workdir or K.lab_dir("sawn/api"), offline, model,
                      tracer_for(trace_file or K.ARTIFACTS / "traces" / "sawn_api.jsonl"))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        stack = ExitStack()
        bridge = stack.enter_context(H.policy_server(mcp))
        console.tools = bridge.agent_tools()
        try:
            yield
        finally:
            for run in console.runs.values():  # release threads waiting for a decision
                console.decide(run, False)
            stack.close()

    app = FastAPI(title="Sawn API", version="1.0.0", lifespan=lifespan)
    app.state.console = console

    def api_key(x_api_key: str = Header(default="")) -> str:
        if x_api_key not in API_KEYS:
            raise HTTPException(401, "Missing or invalid X-API-Key header.")
        return x_api_key

    def get_run(run_id: str) -> Run:
        run = console.runs.get(run_id)
        if not run:
            raise HTTPException(404, f"No run {run_id}.")
        return run

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(HERE / "static" / "index.html", media_type="text/html")

    @app.get("/healthz")
    def healthz() -> dict:
        return {"ok": bool(console.tools), "model": console.model_label, "offline": console.offline,
                "mcp_tools": [t.__name__ for t in console.tools], "configs": {k: v["label"] for k, v in H.CONFIGS.items()},
                "default_config": H.DEFAULT_CONFIG, "runs": len(console.runs)}

    @app.get("/api/inbox", dependencies=[Depends(api_key)])
    def inbox() -> list[dict]:
        latest: dict[str, Run] = {}
        for run in console.runs.values():
            if run.inbox_id:
                latest[run.inbox_id] = run
        return [{**r, "run_id": latest[r["id"]].id if r["id"] in latest else None,
                 "status": latest[r["id"]].status if r["id"] in latest else "new"} for r in load_inbox()]

    @app.post("/api/runs", dependencies=[Depends(api_key)])
    def start(req: RunIn) -> dict:
        text, channel, sender = req.text.strip(), req.channel, req.sender.strip()
        if req.inbox_id:
            item = next((r for r in load_inbox() if r["id"] == req.inbox_id), None)
            if not item:
                raise HTTPException(404, f"No inbox request {req.inbox_id}.")
            text, channel, sender = item["body"], item["channel"], item["from_addr"]
        if not text:
            raise HTTPException(422, "Give the request text or an inbox_id.")
        if not console.tools:
            raise HTTPException(503, "The policy server is not connected yet.")
        run = console.start(Run(text, channel, sender, req.inbox_id, req.config))
        return {"run_id": run.id}

    @app.get("/api/runs", dependencies=[Depends(api_key)])
    def runs() -> list[dict]:
        out = []
        for run in sorted(console.runs.values(), key=lambda r: -r.created)[:50]:
            v = run.view(len(run.events))
            out.append({k: v.get(k) for k in ("id", "status", "created", "inbox_id", "config", "turns", "stopped")}
                       | {"request": run.request[:160]})
        return out

    @app.get("/api/runs/{run_id}", dependencies=[Depends(api_key)])
    def run_state(run_id: str, since: int = Query(default=0, ge=0)) -> dict:
        return get_run(run_id).view(since)

    @app.post("/api/runs/{run_id}/decision", dependencies=[Depends(api_key)])
    def decision(run_id: str, req: DecisionIn) -> dict:
        run = get_run(run_id)
        if not run.pending:
            raise HTTPException(409, "Nothing is waiting for approval in this run.")
        tool = run.pending["tool"]
        console.decide(run, req.approve)
        return {"run_id": run.id, "tool": tool, "approved": req.approve}

    @app.post("/api/runs/{run_id}/message", dependencies=[Depends(api_key)])
    def message(run_id: str, req: MessageIn) -> dict:
        run = get_run(run_id)
        if run.status in ("running", "awaiting_approval"):
            raise HTTPException(409, "This run is still working. Wait for it to finish.")
        if len(run.turns) >= MAX_TURNS:
            raise HTTPException(429, f"A run takes at most {MAX_TURNS} turns. Start a new run.")
        run.add({"kind": "request", "text": req.text.strip(), "channel": run.channel, "sender": run.sender,
                 "follow_up": True})
        console.follow_up(run, req.text.strip())
        return {"run_id": run.id, "turn": len(run.turns) + 1}

    @app.get("/api/records/{table}", dependencies=[Depends(api_key)])
    def records(table: str) -> list[dict]:
        if table not in RECORDS:
            raise HTTPException(404, f"Unknown table {table!r}. Tables: {', '.join(RECORDS)}.")
        rows = console.sawn.sql(RECORDS[table])
        if table == "customers":
            for r in rows:
                r["national_id"] = mask_id(r["national_id"])
                r["mobile"] = mask_id(r["mobile"])
        return rows

    @app.post("/api/reset", dependencies=[Depends(api_key)])
    def reset() -> dict:
        for run in console.runs.values():
            if run.pending:
                console.decide(run, False)
        console.runs.clear()
        console.sawn.reset()
        return {"ok": True}

    return app


def serve_in_background(port: int = 8000, host: str = "127.0.0.1", **kw):
    """Start the service in a thread (for notebooks): returns the uvicorn server; stop it with `server.should_exit = True`."""
    import httpx
    import uvicorn

    server = uvicorn.Server(uvicorn.Config(create_app(**kw), host=host, port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(240):
        try:
            if httpx.get(f"http://{host}:{port}/healthz", timeout=2).json().get("ok"):
                return server
        except Exception:  # noqa: BLE001 - not up yet
            pass
        time.sleep(0.5)
    raise TimeoutError(f"The service did not start on port {port}.")


if __name__ == "__main__":
    import uvicorn

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--offline", action="store_true", help="scripted stand-in model for the sample requests")
    ap.add_argument("--mcp", choices=["stdio", "inproc"], default="stdio")
    ap.add_argument("--model", default=None)
    a = ap.parse_args()
    if not a.offline:
        K.api_key()
    print(f"Sawn console: http://{a.host}:{a.port}  (API key: {sorted(API_KEYS)[0]})")
    uvicorn.run(create_app(offline=a.offline, mcp=a.mcp, model=a.model), host=a.host, port=a.port, log_level="warning")
