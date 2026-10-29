# /// script
# requires-python = ">=3.11"
# dependencies = ["openai>=3.30", "pydantic>=2.12", "python-dotenv>=1.1", "pandas>=2.2", "mcp>=2.3"]
# ///
"""Sawn (صَوْن) from the command line: one request, a conversation, the eval suite, its report, and the attack.

    uv run sawn/sawn_agent.py inbox                              # the sample requests in data/inbox.jsonl
    uv run sawn/sawn_agent.py ask --inbox REQ-001                # one request; each write asks for your approval
    uv run sawn/sawn_agent.py ask "أبي نسخة من بياناتي ..."         # any request text
    uv run sawn/sawn_agent.py chat --session s1                  # a conversation with one requester (memory kept)
    uv run sawn/sawn_agent.py eval --config A B                  # the eval suite on both configurations (resumable)
    uv run sawn/sawn_agent.py report                             # results/REPORT.md + the README's evaluation section
    uv run sawn/sawn_agent.py attack                             # stored prompt injection, naive vs defended
    uv run sawn/sawn_agent.py attack --live                      # the same attack through the real model

Run from labs/ of the course repository: Sawn imports agentkit, evals and madar from there. Needs OPENROUTER_API_KEY
in labs/.env, except with --offline (a scripted stand-in model, for trying the harness without an API key).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
LABS = HERE.parent
for p in (str(LABS), str(HERE)):
    if p not in sys.path:
        sys.path.insert(0, p)

import agentkit as K  # noqa: E402
import harness as H  # noqa: E402
from sawn import DATA, Sawn  # noqa: E402

WORKDIR = K.ARTIFACTS / "sawn" / "cli"


def load_inbox() -> list[dict]:
    return [json.loads(line) for line in (DATA / "inbox.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]


def model_shape(args, request: str = "", inbox_id: str | None = None) -> H.MeteredShape:
    if args.offline:
        from offline import ScriptedShape, script_for

        return ScriptedShape(script_for(request, inbox_id=inbox_id))
    return H.MeteredShape()


def show_outcome(o: H.Outcome) -> None:
    print(f"\n{'─' * 100}\n{o.final}\n{'─' * 100}")
    delivery = o.delivery["status"] + (f" → {o.delivery.get('to')}" if o.delivery.get("to") else "") if o.delivery else \
        "no email (nothing was changed)"
    print(f"stopped: {o.stopped} · steps {o.steps} · model requests {o.requests} ({o.cached} from cache) · "
          f"tokens {o.tokens:,} · cases {o.cases or '—'} · reply: {delivery}")


def cmd_inbox(args) -> int:
    for r in load_inbox():
        print(f"{r['id']}  [{r['channel']:<10}] {r['from_addr'] or '—':<34} {' '.join(r['body'].split())[:90]}")
    return 0


def cmd_ask(args) -> int:
    sawn = Sawn(WORKDIR)
    if args.reset:
        sawn.reset()
    channel, sender, text, inbox_id = "email", "", args.request, None
    if args.inbox:
        item = next((r for r in load_inbox() if r["id"] == args.inbox.upper()), None)
        if not item:
            print(f"No inbox request {args.inbox}. Run `inbox` for the list.")
            return 1
        channel, sender, text, inbox_id = item["channel"], item["from_addr"], item["body"], item["id"]
    if not text:
        print("Give the request text, or --inbox REQ-001.")
        return 1
    approve = H.auto_approver() if args.yes else H.cli_approver(sawn)
    with H.policy_server(args.mcp) as mcp:
        o = H.handle(text, sawn=sawn, tools=mcp.agent_tools(), approve=approve, config=args.config, model=args.model,
                     channel=channel, sender=sender, shape=model_shape(args, text, inbox_id), on_event=H.print_event,
                     raise_errors=False)
    show_outcome(o)
    return 0 if o.stopped == "done" else 1


def cmd_chat(args) -> int:
    sawn = Sawn(WORKDIR)
    path = WORKDIR / f"session-{args.session}.json"
    history = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    print("Conversation with one requester. Empty line to quit. History:", path)
    with H.policy_server(args.mcp) as mcp:
        tools = mcp.agent_tools()
        while (msg := input("\n🧑 ").strip()):
            o = H.handle(msg, sawn=sawn, tools=tools, approve=H.cli_approver(sawn), config=args.config,
                         model=args.model, channel="chat", history=H.compact(history), shape=model_shape(args, msg),
                         on_event=H.print_event if args.verbose else None, raise_errors=False)
            print("🤖", o.final)
            if o.delivery:
                print(f"✉️  reply {o.delivery['status']}" + (f" → {o.delivery['to']}" if o.delivery.get("to") else ""))
            if o.history:
                history = o.history
                path.write_text(json.dumps(history, ensure_ascii=False, default=str), encoding="utf-8")
    return 0


def cmd_eval(args) -> int:
    import sawn_eval as E

    tasks = E.load_tasks(Path(args.tasks), ids=args.ids)
    if args.limit:
        tasks = tasks[: args.limit]
    out_dir = Path(args.out)
    if args.offline and out_dir.resolve() == (HERE / "results").resolve():
        out_dir = out_dir / "offline"  # scripted runs never mix with model results or touch the README
    if args.fresh:
        for c in args.config:
            (out_dir / f"eval_{c}.csv").unlink(missing_ok=True)
    factory = None
    if args.offline:
        from offline import ScriptedShape, script_for

        def factory(t):
            return ScriptedShape(script_for(task_id=t["id"]))
    with H.policy_server(args.mcp) as mcp:
        tools = mcp.agent_tools()
        for c in args.config:
            try:
                E.run_suite(c, tasks, tools=tools, model=args.model, out_dir=out_dir, trials=args.trials,
                            shape_factory=factory)
            except E.StopSuite as e:
                print(f"\n⛔ Stopped: {e}\nFinished tasks are saved in {out_dir}/eval_{c}.csv. Run the same command "
                      "again later (after the daily reset for the free tier): finished tasks are skipped and "
                      "finished model calls replay from the cache.")
                return 2
    if not args.offline:
        print(f"\n{K.LEDGER}")
    try:
        summary = E.report(out_dir=out_dir, readme=E.README if out_dir.resolve() == E.RESULTS.resolve() else None)
        print("\n" + summary.split("## By category")[0])
    except FileNotFoundError:
        pass
    return 0


def cmd_report(args) -> int:
    import sawn_eval as E

    print(E.report(out_dir=Path(args.out)))
    return 0


def cmd_attack(args) -> int:
    import redteam

    with H.policy_server(args.mcp) as mcp:
        redteam.run("live" if args.live else "offline", mcp_tools=mcp.agent_tools(), model=args.model,
                    out_dir=Path(args.out))
    print(f"\nwritten to {Path(args.out) / ('attack_live.md' if args.live else 'attack_offline.md')}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--model", default=None, help=f"model ID (default: AGENT_MODEL or {K.MODEL})")
    common.add_argument("--mcp", choices=["stdio", "inproc"], default="stdio",
                        help="policy server over stdio with uv (default) or in-process")
    common.add_argument("--offline", action="store_true",
                        help="scripted stand-in model: no API key, sample requests only")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("inbox", parents=[common])
    a = sub.add_parser("ask", parents=[common])
    a.add_argument("request", nargs="?", default="")
    a.add_argument("--inbox", help="a request ID from data/inbox.jsonl, e.g. REQ-001")
    a.add_argument("--config", choices=list(H.CONFIGS), default=H.DEFAULT_CONFIG)
    a.add_argument("--yes", action="store_true", help="approve every write without asking (demo only)")
    a.add_argument("--reset", action="store_true", help="restore the demo database first")
    c = sub.add_parser("chat", parents=[common])
    c.add_argument("--session", default="default")
    c.add_argument("--config", choices=list(H.CONFIGS), default=H.DEFAULT_CONFIG)
    c.add_argument("--verbose", action="store_true")
    e = sub.add_parser("eval", parents=[common])
    e.add_argument("--tasks", default=str(HERE / "tasks.jsonl"))
    e.add_argument("--config", nargs="+", default=["A", "B"], choices=list(H.CONFIGS))
    e.add_argument("--ids", nargs="*", help="only these task IDs")
    e.add_argument("--limit", type=int, help="only the first N tasks (develop on a few, then run all)")
    e.add_argument("--trials", type=int, default=1, help="runs per task; >1 bypasses the cache for pass^k")
    e.add_argument("--fresh", action="store_true", help="discard previous results for these configs first")
    e.add_argument("--out", default=str(HERE / "results"))
    r = sub.add_parser("report", parents=[common])
    r.add_argument("--out", default=str(HERE / "results"))
    t = sub.add_parser("attack", parents=[common])
    t.add_argument("--live", action="store_true", help="use the real model instead of the scripted compromised one")
    t.add_argument("--out", default=str(HERE / "results"))
    args = ap.parse_args()
    if (not args.offline and args.cmd in ("ask", "chat", "eval")) or (args.cmd == "attack" and args.live):
        K.api_key()  # fail early with the course's message if the key is missing
    return {"inbox": cmd_inbox, "ask": cmd_ask, "chat": cmd_chat, "eval": cmd_eval, "report": cmd_report,
            "attack": cmd_attack}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
