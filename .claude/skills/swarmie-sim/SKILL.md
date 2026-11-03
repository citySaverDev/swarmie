---
name: swarmie-sim
description: Run a Swarmie startup-validation or launch-strategy swarm simulation inside Claude Code (no API key) and publish the report as a private artifact page. Use when the user asks to validate a startup idea, test a launch, run swarmie, or types /swarmie-sim.
---

# Swarmie simulation in Claude Code

This runs the real Swarmie pipeline from this repository (pitch parser → persona archetypes → swarm reactions →
report). Claude Code subagents act as its language model. No API key is used; runs count against the user's
Claude plan usage. Invoking this skill is the user's opt-in to the `swarmie-answer` multi-agent workflow:
3–9 subagents per run, depending on size.

## 0. Setup (once per session)

- `ROOT` = this repository's root (the directory that contains `claude_sim/bridge.py`).
- `BRIDGE` = `uv run --project 'ROOT/backend' python 'ROOT/claude_sim/bridge.py'` (absolute paths, single-quoted).
- If `ROOT/backend/.venv` doesn't exist, run `uv sync --project ROOT/backend` (Python 3.12 is pinned).
- `RUNS` = the session scratchpad directory, if one is listed, plus `/swarmie-runs`; otherwise `/tmp/swarmie-runs`.
  Run data contains the user's idea: never write it inside the repository and never commit it.

## 1. Collect the brief

Ask for anything missing, in one message. Take a pasted brief as-is if it already has the fields.

- **Mode**: `validate` (default), `launch`, or `investor`.
- **Fields** for each mode:
  - validate: PROBLEM, PRODUCT, AUDIENCE, PRICING, COMPETITORS
  - launch: PRODUCT, AUDIENCE, CHANNEL, DIFFERENTIATION, TIMING
  - investor: PROBLEM, SOLUTION, MARKET, TRACTION, TEAM, RAISE
- **Name**: a short product name (e.g. "CitySaver"). It is used in the page title. Before putting it on a command
  line, strip it to letters, digits, spaces, `.`, `_` and `-`, and single-quote it.
- **Personas**: default 24. The allowed range is 8–60. Swarmie caps confidence at "low" when fewer than 15 personas
  speak, so suggest 40 when the user wants a medium or high confidence verdict. Suggest 12 for a quick smoke test. Say that more personas
  means more subagent work, and that 24 means about 4 persona subagents.

Write the brief to `RUN/pitch.txt`, one `FIELD: value` line per field (long values are fine; max 20,000 characters).
`RUN` = `RUNS/<yyyymmdd-hhmm>-<name-slug>-<mode>`.

## 2. Initialise

`BRIDGE init --dir 'RUN' --mode MODE --pitch-file 'RUN/pitch.txt' --agents N --name 'NAME'`

Tell the user the mode, the persona count (note it if clamped) and the seed in one line.

## 3. Loop until done

Keep a round counter for each stage. A stage gets **at most 2 workflow rounds**. Trust `next`'s `n_tasks`, not what the
subagents report.

Repeat:

1. Run `BRIDGE next --dir 'RUN'`.
2. If `status` is `pending` (or `retry`, which means Swarmie rejected an unusable answer and re-requested it), and this
   stage has had fewer than 2 rounds, call the **Workflow** tool:
   - `name: "swarmie-answer"`
   - `args: {"bridge": "<BRIDGE>", "dir": "<RUN>", "stage": "<stage>", "batches": <batches>}`
   - Pass args as a JSON object, not a string.
   - Then go back to step 1 of this loop.
3. If the stage is still pending after 2 rounds:
   - For stage `reactions` or `report`: run `BRIDGE next --dir 'RUN' --allow-missing <stage>` once.
     - `reactions`: missing persona replies count as failed agents, exactly like a failed provider call in Swarmie.
     - `report`: a missing synthesis uses Swarmie's deterministic fallback.
     - The report records both.
     - The flag only covers the named stage, so later stages still go to subagents.
     - If the call returns `pending` for the next stage, continue the loop.
   - For stage `parse` or `archetypes`: stop and tell the user which step failed. Re-running the skill on the same
     run directory resumes from it.
4. If `status` is `error`, stop and relay the `error` text verbatim. It is written to be actionable.
5. If `status` is `done`, go to step 4.

Stages run in this order:
- parse: 1 task
- archetypes: 1 task
- reactions: one task per speaking persona plus sampled silent ones, spread over up to 6 subagents
- report: 1 task, or 2 in launch mode

Never answer pending tasks yourself in the main conversation, and never edit `answers.json` by hand.

**Cancelling:** if the user wants to stop, stop the running workflow (TaskStop on its task id) and say the run was
cancelled. The run directory is kept, and re-running `next` later resumes from the answers already saved.

## 4. Publish

- Publish `RUN/report.html` with the Artifact tool:
  - `icon: "chart"`
  - description: one sentence naming the product and mode.
  - The page is already a finished, theme-aware, responsive page that follows the artifact page contract, so publish
    the file as generated without editing it.
- Send `RUN/report.md` and `RUN/report.json` to the user with SendUserFile (`display: "attach"`).
- Reply in a few lines:
  - the verdict and confidence, and the headline
  - the next action
  - the top 2 objections
  - the artifact link
  - personas run, failed persona calls, and whether the synthesis fallback was used
  - this exact caveat: the personas are AI-simulated, nobody real was interviewed, and the PMF number is an
    uncalibrated heuristic.
