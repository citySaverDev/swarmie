#!/usr/bin/env python3
"""
Swarmie <-> Claude Code bridge.

Runs Swarmie's *own* pipeline stages (pitch parser, archetype generator, swarm
runner, reporter) unchanged, but swaps the HTTP LLM client for a capture/replay
stub. Each time a stage needs an LLM answer, the bridge records the exact
messages Swarmie would have sent ("tasks") and stops. Claude Code subagents
answer those tasks; the answers are fed back and the pipeline is re-run
deterministically (fixed seed) until the report is produced.

No network calls, no API key. Swarmie's prompts, persona sampling, action
dice-rolls, ignore sampling, deterministic metrics, confidence clamping and
PMF index are all reused as-is.

Usage (run from anywhere; uses the backend's uv environment):
  uv run --project backend python claude_sim/bridge.py init   --dir RUN --mode validate --pitch-file pitch.txt [--agents 24] [--seed 7]
  uv run --project backend python claude_sim/bridge.py next   --dir RUN [--allow-missing]
  uv run --project backend python claude_sim/bridge.py answer --dir RUN --file answers.json
  uv run --project backend python claude_sim/bridge.py render --dir RUN   # report.md + report.html

`next` prints a JSON status object:
  {"status": "pending", "stage": "...", "pending_file": ".../pending.json", "n_tasks": N}
  {"status": "done", "report_json": "...", "report_md": "..."}
  {"status": "error", "error": "..."}
"""

from __future__ import annotations

import argparse
import fcntl
import math
import asyncio
import hashlib
import html
import json
import logging
import os
import random
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
BACKEND = HERE.parent / "backend"

# Swarmie's Config requires these at import time. They are never used for a
# network call: every LLM object is replaced by the stub below.
os.environ.setdefault("SECRET_KEY", "claude-sim-local-only")
os.environ.setdefault("LLM_API_KEY", "not-used-by-claude-sim")
os.environ.setdefault("RATE_LIMIT_ENABLED", "false")
sys.path.insert(0, str(BACKEND))

from app.services.swarm.registry import SWARMS, get_swarm  # noqa: E402
from app.services.swarm.pitch_parser import ParsedPitch  # noqa: E402
from app.services.swarm.archetype_generator import Archetype  # noqa: E402

# Capture runs make Swarmie log a "failed" warning for every not-yet-answered
# call. Those are expected; real failures are counted in report meta instead.
logging.getLogger("swarmie").setLevel(logging.ERROR)
logging.getLogger().setLevel(logging.ERROR)

MIN_AGENTS, MAX_AGENTS, DEFAULT_AGENTS = 8, 60, 24
MAX_FAILED_SHARE = 0.5  # more than half the speaking agents failed -> abort
DISCLOSURE = (
    "AI-simulated personas. No real people were interviewed. Treat this as a "
    "hypothesis generator for real customer conversations, not as market research."
)


class NeedAnswers(Exception):
    """Raised by the stub when a stage needs answers that don't exist yet.

    Deliberately NOT a ValueError: Swarmie's archetype generator catches
    ValueError to try a fallback path, and we want the stage to stop instead.
    """


class MissingAnswer(RuntimeError):
    """Raised in --allow-missing mode: behaves like a failed provider call."""


class Store:
    def __init__(self, answers: dict[str, Any], allow_missing: str | None):
        self.answers = answers
        self.allow_missing = allow_missing
        self.pending: list[dict[str, Any]] = []
        self.failed: list[str] = []
        self.counters: Counter[str] = Counter()


class StubLLM:
    """Drop-in for app.utils.llm.LLM: chat/chat_json/achat/achat_json."""

    def __init__(self, store: Store, stage: str, tier: str, kinds: dict[str, str] | None = None):
        self.store = store
        self.stage = stage
        self.tier = tier
        self.kinds = kinds or {}
        self.tracker = None
        self.model = "claude-code-subagent"

    def _key(self, messages: list[dict[str, str]]) -> str:
        digest = hashlib.sha256(json.dumps(messages, sort_keys=True).encode()).hexdigest()[:16]
        occ = self.store.counters[digest]
        self.store.counters[digest] += 1
        return f"{self.stage}:{digest}:{occ}"

    def _resolve(self, messages: list[dict[str, str]]) -> dict[str, Any]:
        key = self._key(messages)
        ans = self.store.answers.get(key)
        if isinstance(ans, dict):
            return ans
        if self.store.allow_missing == self.stage:
            self.store.failed.append(key)
            raise MissingAnswer(f"no answer for {key}")
        system = next((m["content"] for m in messages if m["role"] == "system"), "")
        user = "\n\n".join(m["content"] for m in messages if m["role"] != "system")
        self.store.pending.append({
            "key": key,
            "stage": self.stage,
            "kind": self.kinds.get(system, self.stage),
            "tier": self.tier,
            "system": system,
            "user": user,
        })
        raise NeedAnswers(key)

    def chat_json(self, messages, temperature=None, max_tokens=None, model=None):
        return self._resolve(messages)

    def chat(self, messages, temperature=None, max_tokens=None, response_format=None, model=None):
        return json.dumps(self._resolve(messages))

    async def achat_json(self, messages, temperature=None, max_tokens=None, model=None):
        return self._resolve(messages)

    async def achat(self, messages, temperature=None, max_tokens=None, response_format=None, model=None):
        return json.dumps(self._resolve(messages))


# --------------------------------------------------------------------------- io

def _load(path: Path, default: Any) -> Any:
    return json.loads(path.read_text()) if path.exists() else default


def _dump(path: Path, data: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    os.replace(tmp, path)


def _emit(obj: dict[str, Any]) -> int:
    print(json.dumps(obj, indent=2))
    return 0 if obj.get("status") != "error" else 2


# --------------------------------------------------------------------- pipeline

def _run_pipeline(run_dir: Path, allow_missing: str | None) -> dict[str, Any]:
    cfg = _load(run_dir / "input.json", None)
    if cfg is None:
        return {"status": "error", "error": f"{run_dir}/input.json missing; run `init` first"}
    answers = _load(run_dir / "answers.json", {})
    spec = get_swarm(cfg["mode"])
    store = Store(answers, allow_missing)
    stage = "parse"
    try:
        # 1. parse
        parser = spec.parser_cls.__new__(spec.parser_cls)
        parser.llm = StubLLM(store, "parse", "deep")
        pitch: ParsedPitch = parser.parse(cfg["pitch_text"])
        if not pitch.icp_segments:
            return {"status": "error", "stage": stage,
                    "error": "Pitch parser returned no ICP segments. Add a clearer AUDIENCE line and retry."}

        # 2. archetypes
        stage = "archetypes"
        gen = spec.archgen_cls.__new__(spec.archgen_cls)
        gen.llm = StubLLM(store, "archetypes", "deep")
        archetypes: list[Archetype] = gen.generate(pitch, n_archetypes=cfg["n_archetypes"])

        # 3. swarm reactions (Swarmie's own sampling + prompts, seeded)
        stage = "reactions"
        runner_cls = spec.runner_cls
        kinds = {runner_cls.REACTION_SYSTEM: "reaction", runner_cls.IGNORE_SYSTEM: "ignore"}
        runner = runner_cls(
            cheap_tier_llm=StubLLM(store, "reactions", "cheap", kinds),
            deep_tier_llm=StubLLM(store, "reactions", "deep", kinds),
            max_cost_usd=1e9,  # cost is plan usage, bounded by agent count instead
        )
        random.seed(cfg["seed"])
        reactions = asyncio.run(runner.run(pitch, archetypes, n_agents=cfg["n_agents"], concurrency=4))
        if store.pending:
            raise NeedAnswers("reactions")
        speaking = [r for r in reactions if r.action in ("comment", "post")]
        failed_speaking = [r for r in speaking if not r.text]
        if speaking and len(failed_speaking) / len(speaking) > MAX_FAILED_SHARE:
            return {"status": "error", "stage": stage,
                    "error": f"{len(failed_speaking)}/{len(speaking)} speaking agents failed; "
                             "too many to produce an honest report. Re-run."}

        # 4. report (Swarmie's reporter: deterministic metrics + synthesis)
        stage = "report"
        reporter = spec.reporter_cls.__new__(spec.reporter_cls)
        reporter.llm = StubLLM(store, "report", "synth")
        report = reporter.report(pitch, reactions)
        if store.pending:
            raise NeedAnswers("report")
    except NeedAnswers:
        _dump(run_dir / "pending.json", store.pending)
        return {"status": "pending", "stage": stage, "n_tasks": len(store.pending),
                "batches": _n_batches(store.pending), "pending_file": str(run_dir / "pending.json")}
    except MissingAnswer as exc:
        return {"status": "error", "stage": stage,
                "error": f"The {stage} step failed ({exc}). This step is required; re-run it."}
    except Exception as exc:  # an answer Swarmie's own code couldn't use (e.g. no archetypes)
        dropped = [k for k in answers if k.startswith(f"{stage}:")]
        for k in dropped:
            answers.pop(k)
        _dump(run_dir / "answers.json", answers)
        return {"status": "retry", "stage": stage, "dropped_answers": len(dropped),
                "error": f"Swarmie could not use the {stage} answer ({type(exc).__name__}: {str(exc)[:200]}). "
                         "Those answers were discarded; run `next` again to re-request them."}

    out = report.to_dict()
    synth_failed = [k for k in store.failed if k.startswith("report:")]
    out["meta"] = {
        "engine": "swarmie (hp-8/swarmie) pipeline, answered by Claude Code subagents",
        "mode": cfg["mode"],
        "swarm_label": spec.label,
        "name": cfg.get("name", ""),
        "n_agents": cfg["n_agents"],
        "n_archetypes": len(archetypes),
        "seed": cfg["seed"],
        "created_at": cfg["created_at"],
        "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "speaking_agents": len(speaking),
        "failed_agent_calls": len([k for k in store.failed if k.startswith("reactions:")]),
        "synthesis_fallback_used": bool(synth_failed),
        "llm_tasks_answered": len([a for a in answers.values() if isinstance(a, dict) and not a.get("__failed__")]),
        "disclosure": DISCLOSURE,
    }
    out["pitch"] = pitch.to_dict()
    out["pitch_text"] = cfg["pitch_text"]
    out["archetypes"] = [a.to_dict() for a in archetypes]
    out["reactions"] = [r.to_dict() for r in reactions]
    _dump(run_dir / "report.json", out)
    (run_dir / "pending.json").unlink(missing_ok=True)
    (run_dir / "report.md").write_text(render_markdown(out))
    (run_dir / "report.html").write_text(render_html(out))
    return {"status": "done", "report_json": str(run_dir / "report.json"),
            "report_md": str(run_dir / "report.md"), "report_html": str(run_dir / "report.html"),
            "meta": out["meta"]}


# ---------------------------------------------------------------------- render

_VERDICT_TEXT = {
    "ship_it": "Ship it", "sharpen_positioning": "Sharpen positioning",
    "wrong_audience": "Wrong audience", "kill": "Kill",
    "go": "Go", "sharpen": "Sharpen", "hold": "Hold",
    "fundable": "Fundable", "sharpen_story": "Sharpen the story", "wrong_stage": "Wrong stage",
    "not_fundable": "Not fundable",
}


def _verdict(v: str) -> str:
    return _VERDICT_TEXT.get(v, v.replace("_", " ").capitalize())


def render_markdown(r: dict[str, Any]) -> str:
    m, L = r["meta"], []
    L += [f"# Swarmie {m['swarm_label']} simulation: {r['headline']}", "",
          f"> **{DISCLOSURE}**", "",
          f"- **Verdict:** {_verdict(r['verdict'])} (confidence: {r['confidence']}): {r['verdict_reason']}",
          f"- **Next action:** {r['next_action']}",
          f"- **Confidence note:** {r['confidence_reason']}",
          f"- **PMF score (Swarmie heuristic, uncalibrated):** {r['pmf_score']}/10",
          f"- **Agents:** {m['n_agents']} simulated ({m['speaking_agents']} spoke, "
          f"{m['failed_agent_calls']} failed calls) from {m['n_archetypes']} archetypes; seed {m['seed']}",
          "", "## Narrative", "", r["narrative"], ""]
    s = r["sentiment_split"]
    a = r["action_split"]
    L += ["## Sentiment and actions", "",
          f"Positive {s.get('positive', 0):.0f}% · Neutral {s.get('neutral', 0):.0f}% · Negative {s.get('negative', 0):.0f}%", "",
          " · ".join(f"{k}: {v}" for k, v in a.items()), ""]
    L += ["## Top objections", ""]
    for o in r["top_objections"]:
        L += [f"### {o['category']} ({o['count']})", f"> {o['example_quote']}" if o["example_quote"] else "",
              f"- **Ask 5 real users:** {o.get('real_test', '')}",
              f"- **Kill criteria:** {o.get('kill_criteria', '')}",
              f"- **Suggested fix:** {o.get('suggested_fix', '')}", ""]
    if r.get("messaging_gaps"):
        L += ["## Weaknesses / messaging gaps", ""] + [f"- {g}" for g in r["messaging_gaps"]] + [""]
    L += ["## Segment differences", "", "| Segment | Agents | Spoke | Avg sentiment | Dominant action |", "|---|---|---|---|---|"]
    for seg, v in r["icp_fit"].items():
        L.append(f"| {seg} | {v['count']} | {v['speaking_count']} | {v['avg_sentiment']:+.2f} | {v['dominant_action']} |")
    L += ["", f"## Why they scrolled past ({r['silent_share_pct']:.0f}% silent)", ""]
    for ir in r.get("ignore_reasons", []):
        L.append(f"- **{ir['label']}** ({ir['share_pct']:.0f}% of sampled): {ir.get('example', '')}. {ir.get('implication', '')}")
    lb = r.get("launch_brief")
    if lb:
        L += ["", "## Launch brief", ""]
        for key, title in (("questions", "Questions the thread will ask"), ("confusion", "Likely confusion"),
                           ("risks", "Risks"), ("themes", "Dominant themes"), ("next_actions", "Before going live")):
            if lb.get(key):
                L += [f"**{title}**", ""] + [f"- {x}" for x in lb[key]] + [""]
        if lb.get("playbook"):
            L += ["**Reply playbook**", ""] + [f"- *{p['trigger']}*: {p['response']}" for p in lb["playbook"]] + [""]
    L += ["", "## Recommended real-world tests", ""]
    tests = [o["real_test"] for o in r["top_objections"] if o.get("real_test")]
    L += [f"{i}. {t}" for i, t in enumerate(tests, 1)] or ["- (none produced)"]
    L += ["", "## Representative reactions", ""]
    for q in r["quoted_reactions"]:
        L.append(f"- **{q['name']}** ({q['segment']}, {q['tone']}, {q['sentiment']:+.1f}): {q['text']}")
    L += ["", "---", f"Generated {m['finished_at']} by {m['engine']}. {DISCLOSURE}", ""]
    return "\n".join(L)


def render_html(r: dict[str, Any]) -> str:
    tpl = (HERE / "report_template.html").read_text()
    payload = dict(r, _markdown=render_markdown(r))
    data = json.dumps(payload, ensure_ascii=False).replace("<", "\\u003c")
    name = r["meta"].get("name") or "Swarmie"
    title = html.escape(f"{name} {r['meta']['swarm_label']} Swarm")
    return tpl.replace("__TITLE__", title).replace("__REPORT_JSON__", data)


# -------------------------------------------------------------------- batching

PERSONAS_PER_BATCH, MAX_BATCHES = 6, 6


def _n_batches(tasks: list[dict[str, Any]]) -> int:
    """Persona tasks are spread over a few subagents; synthesis-style tasks get one each."""
    persona = [t for t in tasks if t["kind"] in ("reaction", "ignore")]
    if not persona:
        return len(tasks)
    return max(1, min(MAX_BATCHES, math.ceil(len(persona) / PERSONAS_PER_BATCH)))


def _batch(tasks: list[dict[str, Any]], i: int, n: int) -> list[dict[str, Any]]:
    persona = [t for t in tasks if t["kind"] in ("reaction", "ignore")]
    if not persona:
        return [tasks[i]] if i < len(tasks) else []
    return persona[i::n]  # round-robin so each batch gets a mix of archetypes and actions


# -------------------------------------------------------------------- commands

def cmd_init(a: argparse.Namespace) -> int:
    mode = a.mode.lower()
    if mode not in SWARMS:
        return _emit({"status": "error", "error": f"unknown mode '{mode}'; choose from {sorted(SWARMS)}"})
    pitch = Path(a.pitch_file).read_text().strip()
    if not pitch:
        return _emit({"status": "error", "error": "pitch file is empty"})
    if len(pitch) > 20000:
        return _emit({"status": "error", "error": "pitch too long (max 20000 chars, same as Swarmie)"})
    n = max(MIN_AGENTS, min(int(a.agents), MAX_AGENTS))
    run_dir = Path(a.dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    for f in ("answers.json", "pending.json", "report.json", "report.md", "report.html"):
        (run_dir / f).unlink(missing_ok=True)
    cfg = {
        "mode": mode, "pitch_text": pitch, "n_agents": n,
        "n_archetypes": get_swarm(mode).n_archetypes,  # same as the web app (roast.py)
        "name": re.sub(r"[^A-Za-z0-9 ._-]", "", a.name or "").strip()[:40],
        "seed": int(a.seed) if a.seed is not None else random.SystemRandom().randint(1, 10**6),
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    _dump(run_dir / "input.json", cfg)
    return _emit({"status": "initialized", "dir": str(run_dir), **{k: cfg[k] for k in ("mode", "n_agents", "n_archetypes", "seed")},
                  "clamped": n != int(a.agents)})


def cmd_next(a: argparse.Namespace) -> int:
    run_dir = Path(a.dir)
    with open(run_dir / ".answers.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _emit(_run_pipeline(run_dir, a.allow_missing))


def cmd_show(a: argparse.Namespace) -> int:
    """Print one batch of pending tasks, grouping the shared system prompts."""
    tasks = _load(Path(a.dir) / "pending.json", [])
    batch = _batch(tasks, a.batch, a.of)
    if a.only_missing:
        done = _load(Path(a.dir) / "answers.json", {})
        batch = [t for t in batch if t["key"] not in done]
    systems: dict[str, str] = {}
    items = []
    for t in batch:
        sid = systems.setdefault(t["system"], f"S{len(systems) + 1}")
        items.append({"key": t["key"], "kind": t["kind"], "system_id": sid, "user": t["user"]})
    print(json.dumps({"batch": a.batch, "of": a.of, "n_tasks": len(items),
                      "system_prompts": {v: k for k, v in systems.items()}, "tasks": items},
                     indent=2, ensure_ascii=False))
    return 0


def cmd_answer(a: argparse.Namespace) -> int:
    run_dir = Path(a.dir)
    with open(run_dir / ".answers.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)  # parallel subagents submit concurrently
        return _answer_locked(a, run_dir)


def _answer_locked(a: argparse.Namespace, run_dir: Path) -> int:
    pending = {t["key"]: t for t in _load(run_dir / "pending.json", [])}
    try:
        incoming = json.loads(Path(a.file).read_text())
    except (OSError, json.JSONDecodeError) as exc:
        return _emit({"status": "error", "error": f"answers file is not valid JSON: {exc}"})
    if isinstance(incoming, dict) and "answers" in incoming:
        incoming = incoming["answers"]
    if not isinstance(incoming, list):
        return _emit({"status": "error", "error": "answers file must be a JSON array of {key, response}"})
    incoming = [i for i in incoming if isinstance(i, dict)] + [
        {"key": None, "response": None} for i in incoming if not isinstance(i, dict)]
    answers = _load(run_dir / "answers.json", {})
    accepted, rejected = 0, []
    for item in incoming:
        key, resp = item.get("key"), item.get("response")
        if key not in pending:
            rejected.append({"key": key, "why": "not a pending task"})
            continue
        if isinstance(resp, str):
            try:
                resp = json.loads(resp)
            except json.JSONDecodeError:
                rejected.append({"key": key, "why": "response is not JSON"})
                continue
        if not isinstance(resp, dict):
            rejected.append({"key": key, "why": "response must be a JSON object"})
            continue
        answers[key] = resp
        accepted += 1
    _dump(run_dir / "answers.json", answers)
    missing = [k for k in pending if k not in answers]
    file_keys = {i.get("key") for i in incoming}
    return _emit({"status": "answers_saved", "accepted": accepted, "rejected": rejected,
                  "still_missing_overall": len(missing),
                  "batch_keys_missing": [k for k in missing if k in file_keys]})


def cmd_render(a: argparse.Namespace) -> int:
    run_dir = Path(a.dir)
    r = _load(run_dir / "report.json", None)
    if r is None:
        return _emit({"status": "error", "error": "no report.json yet"})
    (run_dir / "report.md").write_text(render_markdown(r))
    (run_dir / "report.html").write_text(render_html(r))
    return _emit({"status": "rendered", "report_md": str(run_dir / "report.md"), "report_html": str(run_dir / "report.html")})


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("init"); s.add_argument("--dir", required=True); s.add_argument("--mode", default="validate")
    s.add_argument("--pitch-file", required=True); s.add_argument("--agents", type=int, default=DEFAULT_AGENTS)
    s.add_argument("--seed", type=int); s.add_argument("--name", default="")
    s.set_defaults(fn=cmd_init)
    s = sub.add_parser("next"); s.add_argument("--dir", required=True)
    s.add_argument("--allow-missing", choices=["reactions", "report"],
                   help="treat still-unanswered tasks of THIS stage as failed calls (never other stages)")
    s.set_defaults(fn=cmd_next)
    s = sub.add_parser("answer"); s.add_argument("--dir", required=True); s.add_argument("--file", required=True)
    s.set_defaults(fn=cmd_answer)
    s = sub.add_parser("show"); s.add_argument("--dir", required=True); s.add_argument("--batch", type=int, default=0)
    s.add_argument("--of", type=int, default=1)
    s.add_argument("--only-missing", action="store_true"); s.set_defaults(fn=cmd_show)
    s = sub.add_parser("render"); s.add_argument("--dir", required=True); s.set_defaults(fn=cmd_render)
    a = p.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
