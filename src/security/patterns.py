"""Dangerous command detection patterns."""

import re

DANGEROUS_PATTERNS: list[re.Pattern] = [
    re.compile(r'\brm\s'),
    re.compile(r'\bgit\s+(push|reset|clean|checkout\s+\.)'),
    re.compile(r'\bsudo\b'),
    re.compile(r'\bmkfs\b'),
    re.compile(r'\bdd\s'),
    re.compile(r'>\s*/dev/'),
    re.compile(r'\bkill\b'),
    re.compile(r'\bpkill\b'),
    re.compile(r'\breboot\b'),
    re.compile(r'\bshutdown\b'),
    re.compile(r'\bchmod\s+777'),
    re.compile(r'\bchown\s'),
    re.compile(r'\bfork\s+bomb|:\s*\(\)\s*\{'),
    re.compile(r'\bwget\s.*\|\s*(ba)?sh'),
    re.compile(r'\bcurl\s.*\|\s*(ba)?sh'),
]


def is_dangerous(command: str) -> bool:
    """Check if a shell command matches any dangerous pattern."""
    return any(p.search(command) for p in DANGEROUS_PATTERNS)
