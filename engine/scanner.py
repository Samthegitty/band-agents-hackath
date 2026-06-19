# engine/scanner.py
import os
import re
import logging
from models.findings import CryptoFinding
from engine.parsers.python_parser import PythonParser
from engine.parsers.js_parser import JavaScriptParser

log = logging.getLogger("vyala_archon")

# --- Business Context Heuristics ---
CRITICAL_PATH_SIGNALS = ["auth", "jwt", "tls", "ssl", "sign", "crypto", "login", "token", "password", "payment"]
LONG_LIVED_DATA_SIGNALS = ["health", "medical", "finance", "bank", "legal", "pii", "gdpr", "record", "user", "account"]

def _is_critical_path(filepath: str, snippet: str) -> bool:
    combined = (filepath + snippet).lower()
    return any(sig in combined for sig in CRITICAL_PATH_SIGNALS)

def _is_long_lived(filepath: str) -> bool:
    return any(sig in filepath.lower() for sig in LONG_LIVED_DATA_SIGNALS)

# Initialize AST Parsers
py_parser = PythonParser()
js_parser = JavaScriptParser()

def scan_file_content(filepath: str, content: str) -> list[CryptoFinding]:
    """
    The Dispatch Hub. Routes files to the correct AST parser 
    or falls back to Regex.
    """
    ext = os.path.splitext(filepath)[1].lower()
    findings = []
    
    # 🧠 Python Tree-sitter
    if ext == '.py':
        findings = py_parser.parse_content(filepath, content)
    # 🧠 JavaScript / TypeScript Tree-sitter
    elif ext in ['.js', '.jsx', '.ts', '.tsx', '.mjs', '.cjs']:
        findings = js_parser.parse_content(filepath, content)
    # 🧠 Regex Fallback for Go/Java/etc
    else:
        findings = _regex_scan(filepath, content)

    # Enrich AST/Regex findings with Business Context
    for f in findings:
        f.critical_path = _is_critical_path(filepath, f.raw_code_snippet)
        f.data_long_lived = _is_long_lived(filepath)
        
    return findings

def _regex_scan(filepath: str, content: str) -> list[CryptoFinding]:
    """Fallback for languages without AST parsers yet."""
    CRYPTO_PATTERNS = {
        "RSA": {"patterns": [r"RSA\.generate\s*\(", r"crypto\.generateKeyPairSync\(['\"]rsa['\"]"], "use_case": "signing_or_key_exchange", "key_sizes": [2048], "quantum_vulnerable": True},
        "ECDSA": {"patterns": [r"ECDSA\s*\("], "use_case": "signing", "key_sizes": [256], "quantum_vulnerable": True},
        "AES-128": {"patterns": [r"AES\.new\s*\(.*128", r"aes-128", r"createCipheriv\(['\"]aes-128"], "use_case": "symmetric", "key_sizes": [128], "quantum_vulnerable": True},
        "MD5": {"patterns": [r"hashlib\.md5\s*\(", r"crypto\.createHash\(['\"]md5['\"]\)"], "use_case": "hashing", "key_sizes": [128], "quantum_vulnerable": True},
        "SHA1": {"patterns": [r"hashlib\.sha1\s*\(", r"crypto\.createHash\(['\"]sha1['\"]\)"], "use_case": "hashing", "key_sizes": [160], "quantum_vulnerable": True},
        "JWT": {"patterns": [r"jwt\.sign\s*\(", r"jwt\.verify\s*\("], "use_case": "signing", "key_sizes": [256], "quantum_vulnerable": True},
    }
    
    findings = []
    for algo_name, config in CRYPTO_PATTERNS.items():
        for pattern in config["patterns"]:
            for match in re.finditer(pattern, content, re.IGNORECASE):
                line_num = content[: match.start()].count("\n") + 1
                snippet = content[max(0, match.start() - 60): match.end() + 60].strip()
                key_size = 2048 if "2048" in snippet else config["key_sizes"][0]
                
                findings.append(CryptoFinding(
                    file=filepath, line=line_num, algorithm=f"{algo_name}-{key_size}",
                    use_case=config["use_case"], key_size=key_size,
                    critical_path=False, data_long_lived=False,
                    quantum_vulnerable=config["quantum_vulnerable"], raw_code_snippet=snippet
                ))
                break 
    return findings