from dataclasses import dataclass, field
from enum import Enum

class Severity(str, Enum):
    CRITICAL = "CRITICAL"
    HIGH     = "HIGH"
    MEDIUM   = "MEDIUM"
    LOW      = "LOW"
    REVIEW   = "NEEDS_HUMAN_REVIEW"

@dataclass
class CryptoFinding:
    file: str
    line: int
    algorithm: str
    use_case: str
    key_size: int
    critical_path: bool
    data_long_lived: bool
    quantum_vulnerable: bool
    raw_code_snippet: str = ""

@dataclass
class ScoredFinding(CryptoFinding):
    quantum_risk_score: float = 0.0
    severity: Severity = Severity.MEDIUM

@dataclass
class RemediationItem:
    finding: ScoredFinding
    pqc_replacement: str = ""
    fips_standard: str = ""
    iq_confidence: float = 0.0
    iq_citations: list[str] = field(default_factory=list)
    migration_effort_hours: float = 0.0
    hybrid_transition: bool = False
    code_guidance: str = ""
    status: str = "OK"
    review_reason: str = ""
    qass_threat_summary: str = ""