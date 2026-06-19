"""
agents/orchestrator.py — VyalaArchon Orchestrator
Single-step coordinator: creates the room, recruits agents,
kicks off the FIRST task only. Does not wait/loop — Band's
message routing handles the rest as agents reply to each other.
"""
import asyncio, logging
from dotenv import load_dotenv
from band import Agent
from band.config import load_agent_config
from agents.base_adapter import AimlAdapter

load_dotenv()
log = logging.getLogger("quantumshield.orchestrator")

SYSTEM_PROMPT = """
You are the VyalaArchon Orchestrator.

You do ONE thing per message you receive. You do NOT wait for replies,
loop, or chain multiple steps yourself — other agents will continue
the pipeline by replying with their own @mentions.

RULES:
- If this is the first message in the room (a repo URL or "scan" request):
  1. Call band_add_participant for: qs-assessment, qs-curator, qs-studyplan
     (use their handles exactly as given in the room participants list)
  2. Send ONE message mentioning @qs-assessment with: SCAN_REPO <repo_url>
  3. Stop. Do not call any other tools. Do not try to wait for a reply.
  4. CRITICAL RULE: Once you have triggered the scan, your job is done. Do not ask for a repo URL again. Do not mention the other agents. Just remain silent.

- If you are mentioned again later for any other reason, just acknowledge
  briefly and mention the human user back.

Never attempt to simulate or guess what other agents will say.
Never call more than one task-assignment tool per turn.
""".strip()


async def main():
    load_dotenv()
    agent_id, api_key = load_agent_config("qs_orchestrator")
    adapter = AimlAdapter(system_prompt=SYSTEM_PROMPT)
    agent = Agent.create(adapter=adapter, agent_id=agent_id, api_key=api_key)
    log.info("Orchestrator running — waiting in Band...")
    await agent.run()

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    asyncio.run(main())