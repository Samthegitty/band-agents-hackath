"""
agents/assessment.py — VyalaArchon Assessment Agent
Scans GitHub repos for quantum-vulnerable cryptography.
"""
import asyncio, json, logging, os
from dotenv import load_dotenv
from openai import OpenAI
from band import Agent
from band.config import load_agent_config
from langchain_core.tools import tool
from agents.base_adapter import AimlAdapter, AIML_API_KEY, AIML_MODEL

load_dotenv()
log = logging.getLogger("quantumshield.assessment")

_llm = OpenAI(api_key=AIML_API_KEY, base_url="https://api.aimlapi.com/v1")

PQC_MAP = {
    "RSA":     ("ML-KEM-768",  "FIPS 203", "Shor's algorithm breaks RSA factoring"),
    "ECDSA":   ("ML-DSA-44",   "FIPS 204", "Shor's algorithm breaks ECDLP"),
    "ECDH":    ("ML-KEM-768",  "FIPS 203", "Shor's algorithm breaks ECDH key exchange"),
    "AES-128": ("AES-256",     "FIPS 197", "Grover's algorithm halves effective key length to 64-bit"),
    "MD5":     ("SHA3-256",    "FIPS 202", "Classically broken + quantum collision speedup"),
    "SHA1":    ("SHA3-256",    "FIPS 202", "Classically broken + quantum collision speedup"),
    "JWT":     ("ML-DSA-44",   "FIPS 204", "Underlying ECDSA/RSA signing is quantum-vulnerable"),
}
QASS = {
    "RSA":     {"logical_qubits": 4096, "breakable_by_2030": True, "urgency": "CRITICAL", "time_to_break": "~8h on 2030 FTQC"},
    "ECDSA":   {"logical_qubits": 2048, "breakable_by_2030": True, "urgency": "CRITICAL", "time_to_break": "~1h on 2030 FTQC"},
    "ECDH":    {"logical_qubits": 2048, "breakable_by_2030": True, "urgency": "CRITICAL", "time_to_break": "~1h on 2030 FTQC"},
    "AES-128": {"logical_qubits": 256,  "breakable_by_2030": True, "urgency": "HIGH",     "time_to_break": "Grover: 2^64 ops"},
    "MD5":     {"logical_qubits": 128,  "breakable_by_2030": True, "urgency": "HIGH",     "time_to_break": "Classically broken"},
    "SHA1":    {"logical_qubits": 160,  "breakable_by_2030": True, "urgency": "HIGH",     "time_to_break": "Classically broken"},
    "JWT":     {"logical_qubits": 2048, "breakable_by_2030": True, "urgency": "CRITICAL", "time_to_break": "Via underlying ECDSA/RSA"},
}


@tool
def scan_repository(repo_url: str) -> str:
    """Scan a GitHub repository for post-quantum cryptography vulnerabilities.
    Returns a compact JSON report with top findings, severity scores, and NIST replacements."""
    try:
        from engine.github_fetcher import get_repo_files
        from engine.scanner import scan_file_content
        from engine.scoring import score_finding
    except ImportError as e:
        return json.dumps({"error": f"Engine not available: {e}", "findings": []})

    files = get_repo_files(repo_url)
    if not files:
        return json.dumps({"error": "Could not fetch repo — check URL and GITHUB_TOKEN", "findings": []})

    # Collect all findings — only count algorithms we actually recognize.
    # The AST scanner sometimes tags generic terms (Hash, TLS, ECC,
    # PyCA/cryptography) as "algorithms" which inflates findings with
    # false positives. We only score things present in PQC_MAP.
    all_findings = []
    for filepath, content in files.items():
        for rf in scan_file_content(filepath, content):
            base = rf.algorithm.split("-")[0]
            if base not in PQC_MAP:
                continue   # skip unrecognized / generic tags

            scored = score_finding(rf)
            replacement, fips, reason = PQC_MAP[base]
            all_findings.append({
                "file": rf.file, "line": rf.line,
                "algorithm": rf.algorithm,
                "severity": scored.severity.value,
                "quantum_risk_score": scored.quantum_risk_score,
                "pqc_replacement": replacement,
                "fips_standard": fips,
                "reason": reason,
                "critical_path": getattr(rf, "critical_path", False),
                "breakable_by_2030": QASS.get(base, {}).get("breakable_by_2030", True),
            })

    unique = list({f["algorithm"].split("-")[0] for f in all_findings})
    critical = sum(1 for f in all_findings if f["severity"] == "CRITICAL")
    high     = sum(1 for f in all_findings if f["severity"] == "HIGH")

    # Deduplicate: worst finding per (file, base_algo), cap at 15
    seen: set[tuple] = set()
    top_findings = []
    for f in sorted(all_findings, key=lambda x: x["quantum_risk_score"], reverse=True):
        key = (f["file"], f["algorithm"].split("-")[0])
        if key not in seen:
            seen.add(key)
            top_findings.append(f)
        if len(top_findings) >= 15:
            break

    # Short threat summary (small prompt = no blank response)
    threat = ""
    if unique:
        try:
            r = _llm.chat.completions.create(
                model=AIML_MODEL,
                messages=[{"role": "user", "content":
                    f"In 2 sentences, explain the quantum threat from: {', '.join(unique)}. "
                    f"Mention Shor's/Grover's algorithm and the 2030 FTQC deadline."}],
                max_tokens=100, temperature=0.2,
            )
            threat = r.choices[0].message.content.strip()
        except Exception as e:
            log.warning(f"Threat summary error: {e}")
            threat = f"Found {len(all_findings)} quantum-vulnerable usages requiring urgent PQC migration."

    # Compact plain-text summary (what the LLM will read + relay to Band)
    lines = [
        f"✅ SCAN COMPLETE — {repo_url}",
        f"Files scanned: {len(files)} | Total findings: {len(all_findings)} | Critical: {critical} | High: {high}",
        f"Vulnerable algorithms: {', '.join(unique)}",
        f"Threat: {threat}",
        "",
        "Top findings:",
    ]
    for f in top_findings:
        lines.append(
            f"  [{f['severity']}] {f['file']}:{f['line']} "
            f"{f['algorithm']} → {f['pqc_replacement']} ({f['fips_standard']})"
        )

    return json.dumps({
        "repo_url": repo_url,
        "total_files_scanned": len(files),
        "findings_count": len(all_findings),
        "critical_count": critical,
        "high_count": high,
        "unique_algorithms": unique,
        "ai_threat_summary": threat,
        "compact_summary": "\n".join(lines),
        "qass_summary": {a: QASS[a] for a in unique if a in QASS},
        "findings": top_findings,   # capped at 15, no raw snippets
    })


SYSTEM_PROMPT = """
You are the VyalaArchon Assessment Agent — PQC vulnerability scanner.

When you receive a message containing SCAN_REPO <url>:
1. Call scan_repository with that URL ONCE.
2. Take the compact_summary field from the tool result.
3. Post EXACTLY this as your message — do not modify, shorten, or rewrite it:

   <paste compact_summary here verbatim>

   @banjarapadam62/qs-curator BUILD_LEARNING_PATH

CRITICAL RULE: You ONLY respond if the message contains the exact phrase "SCAN_REPO" or "scan". 
If the message does NOT contain a scan command, you MUST output an empty string. Do not reply, do not acknowledge, do not say "Standing by". Just output nothing.
Do NOT paste raw JSON. Do NOT call the tool more than once.
Do NOT add your own commentary before or after the summary.
Your entire reply should be the compact_summary text plus the final mention line.
""".strip()


async def main():
    load_dotenv()
    agent_id, api_key = load_agent_config("qs_assessment")
    adapter = AimlAdapter(system_prompt=SYSTEM_PROMPT, tools=[scan_repository])
    agent = Agent.create(adapter=adapter, agent_id=agent_id, api_key=api_key)
    log.info("Assessment Agent running...")
    await agent.run()

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    asyncio.run(main())