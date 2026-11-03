"""Bridge tests. All LLM answers here are canned fixtures (no model involved);
they exercise the plumbing, not simulation quality."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bridge  # noqa: E402
from app.services.swarm.roast_reporter import deterministic_report  # noqa: E402
from app.services.swarm.swarm_runner import AgentReaction  # noqa: E402

VALIDATE_PITCH = """PROBLEM: Small-city finance directors miss savings hidden in vendor contracts.
PRODUCT: Scans municipal spending exports and flags duplicate and overpriced contracts.
AUDIENCE: Finance directors in US cities under 100k people.
PRICING: $500/month per city, annual contract.
COMPETITORS: OpenGov, consultants, spreadsheets."""


def fake_answer(task: dict, i: int) -> dict:
    kind = task["kind"]
    if kind == "parse":
        return {"one_liner": "Find savings in city spending", "problem": "missed savings",
                "solution": "contract scanner", "target_icp": "small-city finance directors",
                "icp_segments": ["finance directors", "city managers", "procurement officers"],
                "pricing": "$500/mo", "channels": [], "competitors": ["OpenGov"],
                "founder_ask": "Will they buy?", "launch_channel": "Product Hunt"}
    if kind == "archetypes":
        segs = ["finance directors", "city managers", "procurement officers"]
        tones = ["skeptical", "curious", "enthusiastic", "indifferent"]
        return {"archetypes": [{
            "id": f"a{j}", "segment": segs[j % 3], "name": f"Handle{j}", "persona": "A persona.",
            "tone": tones[j % 4], "objection_bias": ["price", "trust"],
            "action_likelihood": {"post": 0.15, "comment": 0.45, "upvote": 0.2, "ignore": 0.2},
            "weight": 1.0} for j in range(12)]}
    if kind == "reaction":
        return {"text": f"reaction {i}", "objections": ["price"] if i % 2 else ["trust"],
                "sentiment": 0.5 if i % 3 else -0.6}
    if kind == "ignore":
        return {"reason": "not for me", "category": "not_my_problem"}
    # report-stage synthesis calls
    return {"headline": "Price is the wall", "narrative": "Para one.\n\nPara two.",
            "messaging_gaps": ["lead with ROI"], "verdict": "sharpen_positioning",
            "verdict_reason": "price", "next_action": "talk to 5 CFOs", "confidence": "high",
            "confidence_reason": "small sample",
            "objections_enriched": [{"category": "price", "real_test": "Would you pay $500?",
                                     "kill_criteria": "3/5 say no", "suggested_fix": "pilot pricing"}],
            "questions": ["q"], "confusion": ["c"], "risks": ["r"], "themes": ["t"],
            "playbook": [{"trigger": "too pricey", "response": "ROI"}], "next_actions": ["n"]}


def run_to_completion(run_dir: Path, mode: str, agents: int = 16, seed: int = 7, drop: set[str] | None = None,
                      allow_missing_stage: str | None = None) -> dict:
    pitch = run_dir / "pitch.txt"
    pitch.write_text(VALIDATE_PITCH)
    assert bridge.main(["init", "--dir", str(run_dir), "--mode", mode, "--pitch-file", str(pitch),
                        "--agents", str(agents), "--seed", str(seed)]) == 0
    stages = []
    for _ in range(10):
        status = bridge._run_pipeline(run_dir, allow_missing=allow_missing_stage in stages[-1:])
        if status["status"] != "pending":
            return status | {"stages": stages}
        stages.append(status["stage"])
        tasks = json.loads(Path(status["pending_file"]).read_text())
        answers = [{"key": t["key"], "response": fake_answer(t, i)} for i, t in enumerate(tasks)
                   if not (drop and t["kind"] in drop)]
        f = run_dir / "answers_in.json"
        f.write_text(json.dumps(answers))
        bridge.main(["answer", "--dir", str(run_dir), "--file", str(f)])
    raise AssertionError("pipeline did not converge")


@pytest.mark.parametrize("mode", ["validate", "launch", "investor"])
def test_end_to_end_each_mode(tmp_path, mode):
    out = run_to_completion(tmp_path, mode)
    assert out["status"] == "done", out
    assert out["stages"] == ["parse", "archetypes", "reactions", "report"]
    rep = json.loads((tmp_path / "report.json").read_text())
    assert rep["meta"]["n_agents"] == 16 and len(rep["reactions"]) == 16
    assert rep["meta"]["failed_agent_calls"] == 0
    assert rep["headline"] == "Price is the wall"
    if mode == "launch":
        assert rep["launch_brief"]["playbook"][0]["trigger"] == "too pricey"
    html = (tmp_path / "report.html").read_text()
    assert "__REPORT_JSON__" not in html and "Swarm</title>" in html
    assert "No real people were interviewed" in (tmp_path / "report.md").read_text()


def test_confidence_is_clamped_by_swarmie(tmp_path):
    out = run_to_completion(tmp_path, "validate")
    rep = json.loads((tmp_path / "report.json").read_text())
    # fewer than 15 speakers -> Swarmie's ceiling is "low" even though the answer said "high"
    if rep["meta"]["speaking_agents"] < 15:
        assert rep["confidence"] == "low"
    assert out["status"] == "done"


def test_metrics_match_swarmie_deterministic_report(tmp_path):
    run_to_completion(tmp_path, "validate")
    rep = json.loads((tmp_path / "report.json").read_text())
    reactions = [AgentReaction(**r) for r in rep["reactions"]]
    det = deterministic_report(reactions)
    for k in ("sentiment_split", "action_split", "icp_fit", "silent_share_pct"):
        assert rep[k] == det[k], k


def test_same_seed_same_sampling(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(); b.mkdir()
    run_to_completion(a, "validate", seed=11)
    run_to_completion(b, "validate", seed=11)
    ra = [(r["archetype_id"], r["action"]) for r in json.loads((a / "report.json").read_text())["reactions"]]
    rb = [(r["archetype_id"], r["action"]) for r in json.loads((b / "report.json").read_text())["reactions"]]
    assert sorted(ra) == sorted(rb)


def test_agent_count_is_clamped(tmp_path):
    p = tmp_path / "p.txt"; p.write_text(VALIDATE_PITCH)
    bridge.main(["init", "--dir", str(tmp_path), "--pitch-file", str(p), "--agents", "5000"])
    assert json.loads((tmp_path / "input.json").read_text())["n_agents"] == bridge.MAX_AGENTS


def test_all_reactions_failing_is_an_error_not_an_empty_report(tmp_path):
    out = run_to_completion(tmp_path, "validate", drop={"reaction", "ignore"}, allow_missing_stage="reactions")
    assert out["status"] == "error" and "speaking agents failed" in out["error"]
    assert not (tmp_path / "report.json").exists()


def test_missing_synthesis_falls_back_and_is_flagged(tmp_path):
    out = run_to_completion(tmp_path, "validate", drop={"report"}, allow_missing_stage="report")
    assert out["status"] == "done"
    assert out["meta"]["synthesis_fallback_used"] is True


def test_answer_rejects_non_object_and_unknown_keys(tmp_path):
    p = tmp_path / "p.txt"; p.write_text(VALIDATE_PITCH)
    bridge.main(["init", "--dir", str(tmp_path), "--pitch-file", str(p)])
    st = bridge._run_pipeline(tmp_path, False)
    key = json.loads(Path(st["pending_file"]).read_text())[0]["key"]
    f = tmp_path / "in.json"
    f.write_text(json.dumps([{"key": key, "response": "not json"}, {"key": "bogus", "response": {}}]))
    bridge.main(["answer", "--dir", str(tmp_path), "--file", str(f)])
    assert json.loads((tmp_path / "answers.json").read_text()) == {}


def test_show_partitions_reaction_tasks_without_overlap(tmp_path, capsys):
    p = tmp_path / "p.txt"; p.write_text(VALIDATE_PITCH)
    bridge.main(["init", "--dir", str(tmp_path), "--pitch-file", str(p), "--agents", "24", "--seed", "3"])
    for _ in range(2):  # answer parse + archetypes
        st = bridge._run_pipeline(tmp_path, False)
        tasks = json.loads(Path(st["pending_file"]).read_text())
        f = tmp_path / "in.json"
        f.write_text(json.dumps([{"key": t["key"], "response": fake_answer(t, 0)} for t in tasks]))
        bridge.main(["answer", "--dir", str(tmp_path), "--file", str(f)])
    st = bridge._run_pipeline(tmp_path, False)
    assert st["stage"] == "reactions" and 1 <= st["batches"] <= bridge.MAX_BATCHES
    capsys.readouterr()
    seen = []
    for i in range(st["batches"]):
        bridge.main(["show", "--dir", str(tmp_path), "--batch", str(i), "--of", str(st["batches"])])
        out = json.loads(capsys.readouterr().out)
        seen += [t["key"] for t in out["tasks"]]
        assert set(t["system_id"] for t in out["tasks"]) <= set(out["system_prompts"])
    assert sorted(seen) == sorted(t["key"] for t in json.loads(Path(st["pending_file"]).read_text()))
