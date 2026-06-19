# VyalaArchon

**A multi-agent post-quantum cryptography (PQC) readiness platform, built on [Band](https://band.ai) for the Band of Agents Hackathon 2026.**

> By 2030, RSA, ECDSA, and other widely-deployed cryptographic algorithms are expected to be breakable by fault-tolerant quantum computers. NIST has already published the replacement standards (FIPS 203, 204, 205). VyalaArchon automates the first, hardest step of migration: finding out where you're vulnerable, and what to do about it.

---

## What it does

VyalaArchon scans a GitHub repository for quantum-vulnerable cryptographic algorithms, maps each finding to its NIST-approved replacement, and builds a grounded learning path — all coordinated through a team of specialized agents that talk to each other entirely inside a shared Band chat room.

This isn't a single LLM call wrapped in a CLI. It's agents with distinct responsibilities, handing work off to one another the same way a human team would: by posting messages, tagging the next person, and reading what came before. A web frontend is also included, letting anyone paste a repo URL into a browser and watch the same agent pipeline run live, streamed from the same Band room.

## Architecture

```
User (Band chat OR web UI)
  │
  │ "scan <repo_url>"
  ▼
┌────────────────────┐
│  Orchestrator Agent │  Classifies the request, recruits the team into
│                     │  the room, kicks off the first task. Does not
└──────────┬──────────┘  wait or loop — Band's mention routing does that.
           │ @qs-assessment SCAN_REPO <url>
           ▼
┌────────────────────┐
│  Assessment Agent   │  Fetches repo source via GitHub API, scans with
│                     │  AST parsers (Python/JS) + regex fallback, scores
└──────────┬──────────┘  quantum risk, maps each finding to its NIST FIPS
           │              replacement.
           │ @qs-curator BUILD_LEARNING_PATH
           ▼
┌────────────────────┐
│  Curator Agent      │  Builds a NIST-grounded learning module per
│                     │  vulnerable algorithm — theory, key concepts,
└──────────┬──────────┘  implementation steps, FIPS citations.
           │ @qs-studyplan GENERATE_STUDY_PLAN
           ▼
┌────────────────────┐
│  Study Plan Agent   │  Generates a role-aware weekly study schedule
│                     │  (work in progress — see Known limitations).
└─────────────────────
```

Every arrow above is a real message posted in a Band chat room — not a Python function call, not a shared database write. **Band is the actual collaboration layer between agents**, which was the core requirement of this hackathon's challenge.

### Why this satisfies "real" multi-agent collaboration

- Each agent is a **separate process** with its own Band identity (UUID + API key), registered independently on band.ai.
- Agents **never call each other directly**. The only way information moves between them is by posting a message in the shared room and `@mentioning` the next agent.
- The Orchestrator does not orchestrate by waiting and looping — it fires the first task and stops. The rest of the pipeline continues purely through Band's mention-based message routing, the same way a Slack thread would route a request between teammates.
- A custom `AimlAdapter` (`agents/base_adapter.py`) extends Band's `SimpleAdapter` so every agent can reason with an external LLM provider (AI/ML API) while still using all of Band's native platform tools (`send_message`, `add_participant`, `create_chatroom`, `execute_tool_call`, etc.) side-by-side with our own custom domain tools (`scan_repository`, `build_learning_path`, `generate_study_plan`).

## How Band is used

Band is not a notification system bolted onto the end of this project — it is the messaging substrate the whole pipeline runs on.

- **Agent identity**: every agent (`qs-orchestrator`, `qs-assessment`, `qs-curator`, `qs-studyplan`) is registered as an External Agent on band.ai, each with its own UUID and API key.
- **Connection**: each agent process holds an open WebSocket connection to Band via the `band-sdk` (`AgentRuntime` / `Agent.create()`), listening for messages in any room it's been added to.
- **Coordination**: when one agent finishes its work, it posts a message into the shared room and `@mentions` the next agent by handle. Band delivers that message to the mentioned agent's `on_message` handler — there is no other channel for handing off state between agents.
- **Platform tools**: agents use Band's built-in tools (exposed via `tools.get_openai_tool_schemas()` and `tools.execute_tool_call()`) for things like adding participants and creating rooms, fully integrated alongside our domain-specific tools in the same tool-calling loop.
- **Mention constraints**: Band requires every message to carry at least one valid mention, and rejects self-mentions outright. `base_adapter.py`'s `_get_mentions()` / `_send_with_fallback()` handle this defensively — preferring a human participant, falling back to any other agent in the room, and retrying through every available handle if the first attempt is rejected for `cannot_mention_self`.
- **Web bridge**: `web/backend/server.py` uses Band's official `thenvoi_rest.AsyncRestClient` (bundled with `band-sdk`) to programmatically create a room, add all four agents as participants, and post the kickoff message — entirely via REST, with no SDK polling loop of its own. It then streams the room's messages back to the browser over WebSocket so the same agent pipeline is visible outside Band's own chat UI.

## How AI/ML API is used

Every agent's reasoning is powered by [AI/ML API](https://aimlapi.com) — a single, OpenAI-compatible endpoint that gives access to a wide range of frontier and open models without juggling separate SDKs or billing accounts per provider.

- `agents/base_adapter.py` constructs one shared `AsyncOpenAI` client pointed at `https://api.aimlapi.com/v1` (or OpenRouter, depending on `.env` configuration — see below), used by all four agents.
- The model is configurable via the `AIML_MODEL` environment variable. This project was built and tested primarily with `nvidia/nemotron-3-ultra-550b-a55b` — a frontier reasoning model (550B total / 55B active parameters, hybrid Transformer-Mamba MoE, up to 1M token context) chosen specifically because it's designed for long-running agentic workflows and multi-step tool orchestration, which is exactly what a 4-agent pipeline like this needs.
- Each agent's tool-calling loop (`_handle_message` in `base_adapter.py`) is a standard OpenAI-style function-calling loop: the model is offered both Band's platform tools and the agent's own domain tool (e.g. `scan_repository`), decides which to call, and the adapter executes whichever it picks — Band tools via `tools.execute_tool_call()`, domain tools via direct Python invocation — before looping back with the result until the model produces a final text reply.
- `_strip_reasoning()` defensively strips chain-of-thought leakage that some reasoning models emit directly into `message.content` instead of a separate reasoning channel, so the Band room only ever sees the agent's intended final output.

> **Note on provider flexibility**: because the adapter only depends on the OpenAI-compatible `chat.completions` interface, swapping providers is a one-line change — this project has also been run against OpenRouter's free-tier models (e.g. `deepseek/deepseek-r1-0528:free`) during development without touching any agent logic.

## Project structure

```
Band-agents-hack/
├── agents/
│   ├── base_adapter.py     # Shared adapter — routes Band messages through AI/ML API
│   ├── orchestrator.py     # Recruits the team, kicks off the first task
│   ├── assessment.py       # Repo scanner + NIST replacement mapping
│   ├── curator.py          # NIST-grounded learning path builder
│   └── study_plan.py       # Role-based weekly study schedule (in progress)
├── engine/
│   ├── scanner.py          # AST + regex dispatch hub
│   ├── github_fetcher.py   # GitHub REST API repo fetcher
│   ├── scoring.py          # Quantum risk scoring model
│   └── parsers/            # Python / JavaScript AST parsers
├── models/
│   └── findings.py         # CryptoFinding, ScoredFinding, Severity dataclasses
├── data/
│   ├── work_signals.json   # Engineer role data (for study plan personalization)
│   └── team_data.json      # Team seed data
├── web/
│   ├── README.md           # Web UI setup instructions
│   ├── backend/
│   │   └── server.py       # FastAPI bridge — Band REST + WebSocket streaming
│   └── frontend/
│       └── src/            # React app — repo input + live agent feed
├── main.py                 # Entry point — launches all agents concurrently
├── config_bootstrap.py     # Builds agent_config.yaml from env vars at startup
├── agent_config.yaml       # Band agent_id + api_key per agent (gitignored)
├── .env                    # AI/ML API key + GitHub token (gitignored)
├── railway.toml            # Railway deploy config for the agents service
└── requirements.txt
```

## Setup

### 1. Register agents on Band

Go to [band.ai](https://band.ai) → **Agents** → **New Agent** → **External Agent**, and create one entry for each:

| Agent | Suggested name |
|---|---|
| Orchestrator | `qs-orchestrator` |
| Assessment | `qs-assessment` |
| Curator | `qs-curator` |
| Study Plan | `qs-studyplan` |

Copy the **API key** (shown once) and **Agent UUID** for each.

### 2. Configure credentials

**Local development** — create `agent_config.yaml` in the project root:

```yaml
qs_orchestrator:
  agent_id: "<uuid>"
  api_key:  "<key>"

qs_assessment:
  agent_id: "<uuid>"
  api_key:  "<key>"

qs_curator:
  agent_id: "<uuid>"
  api_key:  "<key>"

qs_studyplan:
  agent_id: "<uuid>"
  api_key:  "<key>"
```

**Production (e.g. Railway)** — set environment variables instead; `config_bootstrap.py` builds `agent_config.yaml` from these automatically at startup:

```
BAND_ORCHESTRATOR_AGENT_ID, BAND_ORCHESTRATOR_API_KEY
BAND_ASSESSMENT_AGENT_ID,   BAND_ASSESSMENT_API_KEY
BAND_CURATOR_AGENT_ID,      BAND_CURATOR_API_KEY
BAND_STUDYPLAN_AGENT_ID,    BAND_STUDYPLAN_API_KEY
```

Create `.env`:

```dotenv
AIML_API_KEY=your_aiml_api_key
AIML_MODEL=nvidia/nemotron-3-ultra-550b-a55b
GITHUB_TOKEN=your_github_token   # optional, avoids GitHub rate limits
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Run all agents

```bash
python main.py
```

This launches all four agents concurrently, each holding an open WebSocket connection to Band, listening for mentions.

### 5. Try it — directly in Band

In a Band chat room with all four agents added as participants:

```
@qs-orchestrator scan https://github.com/stripe/stripe-python
```

Watch the room — the Orchestrator recruits the team and kicks off the scan, the Assessment Agent reports findings with NIST replacements, and the Curator Agent builds the learning path, all without any further input from you.

### 6. Try it — via the web UI



## Known limitations

- The Study Plan agent is still being stabilized — under load, some LLM responses produce malformed tool arguments or trigger Band's mention-resolution edge cases (`cannot_mention_self` when an agent finds itself alone in a room). `base_adapter.py` now handles these defensively, but the full 4-agent chain hasn't been confirmed end-to-end as reliably as the 3-agent (Orchestrator → Assessment → Curator) chain.
- The AST scanner currently supports Python and JavaScript/TypeScript natively; other languages fall back to a regex-based scan, which is less precise.
- NIST FIPS mappings are currently encoded as a curated lookup table rather than retrieved from the source PDFs via RAG — the mappings are accurate, but not yet dynamically grounded in the documents themselves.
- The web backend polls Band's REST API every 2 seconds to stream messages to the frontend rather than using a persistent Band WebSocket connection, since it only needs to read room messages, not act as an agent itself. Each scan also creates a brand-new Band room with no automatic cleanup.

## Hackathon track

Submitted under **Track 3 — Regulated & High-Stakes Workflows**. Post-quantum migration is a NIST-mandated deadline with real compliance consequences, and the multi-agent pipeline mirrors how a security team would actually triage and delegate this work across specialists.

## License

Built for the Band of Agents Hackathon, June 2026.