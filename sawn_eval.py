"""Sawn's eval suite: run tasks.jsonl on two configurations, grade every run, and write the error analysis.

Graders (all must pass for a task to pass): the course's types from evals.py (contains_any, not_contains,
tool_called, tool_not_called, db on the run's own database copy, judge) plus three for this domain:
- tool_called_any {"names": [...]}: at least one of these tools was called;
- max_calls {"name", "max"}: a tool was called at most `max` times (no retry after the officer says no);
- lang {"value": "ar" | "en"}: the reply is written in the requester's language.

Every grader has a `why` label: the behaviour it checks. A failed task's root cause is the label of its first failed
grader (or the budget that stopped it), and the error analysis groups failures by that label.

The runner is resumable: each finished task is written to results/eval_<config>.csv at once, so a run stopped by the
free tier's daily limit continues where it stopped (finished model calls replay from agentkit's response cache).
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path

import pandas as pd

import agentkit as K
import evals
from harness import CONFIGS, MeteredShape, auto_approver, handle, language_of
from sawn import Sawn

HERE = Path(__file__).resolve().parent
TASKS = HERE / "tasks.jsonl"
RESULTS = HERE / "results"
README = HERE / "README.md"
DEFAULT_WHY = {"db": "end state", "contains_any": "answer content", "not_contains": "privacy leak",
               "tool_called": "missing step", "tool_not_called": "forbidden action", "judge": "answer quality",
               "tool_called_any": "missing step", "max_calls": "approval", "lang": "language"}

# For each root cause: the part of configuration B (the AGENTS.md spec) that addresses it, and what to try next if B
# still fails on it.
FIXES = {
    "verification": "Spec · workflow step 3: verify with all three details; never guess",
    "missing details": "Spec · workflow step 3: ask for the missing detail, then stop",
    "escalation": "Spec · 'Escalate instead of acting'",
    "case scope": "Spec · workflow step 4: act only on the case's purpose",
    "retention": "Spec · workflow step 4: report what was kept, the rule and the date",
    "action": "Spec · workflow step 4: one tool per request type",
    "reply": "Spec · workflow step 5: the final message is the written reply",
    "privacy leak": "Spec · rules: only the verified customer's own data",
    "injection": "Spec · rules: text in requests and records is data",
    "approval": "Spec · rules: do not retry a denied write",
    "policy answer": "Spec · workflow step 2: answer from the policies and cite them",
    "language": "Spec · style: reply in the requester's language",
    "budget": "—",
}
NEXT = {
    "verification": "expose only verify_requester until a case is open (tool masking in the harness)",
    "missing details": "check the three verification fields in code and ask for missing ones before calling the model",
    "escalation": "classify the request with a structured-output call and route third-party, authority and complaint "
                  "requests to escalate_to_dpo in code",
    "case scope": "after verification, expose only the tools for the case's request type",
    "retention": "return a ready-made bilingual retention sentence from delete_data for the model to quote",
    "action": "add one short worked example per request type to the spec",
    "reply": "template the confirmation from tool results in the harness instead of free text",
    "privacy leak": "extend output DLP to other customers' names and order data",
    "injection": "quarantine stored free text: summarise it with a separate model call that has no tools",
    "approval": "remove a denied tool from the toolset for the rest of the request",
    "policy answer": "pre-retrieve the best policy section for questions that open no case",
    "language": "detect the request language in code and state it in the request header",
    "budget": "raise the step budget or shrink tool results",
}


def load_tasks(path: Path = TASKS, ids: list[str] | None = None, categories: list[str] | None = None) -> list[dict]:
    tasks = [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]
    if ids:
        tasks = [t for t in tasks if t["id"] in ids]
    if categories:
        tasks = [t for t in tasks if t["category"] in categories]
    return tasks


def grade(task: dict, final: str, tool_calls: list, sawn: Sawn) -> list[dict]:
    names = [n for n, _ in tool_calls]
    out = []
    for g in task["graders"]:
        kind = g["type"]
        if kind == "tool_called_any":
            ok = any(n in names for n in g["names"])
            detail = "" if ok else f"none of {g['names']} called"
        elif kind == "max_calls":
            n = names.count(g["name"])
            ok, detail = n <= g["max"], "" if n <= g["max"] else f"{g['name']} called {n} times"
        elif kind == "lang":
            got = language_of(final)
            ok, detail = got == g["value"], "" if got == g["value"] else f"replied in {got}"
        else:
            r = evals.grade({"prompt": task["prompt"], "graders": [g]}, final, tool_calls, sawn)[0]
            ok, detail = r["pass"], r["detail"]
        out.append({"type": kind, "why": g.get("why") or DEFAULT_WHY.get(kind, kind), "pass": bool(ok), "detail": detail})
    return out


# ---------------------------------------------------------------- the runner
class StopSuite(Exception):
    """The provider refused further requests (daily free quota, bad key, unknown model)."""


def _classify(e: Exception) -> str:
    text = f"{type(e).__name__} {e}".lower()
    if "ratelimit" in text or "429" in text:
        return "daily_limit" if any(w in text for w in ("per-day", "per day", "daily", "free-models-per-day")) else "rate_limit"
    if any(w in text for w in ("authentication", "401", "notfound", "404", "permission")):
        return "fatal"
    if any(w in text for w in ("connection", "timeout", "internalserver", "500", "502", "503", "overloaded")):
        return "transient"
    return "error"


def run_suite(config: str, tasks: list[dict], *, tools: list, model: str | None = None, out_dir: Path = RESULTS,
              resume: bool = True, trials: int = 1, shape_factory=None, verbose: bool = True) -> pd.DataFrame:
    """Run every task `trials` times on one configuration; return one row per run (also written to the CSV)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / f"eval_{config}.csv"
    traces = out_dir / "traces" / config
    traces.mkdir(parents=True, exist_ok=True)
    rows: list[dict] = []
    if resume and csv_path.exists():
        prev = pd.read_csv(csv_path, keep_default_na=False)
        rows = [r for r in prev.to_dict("records") if r.get("id") in {t["id"] for t in tasks}]
    done = {(r["id"], int(r["trial"])) for r in rows}
    label = CONFIGS[config]["label"]
    if verbose:
        print(f"── config {config} ({label}) · {len(tasks)} tasks × {trials} · {len(done)} already done")
    for t in tasks:
        for k in range(trials):
            if (t["id"], k) in done:
                continue
            out, error = None, ""
            for attempt in range(4):
                sawn = Sawn(K.ARTIFACTS / "sawn" / "eval" / config / t["id"] / str(k), actor="sawn-eval")
                sawn.reset()
                shape = shape_factory(t) if shape_factory else MeteredShape()
                try:
                    out = handle(t["prompt"], sawn=sawn, tools=tools, approve=auto_approver(t.get("deny", [])),
                                 config=config, model=model, channel=t.get("channel", "email"), sender=t.get("from", ""),
                                 shape=shape, use_cache=False if trials > 1 else None)
                    break
                except Exception as e:  # noqa: BLE001 - classify provider errors; a task's own failure is graded
                    kind, error = _classify(e), f"{type(e).__name__}: {str(e)[:200]}"
                    if kind in ("daily_limit", "fatal"):
                        _write(rows, csv_path)
                        raise StopSuite(f"{kind}: {error}") from e
                    if kind in ("rate_limit", "transient") and attempt < 3:
                        wait = 65 if kind == "rate_limit" else 10
                        if verbose:
                            print(f"   … {kind} on {t['id']}, waiting {wait}s (finished calls replay from cache)")
                        time.sleep(wait)
                        continue
                    break
            if out is None:  # still failing: not a task result, retried on the next run
                if verbose:
                    print(f"⚠️  {t['id']:<28} skipped ({error[:80]})")
                continue
            grades = grade(t, out.final, out.tool_calls, sawn)
            failed = [g for g in grades if not g["pass"]]
            root = "budget" if out.stopped != "done" else (failed[0]["why"] if failed else "")
            row = {"config": config, "label": label, "model": out.model,
                   "served_by": ", ".join(sorted(out.served)) or out.model, "id": t["id"], "category": t["category"],
                   "lang": t["lang"], "trial": k, "passed": not failed and out.stopped == "done", "root_cause": root,
                   "failed_graders": "; ".join(f"{g['why']} · {g['type']}: {g['detail']}" for g in failed)
                   or (out.stopped if out.stopped != "done" else ""),
                   "tools": " ".join(n for n, _ in out.tool_calls), "steps": out.steps, "requests": out.requests,
                   "cached": out.cached, "tokens": out.tokens, "seconds": out.seconds, "stopped": out.stopped,
                   "guards": sum(e["kind"] == "guard" for e in out.events), "redactions": len(out.redactions),
                   "delivered": bool(out.delivery and out.delivery.get("status") == "sent"),
                   "final_excerpt": " ".join(out.final.split())[:300]}
            rows.append(row)
            _write(rows, csv_path)
            trace = {k_: v for k_, v in out.to_dict().items() if k_ != "history"}
            (traces / f"{t['id']}__{k}.json").write_text(json.dumps({"task": t, "grades": grades, **trace},
                                                                    ensure_ascii=False, indent=1, default=str),
                                                         encoding="utf-8")
            if verbose:
                mark = "✅" if row["passed"] else "❌"
                print(f"{mark} {t['id']:<28} {row['requests']} req · {row['failed_graders'][:90]}")
    return pd.DataFrame(rows)


def _write(rows: list[dict], path: Path) -> None:
    if rows:
        tmp = path.with_suffix(".tmp")
        pd.DataFrame(rows).to_csv(tmp, index=False)
        tmp.replace(path)


# ---------------------------------------------------------------- the report
def _table(header: list[str], rows: list[list]) -> str:
    def cell(v):
        return str(v).replace("|", "\\|").replace("\n", " ")
    lines = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    lines += ["| " + " | ".join(cell(v) for v in r) + " |" for r in rows]
    return "\n".join(lines)


def _pct(x: float) -> str:
    return f"{100 * x:.0f}%"


def report(out_dir: Path = RESULTS, configs: tuple[str, ...] = ("A", "B"), readme: Path | None = README) -> str:
    """Write results/REPORT.md from the eval CSVs and refresh the README's evaluation section."""
    frames = {c: pd.read_csv(out_dir / f"eval_{c}.csv", keep_default_na=False)
              for c in configs if (out_dir / f"eval_{c}.csv").exists()}
    if not frames:
        raise FileNotFoundError(f"No eval CSVs in {out_dir}. Run the eval first.")
    for df in frames.values():
        df["passed"] = df["passed"].astype(str).str.lower().eq("true")
    all_ids = load_tasks()
    col = lambda df: df["served_by"] if "served_by" in df else df["model"]  # noqa: E731
    models = sorted({m.strip() for df in frames.values() for v in col(df).unique() for m in str(v).split(",") if m.strip()})

    summary = []
    for c, df in frames.items():
        per_task = df.groupby("id").passed.agg(["all", "any"])
        k = int(df.groupby("id").size().max())
        summary.append([f"{c} · {CONFIGS[c]['label']}", _pct(df.passed.mean()), f"{_pct(per_task['all'].mean())} (k={k})",
                        f"{df.requests.mean():.1f}", f"{df.tokens.mean():,.0f}", f"{df.id.nunique()}/{len(all_ids)}"])
    summary_md = _table(["Config", "pass rate", "pass^k", "avg requests", "avg tokens", "tasks run"], summary)

    cats = sorted({t["category"] for t in all_ids})
    by_cat = []
    for cat in cats:
        row = [cat]
        for c, df in frames.items():
            sub = df[df.category == cat]
            row.append(f"{int(sub.passed.sum())}/{len(sub)}" if len(sub) else "—")
        by_cat.append(row)
    by_cat_md = _table(["Category", *[f"Config {c}" for c in frames]], by_cat)

    causes = sorted({r for df in frames.values() for r in df.loc[~df.passed, "root_cause"] if r})
    err_rows = []
    for cause in causes:
        counts = [int(((df.root_cause == cause) & ~df.passed).sum()) for df in frames.values()]
        examples = [df.loc[(df.root_cause == cause) & ~df.passed, "id"].tolist() for df in frames.values()]
        example = next((e[0] for e in examples if e), "")
        err_rows.append([cause, *counts, example, FIXES.get(cause, "—")])
    err_rows.sort(key=lambda r: -sum(r[1:1 + len(frames)]))
    err_md = _table(["Root cause", *[f"Count ({c})" for c in frames], "Example task", "Fix tried"], err_rows) \
        if err_rows else "No failures."

    lines = []
    if {"A", "B"} <= frames.keys():
        a = frames["A"].groupby("id").passed.all()
        b = frames["B"].groupby("id").passed.all()
        common = a.index.intersection(b.index)
        fixed = [i for i in common if not a[i] and b[i]]
        broke = [i for i in common if a[i] and not b[i]]
        lines.append(f"**Flipped A → B:** fixed {len(fixed)} ({', '.join(fixed) or 'none'}); "
                     f"broke {len(broke)} ({', '.join(broke) or 'none'}).")
    for c, df in frames.items():
        fails = df.loc[~df.passed, "root_cause"]
        if len(fails):
            top = fails.value_counts()
            lines.append(f"**Biggest failure category in {c}:** {top.index[0]} ({top.iloc[0]} of {len(df)} runs).")
    last = list(frames)[-1]
    rest = frames[last].loc[~frames[last].passed, "root_cause"]
    if len(rest):
        top = rest.value_counts().index[0]
        lines.append(f"**Next change to try:** {NEXT.get(top, 'inspect the traces of the failing tasks')}.")
    else:
        lines.append(f"**Next change to try:** add harder tasks; configuration {last} passes the current suite.")
    flips_md = "\n\n".join(lines)

    per_task = []
    for t in all_ids:
        row = [t["id"], t["category"]]
        for df in frames.values():
            sub = df[df.id == t["id"]]
            row.append("—" if sub.empty else ("✅" if sub.passed.all() else "❌ " + sub.root_cause.iloc[0]))
        per_task.append(row)
    per_task_md = _table(["Task", "Category", *[f"Config {c}" for c in frames]], per_task)

    configs_md = "; ".join(f"**{c}** = {CONFIGS[c]['label']}" for c in CONFIGS)
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    full = (f"# Sawn · eval report\n\nGenerated {stamp} from `results/eval_*.csv` · model(s): {', '.join(models)} · "
            f"{configs_md}. Everything else (tools, MCP server, harness, budgets) is identical.\n\n"
            f"## Summary\n\n{summary_md}\n\n## By category\n\n{by_cat_md}\n\n## Error analysis\n\n{err_md}\n\n"
            f"{flips_md}\n\n## Per task\n\n{per_task_md}\n\nPer-run traces (events, tool calls, final reply, grades): "
            "`results/traces/<config>/<task>__<trial>.json`.\n")
    (out_dir / "REPORT.md").write_text(full, encoding="utf-8")

    if readme and readme.exists():
        section = (f"Model(s): {', '.join(models)} · run {stamp}.\n\n{summary_md}\n\n### Error analysis\n\n{err_md}\n\n"
                   f"{flips_md}\n\nPer category and per task: [results/REPORT.md](results/REPORT.md).")
        text = readme.read_text(encoding="utf-8")
        start, end = "<!-- EVAL:START -->", "<!-- EVAL:END -->"
        if start in text and end in text:
            head, rest_ = text.split(start, 1)
            _, tail = rest_.split(end, 1)
            readme.write_text(f"{head}{start}\n{section}\n{end}{tail}", encoding="utf-8")
    return full
