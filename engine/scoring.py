from models.findings import CryptoFinding, ScoredFinding, Severity

_BASE_SCORES = {"RSA": 9.1, "ECDSA": 9.1, "AES-128": 7.4}

def score_finding(finding: CryptoFinding) -> ScoredFinding:
    algo_base = finding.algorithm.split("-")[0]
    base = _BASE_SCORES.get(algo_base, 7.0)
    urgency = 1.0
    if finding.critical_path: urgency *= 1.3
    if finding.use_case == "key_exchange": urgency *= 1.2
    if finding.data_long_lived: urgency *= 1.4
    
    raw_score = min(base * urgency, 10.0)
    severity = Severity.CRITICAL if raw_score >= 9.0 else Severity.HIGH if raw_score >= 7.0 else Severity.MEDIUM
    
    return ScoredFinding(
        **{k: v for k, v in finding.__dict__.items()},
        quantum_risk_score=round(raw_score, 2), severity=severity
    )