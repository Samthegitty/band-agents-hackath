"""
agents/curator.py — VyalaArchon Curator Agent
Builds NIST-grounded learning path from scan findings.
"""
import asyncio, json, logging
from dotenv import load_dotenv
from openai import OpenAI
from band import Agent
from band.config import load_agent_config
from langchain_core.tools import tool
from agents.base_adapter import AimlAdapter, AIML_API_KEY, AIML_MODEL

load_dotenv()
log = logging.getLogger("quantumshield.curator")
_llm = OpenAI(api_key=AIML_API_KEY, base_url="https://api.aimlapi.com/v1")

STUDY_HOURS = {
    "RSA": {"theory": 3, "implementation": 5, "total": 8},
    "ECDSA": {"theory": 3, "implementation": 4, "total": 7},
    "ECDH": {"theory": 4, "implementation": 6, "total": 10},
    "AES-128": {"theory": 1, "implementation": 1, "total": 2},
    "MD5": {"theory": 0.5, "implementation": 0.5, "total": 1},
    "SHA1": {"theory": 0.5, "implementation": 0.5, "total": 1},
    "JWT": {"theory": 2, "implementation": 3, "total": 5},
}

MODULE_SYSTEM = """
You are a PQC educator. Return ONLY valid JSON, no markdown fences:
{
  "theory_summary": "Why this algorithm fails against quantum computers. Cite Shor's or Grover's algorithm specifically.",
  "key_concepts": ["4-5 concepts the engineer must understand"],
  "implementation_steps": ["4-5 concrete steps with real library names e.g. liboqs, pqcrypto"],
  "citations": ["FIPS 203 §4.2", "RFC 9629", ...],
  "prerequisite_knowledge": ["2-3 prerequisites"]
}
""".strip()


import re as _re

_FINDING_LINE_RE = _re.compile(
    r"\[(?P<severity>CRITICAL|HIGH|MEDIUM|LOW)\]\s+"
    r"(?P<file>\S+):(?P<line>\d+)\s+"
    r"(?P<algo>[\w\-]+)\s*→\s*"
    r"(?P<replacement>[\w\-]+)\s*\((?P<fips>FIPS\s*\d+)\)"
)
_REPO_RE = _re.compile(r"SCAN COMPLETE\s*[—-]\s*(?P<repo>\S+)")


def _parse_scan_text(text: str) -> dict:
    """
    Parse the assessment agent's compact_summary text directly —
    avoids relying on the LLM to faithfully relay raw JSON, which
    free-tier models tend to truncate or mangle.
    """
    findings = []
    for m in _FINDING_LINE_RE.finditer(text):
        findings.append({
            "file": m.group("file"),
            "line": int(m.group("line")),
            "algorithm": m.group("algo"),
            "severity": m.group("severity"),
            "pqc_replacement": m.group("replacement"),
            "fips_standard": m.group("fips"),
        })
    repo_match = _REPO_RE.search(text)
    return {
        "repo_url": repo_match.group("repo") if repo_match else "",
        "findings": findings,
    }


@tool
def build_learning_path(scan_results_json: str) -> str:
    """Build a NIST-grounded learning path from PQC scan findings.
    Accepts either the raw compact_summary text from the assessment agent
    OR a JSON string of scan results. Returns a complete learning path
    with one module per vulnerable algorithm."""
    scan_results_json = scan_results_json.strip()

    # Try JSON first; fall back to parsing the plain-text summary
    try:
        scan = json.loads(scan_results_json)
    except Exception:
        scan = _parse_scan_text(scan_results_json)

    findings = scan.get("findings", [])
    if not findings:
        return json.dumps({"modules": [], "total_study_hours": 0,
                            "error": "No findings could be parsed from input"})

    # Deduplicate by base algorithm
    seen, unique = set(), []
    for f in findings:
        base = f["algorithm"].split("-")[0]
        if base not in seen:
            seen.add(base)
            unique.append(f)

    modules = []
    for f in unique:
        algo, replace, fips, severity = (
            f["algorithm"], f.get("pqc_replacement", "ML-DSA-44"),
            f.get("fips_standard", "FIPS 204"), f.get("severity", "HIGH"),
        )
        base = algo.split("-")[0]
        try:
            r = _llm.chat.completions.create(
                model=AIML_MODEL,
                messages=[
                    {"role": "system", "content": MODULE_SYSTEM},
                    {"role": "user", "content": f"FROM: {algo}\nTO: {replace} ({fips})\nSeverity: {severity}"},
                ],
                max_tokens=500, temperature=0.3,
            )
            raw = r.choices[0].message.content.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
            data = json.loads(raw)
        except Exception as e:
            log.warning(f"Module gen failed for {algo}: {e}")
            data = {
                "theory_summary": f"{algo} is vulnerable to quantum attacks.",
                "key_concepts": [f"Understanding {algo} vulnerability", f"Learning {replace}"],
                "implementation_steps": [f"Replace {algo} with {replace}", f"Consult {fips}"],
                "citations": [fips],
                "prerequisite_knowledge": ["Basic cryptography"],
            }

        hours = STUDY_HOURS.get(base, {"theory": 2, "implementation": 3, "total": 5})
        cites = data.get("citations", [])
        modules.append({
            "algorithm": algo, "replacement": replace,
            "fips_standard": fips, "severity": severity,
            "theory_summary": data.get("theory_summary", ""),
            "key_concepts": data.get("key_concepts", []),
            "implementation_steps": data.get("implementation_steps", []),
            "citations": cites,
            "prerequisite_knowledge": data.get("prerequisite_knowledge", []),
            "study_hours": hours,
            "iq_confidence": round(min(0.5 + 0.15 * len(cites), 0.95), 2),
        })

    order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}
    modules.sort(key=lambda m: order.get(m["severity"], 4))
    total_hours = sum(m["study_hours"].get("total", 0) for m in modules)

    return json.dumps({
        "repo_url": scan.get("repo_url", ""),
        "total_study_hours": total_hours,
        "estimated_completion_weeks": max(1, int(total_hours / 5)),
        "target_certification": "NIST PQC Migration Specialist",
        "priority_order": [m["algorithm"] for m in modules],
        "modules": modules,
    }, indent=2)


SYSTEM_PROMPT = """
You are the VyalaArchon Learning Path Curator — PQC education specialist.

When you receive a message containing BUILD_LEARNING_PATH:
1. Pass the ENTIRE message text (including the findings list with lines
   like "[CRITICAL] file:line ALGO → REPLACEMENT (FIPS NNN)") to the
   build_learning_path tool as the scan_results_json argument. You do
   NOT need to convert it to JSON yourself — the tool parses plain text.
2. Call the tool ONCE only.
3. Post ONLY this to the room — no extra commentary:

   📚 LEARNING PATH COMPLETE
   • Total study hours: <from tool result>
   • Estimated completion: <weeks> weeks
   • Study order (severity-ranked):
     1. <algo> → <replacement> (<fips>) — <severity> — <hours>h
        Key concepts: <short list>
     2. ...

   @banjarapadam62/qs-studyplan GENERATE_STUDY_PLAN

CRITICAL RULE: When you receive scan findings, you MUST use the `build_learning_path` tool. 
Pass the ENTIRE raw text of the findings into the tool. 
DO NOT output Python lists, DO NOT output JSON arrays in your text response. 
If you don't have the scan findings yet, output exactly: "Waiting for scan results." and DO NOT mention any other agents.
Do NOT paste raw JSON into your message. Do NOT call the tool more than once.
All recommendations must cite NIST FIPS 203/204/205 sections.
""".strip()


async def main():
    load_dotenv()
    agent_id, api_key = load_agent_config("qs_curator")
    adapter = AimlAdapter(system_prompt=SYSTEM_PROMPT, tools=[build_learning_path])
    agent = Agent.create(adapter=adapter, agent_id=agent_id, api_key=api_key)
    log.info("Curator Agent running...")
    await agent.run()

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    asyncio.run(main())