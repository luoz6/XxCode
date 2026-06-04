"""Dangerous command detection patterns — enhanced with Zsh and advanced patterns."""

import re

# ── Core dangerous patterns ──────────────────────────────────────────

DANGEROUS_PATTERNS: list[re.Pattern] = [
    # File destruction
    re.compile(r'\brm\s'),
    re.compile(r'\brmdir\s'),
    # Git destructive operations
    re.compile(r'\bgit\s+(?:push|reset|clean|checkout\s+\.)'),
    re.compile(r'\bgit\s+push\s+.*(?:--force|-f\b)', re.IGNORECASE),
    re.compile(r'\bgit\s+commit\s+--amend'),
    # Privilege escalation
    re.compile(r'\bsudo\b'),
    re.compile(r'\bdoas\b'),
    re.compile(r'\bpkexec\b'),
    re.compile(r'\bsu\s'),
    # Filesystem destruction
    re.compile(r'\bmkfs\b'),
    re.compile(r'\bdd\s'),
    re.compile(r'>\s*/dev/(?:sd|hd|nvme|mmcblk)'),
    # Process termination
    re.compile(r'\bkill\b'),
    re.compile(r'\bpkill\b'),
    re.compile(r'\bkillall\b'),
    # System shutdown/reboot
    re.compile(r'\breboot\b'),
    re.compile(r'\bshutdown\b'),
    re.compile(r'\bhalt\b'),
    re.compile(r'\bpoweroff\b'),
    # Permission changes
    re.compile(r'\bchmod\s+777'),
    re.compile(r'\bchmod\s+.*777'),
    re.compile(r'\bchown\s'),
    re.compile(r'\bchattr\s'),
    # Fork bomb detection
    re.compile(r'\bfork\s+bomb|:\s*\(\)\s*\{'),
    # Pipe to shell
    re.compile(r'\bwget\s.*\|\s*(?:ba)?sh'),
    re.compile(r'\bcurl\s.*\|\s*(?:ba)?sh'),
    # Network data exfiltration
    re.compile(r'\bnc\s+.*-e\s'),
    re.compile(r'\bncat\s+.*-e\s'),
    # Disk/device manipulation
    re.compile(r'\bfdisk\b'),
    re.compile(r'\bparted\b'),
    re.compile(r'\bmount\s'),
    re.compile(r'\bumount\s'),
    # Kernel module manipulation
    re.compile(r'\bmodprobe\b'),
    re.compile(r'\binsmod\b'),
    re.compile(r'\brmmod\b'),
    # System configuration changes
    re.compile(r'\bsysctl\s'),
    re.compile(r'\biptables\b'),
    re.compile(r'\bnft\b'),
    re.compile(r'\bufw\s'),
    # Service manipulation
    re.compile(r'\bsystemctl\s+(?:start|stop|restart|enable|disable|mask)'),
    re.compile(r'\bservice\s+\w+\s+(?:start|stop|restart)'),
    # Cron/scheduled task manipulation
    re.compile(r'\bcrontab\s'),
    # User account manipulation
    re.compile(r'\buseradd\b'),
    re.compile(r'\buserdel\b'),
    re.compile(r'\busermod\b'),
    re.compile(r'\bpasswd\b'),
    # Dangerous find -exec
    re.compile(r'\bfind\s.*-exec\b'),
    # Dangerous Xargs
    re.compile(r'\bxargs\s.*\brm\b'),
    re.compile(r'\bxargs\s.*\bsh\b'),
    # Environment variable injection
    re.compile(r'\bLD_PRELOAD\s*='),
    re.compile(r'\bLD_LIBRARY_PATH\s*='),
    # Reverse shells
    re.compile(r'\b(?:nc|ncat|netcat)\s+.*(?:-e|--exec)\s+(?:/bin/|/usr/bin/)?(?:ba)?sh'),
    re.compile(r'\b(?:python|python3|ruby|perl|php)\s+.*(?:socket|subprocess|exec)'),
]

# Deduplicated combined dangerous base commands.
DANGEROUS_BASE_COMMANDS: set[str] = {
    "rm", "rmdir", "sudo", "doas", "pkexec", "su",
    "mkfs", "dd", "fdisk", "parted", "mount", "umount",
    "kill", "pkill", "killall",
    "reboot", "shutdown", "halt", "poweroff",
    "chmod", "chown", "chattr",
    "modprobe", "insmod", "rmmod",
    "iptables", "nft", "ufw",
    "crontab",
    "useradd", "userdel", "usermod", "passwd",
}


def is_dangerous(command: str) -> bool:
    """Check if a shell command matches any dangerous pattern."""
    return any(p.search(command) for p in DANGEROUS_PATTERNS)


# ── Risk levels ─────────────────────────────────────────────────────

class RiskLevel:
    SAFE = "safe"
    WARNING = "warning"
    DANGEROUS = "dangerous"
    CRITICAL = "critical"


def assess_risk(command: str) -> str:
    """Assess the risk level of a shell command.

    Returns one of: safe, warning, dangerous, critical.
    """
    if not command.strip():
        return RiskLevel.SAFE

    # Critical: always-never patterns.
    critical_patterns = [
        re.compile(r'\brm\s+.*-rf\s+/'),
        re.compile(r'\brm\s+.*-rf\s+~'),
        re.compile(r':\s*\(\)\s*\{'),  # Fork bomb
        re.compile(r'\bsudo\s+rm\s+.*-rf\s+/'),
    ]
    for p in critical_patterns:
        if p.search(command):
            return RiskLevel.CRITICAL

    # Dangerous: destructive patterns.
    if is_dangerous(command):
        return RiskLevel.DANGEROUS

    # Warning: write operations.
    warning_patterns = [
        re.compile(r'>\s*\w'),
        re.compile(r'\b(?:mv|cp|touch|mkdir)\s'),
        re.compile(r'\bgit\s+(?:commit|branch|tag|merge|rebase)'),
        re.compile(r'\b(?:npm|yarn|pnpm|pip|pip3|cargo)\s+(?:install|add|remove|update)'),
    ]
    for p in warning_patterns:
        if p.search(command):
            return RiskLevel.WARNING

    return RiskLevel.SAFE
