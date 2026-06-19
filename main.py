"""
main.py
=======
VyalaArchon — Band of Agents Hackathon 2026
Launches all 4 agents simultaneously so they listen in Band together.

Usage:
  python main.py              # run all agents
  python main.py --agent orchestrator   # run single agent
"""

import argparse
import asyncio
import logging
import sys
from dotenv import load_dotenv

# Load .env FIRST before any agent module is imported
# Agents read AIML_API_KEY at module level so env must be set beforehand
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(name)-28s %(levelname)s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("quantumshield.main")

AGENTS = {
    "orchestrator": "agents.orchestrator",
    "assessment":   "agents.assessment",
    "curator":      "agents.curator",
    "study_plan":   "agents.study_plan",
}


async def run_all():
    """Launch all agents concurrently — each connects to Band via WebSocket."""
    import importlib

    tasks = []
    for name, module_path in AGENTS.items():
        try:
            mod = importlib.import_module(module_path)
            log.info(f"Starting {name} agent...")
            tasks.append(asyncio.create_task(mod.main(), name=name))
        except Exception as e:
            log.error(f"Failed to load {name}: {e}")

    if not tasks:
        log.error("No agents loaded. Check agent_config.yaml and .env")
        sys.exit(1)

    log.info(f"All {len(tasks)} agents running. Open Band and start a chat!")
    log.info("Tip: @qs-orchestrator scan https://github.com/org/repo")

    try:
        await asyncio.gather(*tasks)
    except KeyboardInterrupt:
        log.info("Shutting down...")
    except Exception as e:
        log.error(f"Agent error: {e}")
        raise


async def run_one(agent_name: str):
    """Run a single agent by name."""
    import importlib
    module_path = AGENTS.get(agent_name)
    if not module_path:
        log.error(f"Unknown agent: {agent_name}. Choose from: {list(AGENTS.keys())}")
        sys.exit(1)
    mod = importlib.import_module(module_path)
    await mod.main()


def main():
    parser = argparse.ArgumentParser(description="VyalaArchon Band Multi-Agent System")
    parser.add_argument(
        "--agent", "-a",
        choices=list(AGENTS.keys()),
        default=None,
        help="Run a single agent (default: run all)",
    )
    args = parser.parse_args()

    print("""
╔══════════════════════════════════════════════════════╗
║        VyalaArchon — PQC Assessment Platform       ║
║        Band of Agents Hackathon 2026                 ║
╠══════════════════════════════════════════════════════╣
║  Agents: Orchestrator → Assessment → Curator         ║
║          → Study Plan                                ║
║  LLM:    AI/ML API (claude-3-7-sonnet)               ║
║  NIST:   FIPS 203, 204, 205, 206 + CNSA 2.0         ║
╚══════════════════════════════════════════════════════╝
    """)

    if args.agent:
        asyncio.run(run_one(args.agent))
    else:
        asyncio.run(run_all())


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\nShutting down VyalaArchon agents... goodbye!")