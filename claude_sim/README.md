# Swarmie in Claude Code

Run Swarmie's startup-validation and launch-strategy simulations from a Claude Code session, on desktop or iPad,
without an LLM API key and without hosting anything.

## How it works

The pipeline is the same as the web app. This repo's own code (`backend/app/services/swarm/`) parses the pitch,
generates persona archetypes, rolls each persona's action (post / comment / upvote / scroll past), builds every
prompt, computes the metrics and writes the report.

The only thing swapped is the language model:

- **Web app:** each prompt goes to an OpenAI-compatible API.
- **Here:** `claude_sim/bridge.py` captures the prompts, and the `swarmie-answer` workflow has Claude Code subagents
  answer them. Persona replies are spread over up to six subagents.

```
/swarmie-sim ──► bridge.py init
                 bridge.py next ──► pending tasks (exact Swarmie prompts)
                      ▲                   │
                      │      Workflow "swarmie-answer": N subagents
                      │      show batch → write answers → bridge.py answer
                      └───────────────────┘
                 bridge.py next ──► report.json / report.md / report.html ──► private artifact page
```

The pieces:

- `claude_sim/bridge.py`: the capture/replay stub plus a command-line tool (`init`, `next`, `show`, `answer`, `render`).
  It never calls a network LLM.
- `claude_sim/report_template.html`: the report page, a single self-contained file that works on phone, tablet and
  desktop in light or dark mode.
- `.claude/workflows/swarmie-answer.js`: the subagent fan-out, with one bounded retry per batch.
- `.claude/skills/swarmie-sim/SKILL.md`: the `/swarmie-sim` entry point.
- `claude_sim/tests/`: plumbing tests. These use canned answers; no model is involved.

## Running a simulation

1. Open the Claude app (iPad or desktop) or claude.ai/code. Start a **Code** session on this repository.
2. Type `/swarmie-sim`. Paste your brief, or answer the prompts:
   - **Validate:** PROBLEM / PRODUCT / AUDIENCE / PRICING / COMPETITORS
   - **Launch:** PRODUCT / AUDIENCE / CHANNEL / DIFFERENTIATION / TIMING
   - **Investor:** PROBLEM / SOLUTION / MARKET / TRACTION / TEAM / RAISE
3. Wait a few minutes. You get:
   - a link to a **private artifact page** with the verdict, sentiment, objections, segment differences, why people
     scrolled past, the launch brief, recommended real-world tests, and every persona's reaction
   - `report.md` and `report.json` as files
4. Open the link in Safari on the iPad. The page has **Copy Markdown** and **Copy JSON** buttons. Artifact pages
   can't start downloads, so use the attached files when you want a file.

**History:** every report stays in your artifact gallery (claude.ai → Artifacts), which is private by default. Run
data is written to the session scratchpad and is **never committed**. This fork is public under the AGPL, so your
ideas stay out of git.

**Stopping:** say "stop" or "cancel". The running workflow is stopped. Re-running resumes from the answers already
saved in the same session. Each stage gets at most 2 subagent rounds; after that, missing persona replies are counted
as failed agents (shown in the report) instead of being retried again.

**From a terminal:** on a desktop with Claude Code installed, clone this repo, run `uv sync --project backend`, open
`claude` in the repo, and type `/swarmie-sim`.

## Cost and size

- **Cost:** runs use your Claude plan's usage. There are no per-token API charges, and the dollar cost can't be
  measured.
- **Size:** 8–60 personas, default 24.
- **Subagents per run:**
  - 1 parse
  - 1 archetypes
  - about 1 persona subagent per 6 persona replies, at most 6
  - 1–2 synthesis
  - plus at most one retry per failed batch
- **Fixed upper limit:** there is no automatic restart loop, so cost can't run away.

## Limitations

- **Simulated people:** these are AI personas, and **no real people are interviewed**. Use the output to decide what
  to ask real customers, not as evidence of demand.
- **Smaller swarms:** tens of personas instead of the web app's 100–500. Personas in one batch are answered by the
  same subagent. Each is prompted as an independent call, but they are less independent than separate API calls.
- **Low confidence below 15 speakers:** Swarmie caps confidence at "low" when fewer than 15 personas speak. With 24
  personas, about 12–15 usually speak, so use 40 or more when you need a medium or high confidence verdict. The
  archetype count (12) and every prompt match the web app; only the persona count is smaller (the web app defaults
  to 100).
- **Pitch text is untrusted input:** the persona subagents can run tools, and your pitch text is part of their
  prompt. They are told to treat it as data only. Don't paste text from untrusted sources into a brief.
- **Uncalibrated scores:** the PMF score and the PMF index are Swarmie's uncalibrated heuristics.
- **Speed:** a run takes several minutes, not the hosted site's advertised 60 seconds.
- **No live dashboard:** progress shows in Claude Code's workflow view instead of the web UI.

## License

AGPL-3.0, like upstream Swarmie (hp-8/swarmie). This directory and `.claude/` are additive modifications.
