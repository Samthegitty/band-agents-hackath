# engine/parsers/python_parser.py
from __future__ import annotations
import re
import logging
from typing import Iterator
import tree_sitter_python as tspython
from tree_sitter import Language, Node, Parser

# Import our new standard finding model
from models.findings import CryptoFinding

log = logging.getLogger("vyala_archon")

# ==============================================================================
# DETECTION KNOWLEDGE BASE (Your excellent mapping, kept intact)
# ==============================================================================

_MODULE_SIGNATURES: dict[str, dict] = {
    "Crypto":       {"algo": "PyCryptodome", "quantum_vulnerable": True, "use_case": "signing_or_key_exchange"},
    "Cryptodome":   {"algo": "PyCryptodome", "quantum_vulnerable": True, "use_case": "signing_or_key_exchange"},
    "cryptography": {"algo": "PyCA/cryptography", "quantum_vulnerable": True, "use_case": "signing_or_key_exchange"},
    "rsa":          {"algo": "RSA", "quantum_vulnerable": True, "use_case": "signing_or_key_exchange"},
    "hashlib":      {"algo": "Hash", "quantum_vulnerable": True, "use_case": "hashing"},
    "hmac":         {"algo": "HMAC", "quantum_vulnerable": True, "use_case": "hashing"},
    "jwt":          {"algo": "JWT", "quantum_vulnerable": True, "use_case": "signing"},
    "jose":         {"algo": "JOSE", "quantum_vulnerable": True, "use_case": "signing"},
    "ssl":          {"algo": "TLS", "quantum_vulnerable": True, "use_case": "key_exchange"},
    "OpenSSL":      {"algo": "OpenSSL", "quantum_vulnerable": True, "use_case": "signing_or_key_exchange"},
    "ecdsa":        {"algo": "ECDSA", "quantum_vulnerable": True, "use_case": "signing"},
    "fastecdsa":    {"algo": "ECDSA", "quantum_vulnerable": True, "use_case": "signing"},
    "dh":           {"algo": "DH", "quantum_vulnerable": True, "use_case": "key_exchange"},
    "pyaes":        {"algo": "AES", "quantum_vulnerable": True, "use_case": "symmetric"},
}

_SUBMODULE_ALGORITHM_MAP: dict[str, str] = {
    "rsa": "RSA", "dsa": "DSA", "ecdsa": "ECDSA", "ec": "ECC",
    "ecdh": "ECDH", "aes": "AES", "md5": "MD5", "sha1": "SHA-1",
    "sha256": "SHA-256", "x25519": "X25519", "ed25519": "Ed25519",
}

_KEY_SIZE_PATTERN = re.compile(r"\b(512|1024|2048|3072|4096|8192|128|192|256)\b")

# ==============================================================================
# CONCRETE PARSER
# ==============================================================================

class PythonParser:
    """
    Tree-sitter–powered scanner for vulnerable cryptography in Python source files.
    Adapted for Micro-Vyala-Agent to parse string content instead of directories.
    """

    def __init__(self) -> None:
        self._ts_language = Language(tspython.language())
        self.parser = Parser(self._ts_language)

    def parse_content(self, filepath: str, content: str) -> list[CryptoFinding]:
        """Parse a single file's content string and return findings."""
        source_bytes = content.encode("utf-8", errors="replace")
        tree = self.parser.parse(source_bytes)

        if tree is None or tree.root_node is None:
            return []

        findings = list(self._extract_crypto_nodes(tree, filepath, source_bytes))
        
        # Deduplicate (Tree-sitter can sometimes hit the same node twice)
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
                if node.type in ("import_statement", "import_from_statement"):
                    finding = self._analyse_import_node(node, filepath, source_bytes, node.type)
                    if finding is not None:
                        yield finding
                if cursor.goto_first_child():
                    visited_children = False
                    continue

            if cursor.goto_next_sibling():
                visited_children = False
            elif cursor.goto_parent():
                visited_children = True
            else:
                break

    def _analyse_import_node(self, node: Node, filepath: str, source_bytes: bytes, node_type: str) -> CryptoFinding | None:
        node_text = self._node_text(node, source_bytes)
        line_number = node.start_point[0] + 1

        root_module = self._extract_root_module(node, source_bytes, node_type)
        if root_module is None:
            return None

        signature = _MODULE_SIGNATURES.get(root_module)
        if signature is None:
            return None

        # Refine algorithm name
        algorithm_name = signature["algo"]
        refined_algorithm = self._refine_algorithm_from_node(node, source_bytes, node_type)
        if refined_algorithm:
            algorithm_name = refined_algorithm

        # Refine key size
        key_size = 0
        match = _KEY_SIZE_PATTERN.search(node_text)
        if match:
            key_size = int(match.group())
            algorithm_name = f"{algorithm_name}-{key_size}"

        return CryptoFinding(
            file=filepath,
            line=line_number,
            algorithm=algorithm_name,
            use_case=signature["use_case"],
            key_size=key_size if key_size > 0 else 2048, # Default fallback
            critical_path=False, # Will be enriched by scanner.py heuristics
            data_long_lived=False, # Will be enriched by scanner.py heuristics
            quantum_vulnerable=signature["quantum_vulnerable"],
            raw_code_snippet=node_text
        )

    # --- Tree-sitter Helpers (Kept exactly as you wrote them) ---
    def _extract_root_module(self, node: Node, source_bytes: bytes, node_type: str) -> str | None:
        if node_type == "import_statement":
            for child in node.children:
                if child.type in ("dotted_name", "aliased_import"):
                    for sub in child.children:
                        if sub.type == "identifier":
                            return self._node_text(sub, source_bytes)
                elif child.type == "identifier":
                    return self._node_text(child, source_bytes)
        elif node_type == "import_from_statement":
            for child in node.children:
                if child.type == "dotted_name":
                    for sub in child.children:
                        if sub.type == "identifier":
                            return self._node_text(sub, source_bytes)
                    raw = self._node_text(child, source_bytes)
                    return raw.split(".")[0] if raw else None
        return None

    def _refine_algorithm_from_node(self, node: Node, source_bytes: bytes, node_type: str) -> str | None:
        if node_type != "import_from_statement":
            return None
        past_import_keyword = False
        for child in node.children:
            if child.type == "import":
                past_import_keyword = True
                continue
            if past_import_keyword:
                identifiers = self._collect_identifiers(child, source_bytes)
                for ident in identifiers:
                    mapped = _SUBMODULE_ALGORITHM_MAP.get(ident.lower())
                    if mapped:
                        return mapped
        for child in node.children:
            if child.type == "dotted_name":
                segments = self._node_text(child, source_bytes).split(".")
                for segment in segments[1:]:
                    mapped = _SUBMODULE_ALGORITHM_MAP.get(segment.lower())
                    if mapped:
                        return mapped
        return None

    @staticmethod
    def _node_text(node: Node, source_bytes: bytes) -> str:
        try:
            raw = source_bytes[node.start_byte : node.end_byte]
            return raw.decode("utf-8", errors="replace").strip()
        except (AttributeError, ValueError):
            return ""

    def _collect_identifiers(self, node: Node, source_bytes: bytes) -> list[str]:
        results: list[str] = []
        if node.type == "identifier":
            text = self._node_text(node, source_bytes)
            if text:
                results.append(text)
        for child in node.children:
            results.extend(self._collect_identifiers(child, source_bytes))
        return results