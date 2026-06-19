# engine/parsers/js_parser.py
from __future__ import annotations
import re
import logging
from typing import Iterator
import tree_sitter_javascript as tsjavascript
from tree_sitter import Language, Node, Parser

from models.findings import CryptoFinding

log = logging.getLogger("vyala_archon")

# ==============================================================================
# JS/TS CRYPTO KNOWLEDGE BASE (Your mappings, kept intact)
# ==============================================================================
_MODULE_SIGNATURES: dict[str, dict] = {
    "crypto":       {"algo": "Node-crypto", "use_case": "signing_or_key_exchange", "quantum_vulnerable": True},
    "tls":          {"algo": "TLS", "use_case": "key_exchange", "quantum_vulnerable": True},
    "https":        {"algo": "HTTPS/TLS", "use_case": "key_exchange", "quantum_vulnerable": True},
    "jsonwebtoken": {"algo": "JWT", "use_case": "signing", "quantum_vulnerable": True},
    "jose":         {"algo": "JOSE", "use_case": "signing", "quantum_vulnerable": True},
    "node-rsa":     {"algo": "RSA", "use_case": "signing_or_key_exchange", "quantum_vulnerable": True},
    "node-forge":   {"algo": "RSA/ECC/DES", "use_case": "signing_or_key_exchange", "quantum_vulnerable": True},
    "jsrsasign":    {"algo": "RSA/ECDSA", "use_case": "signing", "quantum_vulnerable": True},
    "elliptic":     {"algo": "ECC", "use_case": "signing", "quantum_vulnerable": True},
    "crypto-js":    {"algo": "CryptoJS", "use_case": "symmetric", "quantum_vulnerable": True},
    "aes-js":       {"algo": "AES", "use_case": "symmetric", "quantum_vulnerable": True},
    "bcrypt":       {"algo": "bcrypt", "use_case": "hashing", "quantum_vulnerable": True},
    "openpgp":      {"algo": "PGP/RSA", "use_case": "signing_or_key_exchange", "quantum_vulnerable": True},
}

_ALGORITHM_STRING_MAP: dict[str, dict] = {
    "rsa-oaep":         {"algo": "RSA-OAEP", "use_case": "signing_or_key_exchange", "quantum_vulnerable": True},
    "rsassa-pkcs1-v1_5":{"algo": "RSA-PKCS1", "use_case": "signing", "quantum_vulnerable": True},
    "rsa-pss":          {"algo": "RSA-PSS", "use_case": "signing", "quantum_vulnerable": True},
    "ecdsa":            {"algo": "ECDSA", "use_case": "signing", "quantum_vulnerable": True},
    "ecdh":             {"algo": "ECDH", "use_case": "key_exchange", "quantum_vulnerable": True},
    "md5":              {"algo": "MD5", "use_case": "hashing", "quantum_vulnerable": True},
    "sha1":             {"algo": "SHA-1", "use_case": "hashing", "quantum_vulnerable": True},
    "sha-1":            {"algo": "SHA-1", "use_case": "hashing", "quantum_vulnerable": True},
    "aes-128-cbc":      {"algo": "AES-128-CBC", "use_case": "symmetric", "quantum_vulnerable": True},
    "aes-128-gcm":      {"algo": "AES-128-GCM", "use_case": "symmetric", "quantum_vulnerable": True},
    "rs256":            {"algo": "RSA-SHA256", "use_case": "signing", "quantum_vulnerable": True},
    "es256":            {"algo": "ECDSA-SHA256", "use_case": "signing", "quantum_vulnerable": True},
    "p-256":            {"algo": "ECC-P256", "use_case": "signing", "quantum_vulnerable": True},
    "des":              {"algo": "DES", "use_case": "symmetric", "quantum_vulnerable": True},
    "rc4":              {"algo": "RC4", "use_case": "symmetric", "quantum_vulnerable": True},
}

_CRYPTO_METHOD_NAMES = frozenset({
    "createhash", "createhmac", "createcipher", "createcipheriv",
    "createdecipher", "createdecipheriv", "createsign", "createverify",
    "generatekeypair", "generatekeypairsync", "sign", "verify", "encrypt", "decrypt"
})

class JavaScriptParser:
    def __init__(self) -> None:
        self._ts_language = Language(tsjavascript.language())
        self.parser = Parser(self._ts_language)

    def parse_content(self, filepath: str, content: str) -> list[CryptoFinding]:
        source_bytes = content.encode("utf-8", errors="replace")
        tree = self.parser.parse(source_bytes)
        if tree is None or tree.root_node is None: return []
        
        findings = list(self._extract_crypto_nodes(tree, filepath, source_bytes))
        unique_findings = []
        seen = set()
        for f in findings:
            key = (f.file, f.line, f.algorithm)
            if key not in seen:
                seen.add(key)
                unique_findings.append(f)
        return unique_findings

    def _extract_crypto_nodes(self, tree, filepath: str, source_bytes: bytes) -> Iterator[CryptoFinding]:
        cursor = tree.walk()
        visited_children = False
        while True:
            node = cursor.node
            if not visited_children:
                if node.type == "import_statement":
                    finding = self._analyse_import(node, filepath, source_bytes)
                    if finding: yield finding
                elif node.type == "call_expression":
                    finding = self._analyse_require(node, filepath, source_bytes)
                    if finding: yield finding
                elif node.type == "string":
                    finding = self._analyse_string_literal(node, filepath, source_bytes)
                    if finding: yield finding
                
                if cursor.goto_first_child():
                    visited_children = False
                    continue
            if cursor.goto_next_sibling():
                visited_children = False
            elif cursor.goto_parent():
                visited_children = True
            else: break

    def _analyse_import(self, node: Node, filepath: str, source_bytes: bytes) -> CryptoFinding | None:
        node_text = self._node_text(node, source_bytes)
        module_name = self._extract_quoted_string(node_text)
        if not module_name: return None
        return self._match_module(module_name, node, filepath, source_bytes)

    def _analyse_require(self, node: Node, filepath: str, source_bytes: bytes) -> CryptoFinding | None:
        node_text = self._node_text(node, source_bytes)
        if not node_text.startswith("require("): return None
        module_name = self._extract_quoted_string(node_text)
        if not module_name: return None
        return self._match_module(module_name, node, filepath, source_bytes)

    def _match_module(self, module_name: str, node: Node, filepath: str, source_bytes: bytes) -> CryptoFinding | None:
        lower = module_name.lower()
        for key, sig in _MODULE_SIGNATURES.items():
            if key == lower or lower.endswith(f"/{key}"):
                line_number = node.start_point[0] + 1
                return CryptoFinding(
                    file=filepath, line=line_number, algorithm=sig["algo"],
                    use_case=sig["use_case"], key_size=0,
                    critical_path=False, data_long_lived=False,
                    quantum_vulnerable=sig["quantum_vulnerable"],
                    raw_code_snippet=self._node_text(node, source_bytes)
                )
        return None

    def _analyse_string_literal(self, node: Node, filepath: str, source_bytes: bytes) -> CryptoFinding | None:
        raw = self._node_text(node, source_bytes)
        algo_str = raw.strip("\"'`").lower()
        sig = _ALGORITHM_STRING_MAP.get(algo_str)
        if sig is None: return None
        if not self._is_in_crypto_context(node, source_bytes): return None

        line_number = node.start_point[0] + 1
        context_text = self._get_statement_text(node, source_bytes)
        return CryptoFinding(
            file=filepath, line=line_number, algorithm=sig["algo"],
            use_case=sig["use_case"], key_size=0,
            critical_path=False, data_long_lived=False,
            quantum_vulnerable=sig["quantum_vulnerable"],
            raw_code_snippet=context_text or raw
        )

    def _is_in_crypto_context(self, node: Node, source_bytes: bytes) -> bool:
        current = node.parent
        for _ in range(5):
            if current is None: break
            if current.type in ("call_expression", "new_expression", "member_expression", "arguments"):
                text = self._node_text(current, source_bytes).lower()
                for method in _CRYPTO_METHOD_NAMES:
                    if method in text: return True
                if "name" in text and current.type in ("object", "pair"): return True
            current = current.parent
        return False

    def _get_statement_text(self, node: Node, source_bytes: bytes) -> str:
        current = node.parent
        for _ in range(4):
            if current is None: break
            if current.type in ("expression_statement", "variable_declaration", "lexical_declaration", "call_expression"):
                return self._node_text(current, source_bytes)
            current = current.parent
        return self._node_text(node, source_bytes)

    @staticmethod
    def _extract_quoted_string(text: str) -> str | None:
        match = re.search(r"""['"`]([^'"`]+)['"`]""", text)
        return match.group(1) if match else None

    @staticmethod
    def _node_text(node: Node, source_bytes: bytes) -> str:
        try:
            return source_bytes[node.start_byte:node.end_byte].decode("utf-8", errors="replace").strip()
        except (AttributeError, ValueError):
            return ""