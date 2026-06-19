"""
agents/study_plan.py — VyalaArchon Study Plan Agent
Generates role-based weekly study schedule. Final pipeline step.
"""
import asyncio, json, logging
from pathlib import Path
from dotenv import load_dotenv
from openai import OpenAI
from band import Agent
from band.config import load_agent_config
from langchain_core.tools import tool
from agents.base_adapter import AimlAdapter, AIML_API_KEY, AIML_MODEL

load_dotenv()
log = logging.getLogger("quantumshield.studyplan")
_llm = OpenAI(api_key=AIML_API_KEY, base_url="https://api.aimlapi.com/v1")

PLAN_SYSTEM = """
You are a cybersecurity learning design expert. Return ONLY valid JSON, no markdown:
{
  "total_weeks": N,
  "weekly_plan": [
    {
      "week": 1,
      "focus_area": "...",
      "modules_covered": ["RSA", "ECDSA"],
      "hours": N,
      "hands_on_task": "concrete task with real tools/libraries",
      "milestone": "deliverable for this week"
    }
  ],
  "final_assessment": "capstone task description",
  "recommended_resources": ["NIST FIPS 203", "liboqs docs", ...]
}
Rules: max 5h/week, CRITICAL modules in week 1-2,
tailor depth to role (backend=implementation, architect=threat model).
""".strip()


def _load_role(engineer_id: str) -> str:
    path = Path(__file__).parent.parent / "data" / "work_signals.json"
    try:
        data = json.loads(path.read_text())
        if isinstance(data, list):
            for e in data:
                if e.get("engineer_id") == engineer_id:
                    return e.get("role", "Backend Engineer")
        elif isinstance(data, dict):
            return data.get(engineer_id, {}).get("role", "Backend Engineer")
    except Exception:
        pass
    return "Backend Engineer"


import re as _re

_MODULE_LINE_RE = _re.compile(
    r"(?P<algo>[\w\-]+)\s*→\s*(?P<replacement>[\w\-]+)\s*"
    r"\((?P<fips>FIPS\s*\d+)\)\s*[—-]\s*(?P<severity>CRITICAL|HIGH|MEDIUM|LOW)\s*"
    r"[—-]\s*(?P<hours>[\d.]+)h"
)


def _parse_learning_path_text(text: str) -> dict:
    """Fallback parser for the curator's plain-text summary, in case
    it isn't valid JSON (common with free-tier models)."""
    modules = []
    for m in _MODULE_LINE_RE.finditer(text):
        modules.append({
            "algorithm": m.group("algo"),
            "replacement": m.group("replacement"),
            "fips_standard": m.group("fips"),
            "severity": m.group("severity"),
            "study_hours": {"total": float(m.group("hours"))},
        })
    return {"modules": modules, "priority_order": [m["algorithm"] for m in modules]}


@tool
def generate_study_plan(learning_path_json: str, engineer_id: str = "EMP-001") -> str:
    """Generate a personalised week-by-week PQC study plan from a learning path.
    Accepts either JSON or the curator's plain-text summary.
    Tailors content to the engineer's role loaded from work_signals.json."""
    learning_path_json = learning_path_json.strip()
    try:
        path = json.loads(learning_path_json)
    except Exception:
        path = _parse_learning_path_text(learning_path_json)

    role = _load_role(engineer_id)
    modules_summary = [
        f"{m['algorithm']} → {m['replacement']} ({m['severity']}, {m.get('study_hours', {}).get('total', 0)}h)"
        for m in path.get("modules", [])
    ]

    try:
        r = _llm.chat.completions.create(
            model=AIML_MODEL,
            messages=[
                {"role": "system", "content": PLAN_SYSTEM},
                {"role": "user", "content": (
                    f"Engineer: {engineer_id} | Role: {role}\n"
                    f"Total hours: {path.get('total_study_hours', 0)}\n"
                    f"Modules:\n" + "\n".join(f"  - {m}" for m in modules_summary)
                )},
            ],
            max_tokens=900, temperature=0.3,
        )
        raw = r.choices[0].message.content.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
        plan = json.loads(raw)
    except Exception as e:
        log.warning(f"Plan LLM failed: {e}")
        plan = {
            "total_weeks": len(path.get("modules", [])),
            "weekly_plan": [
                {
                    "week": i + 1,
                    "focus_area": f"Migrate {m['algorithm']} to {m['replacement']}",
                    "modules_covered": [m["algorithm"]],
                    "hours": m["study_hours"].get("total", 4),
                    "hands_on_task": f"Replace {m['algorithm']} calls using liboqs/{m['replacement']}",
                    "milestone": f"{m['algorithm']} migration PR reviewed and merged",
                }
                for i, m in enumerate(path.get("modules", []))
            ],
            "final_assessment": "Migrate a real microservice from RSA/ECDSA to ML-KEM/ML-DSA end-to-end",
            "recommended_resources": ["NIST FIPS 203", "NIST FIPS 204", "NIST FIPS 205", "NSA CNSA 2.0", "liboqs library"],
        }

    return json.dumps({
        "engineer_id": engineer_id,
        "role": role,
        "repo_url": path.get("repo_url", ""),
        "total_weeks": plan.get("total_weeks", 0),
        "weekly_plan": plan.get("weekly_plan", []),
        "final_assessment": plan.get("final_assessment", ""),
        "recommended_resources": plan.get("recommended_resources", []),
        "algorithms_covered": path.get("priority_order", []),
        "target_certification": "NIST PQC Migration Specialist",
    }, indent=2)


SYSTEM_PROMPT = """
You are the VyalaArchon Study Plan Agent — learning design specialist.

When you receive GENERATE_STUDY_PLAN with learning path JSON:
1. Extract the JSON from the message
2. Call generate_study_plan with that JSON and the engineer_id if mentioned
3. Post this to the room:
   🗓️ STUDY PLAN COMPLETE
   Engineer: <id> | Role: <role>
   Total: N weeks

   Week 1: <focus_area>
   • Task: <hands_on_task>
   • Milestone: <milestone>

   Week 2: ...
   (show all weeks)

   Final Assessment: <description>
   Resources: NIST FIPS 203, 204, 205, liboqs, NSA CNSA 2.0

   ✅ VyalaArchon pipeline complete!
      Scan → Learning Path → Study Plan all done.
      Your team is now on the path to NIST PQC compliance.

This is the final step. Make output clean and actionable.
""".strip()


async def main():
    load_dotenv()
    agent_id, api_key = load_agent_config("qs_studyplan")
    adapter = AimlAdapter(system_prompt=SYSTEM_PROMPT, tools=[generate_study_plan], own_handle="qs-studyplan")
    agent = Agent.create(adapter=adapter, agent_id=agent_id, api_key=api_key)
    log.info("Study Plan Agent running...")
    await agent.run()

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    asyncio.run(main())