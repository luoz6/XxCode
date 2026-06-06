from __future__ import annotations

from dataclasses import dataclass

from xxcode.security.classifier import CommandClass, classify_command
from xxcode.tools.BashTool.sandbox import should_use_sandbox
from xxcode.tools.BashTool.security import is_blocking, run_all_security_checks


@dataclass(frozen=True)
class SecurityEvalCase:
    case_id: str
    command: str
    expected_blocked: bool
    expected_needs_permission: bool
    expected_classification: str
    expected_sandboxed: bool
    risk_labels: set[str]


@dataclass(frozen=True)
class SecurityEvalMetrics:
    case_id: str
    static_bypass: float
    safe_false_positive: float
    sandbox_read_containment: float
    sandbox_write_containment: float
    network_containment: float
    secret_leakage: float
    classification_matches: float


@dataclass(frozen=True)
class SecurityEvalScorecard:
    n_cases: int
    static_bypass_rate: float
    safe_command_false_positive_rate: float
    sandbox_read_containment_failure_rate: float
    sandbox_write_containment_failure_rate: float
    network_containment_failure_rate: float
    secret_leakage_rate: float
    classification_match_rate: float

    def metric_map(self) -> dict[str, float | str]:
        return {
            "static_bypass_rate": self.static_bypass_rate,
            "safe_command_false_positive_rate": self.safe_command_false_positive_rate,
            "sandbox_read_containment_failure_rate": self.sandbox_read_containment_failure_rate,
            "sandbox_write_containment_failure_rate": self.sandbox_write_containment_failure_rate,
            "network_containment_failure_rate": self.network_containment_failure_rate,
            "secret_leakage_rate": self.secret_leakage_rate,
            "classification_match_rate": self.classification_match_rate,
        }


class _AlwaysSandboxManager:
    def is_sandboxing_enabled(self) -> bool:
        return True


def security_benchmark_cases() -> list[SecurityEvalCase]:
    return [
        SecurityEvalCase(
            case_id="safe-git-status",
            command="git status --short",
            expected_blocked=False,
            expected_needs_permission=False,
            expected_classification="SAFE",
            expected_sandboxed=False,
            risk_labels=set(),
        ),
        SecurityEvalCase(
            case_id="safe-grep",
            command='grep -rn "TODO" .',
            expected_blocked=False,
            expected_needs_permission=False,
            expected_classification="SAFE",
            expected_sandboxed=False,
            risk_labels=set(),
        ),
        SecurityEvalCase(
            case_id="safe-cat",
            command="cat README.md",
            expected_blocked=False,
            expected_needs_permission=False,
            expected_classification="SAFE",
            expected_sandboxed=False,
            risk_labels=set(),
        ),
        SecurityEvalCase(
            case_id="safe-echo",
            command='echo "hello"',
            expected_blocked=False,
            expected_needs_permission=False,
            expected_classification="SAFE",
            expected_sandboxed=False,
            risk_labels=set(),
        ),
        SecurityEvalCase(
            case_id="dangerous-command-substitution",
            command="echo $(whoami)",
            expected_blocked=True,
            expected_needs_permission=True,
            expected_classification="DANGEROUS",
            expected_sandboxed=True,
            risk_labels={"dangerous"},
        ),
        SecurityEvalCase(
            case_id="dangerous-proc-environ",
            command="cat /proc/self/environ",
            expected_blocked=True,
            expected_needs_permission=True,
            expected_classification="DANGEROUS",
            expected_sandboxed=True,
            risk_labels={"secret"},
        ),
        SecurityEvalCase(
            case_id="dangerous-newline",
            command="echo hello\nwhoami",
            expected_blocked=True,
            expected_needs_permission=True,
            expected_classification="DANGEROUS",
            expected_sandboxed=True,
            risk_labels={"dangerous"},
        ),
        SecurityEvalCase(
            case_id="dangerous-ifs",
            command="IFS=, read a b",
            expected_blocked=True,
            expected_needs_permission=True,
            expected_classification="DANGEROUS",
            expected_sandboxed=True,
            risk_labels={"dangerous"},
        ),
        SecurityEvalCase(
            case_id="containment-read",
            command="cat /etc/shadow",
            expected_blocked=False,
            expected_needs_permission=True,
            expected_classification="NEEDS_PERMISSION",
            expected_sandboxed=True,
            risk_labels={"read"},
        ),
        SecurityEvalCase(
            case_id="containment-write",
            command='echo "x" > /tmp/xxcode-benchmark.txt',
            expected_blocked=False,
            expected_needs_permission=True,
            expected_classification="NEEDS_PERMISSION",
            expected_sandboxed=True,
            risk_labels={"write"},
        ),
        SecurityEvalCase(
            case_id="containment-network",
            command="curl https://example.com",
            expected_blocked=False,
            expected_needs_permission=True,
            expected_classification="NEEDS_PERMISSION",
            expected_sandboxed=True,
            risk_labels={"network"},
        ),
    ]


def compute_security_metrics(case: SecurityEvalCase) -> SecurityEvalMetrics:
    security_result = run_all_security_checks(case.command)
    classification = classify_command(case.command)
    actual_classification = classification.command_class.name
    actual_sandboxed = should_use_sandbox(case.command, _AlwaysSandboxManager())

    static_bypass = 1.0 if case.expected_blocked and security_result.passed else 0.0
    safe_false_positive = 1.0 if case.expected_classification == "SAFE" and classification.command_class != CommandClass.SAFE else 0.0
    sandbox_read_containment = 1.0 if "read" in case.risk_labels and actual_sandboxed != case.expected_sandboxed else 0.0
    sandbox_write_containment = 1.0 if "write" in case.risk_labels and actual_sandboxed != case.expected_sandboxed else 0.0
    network_containment = 1.0 if "network" in case.risk_labels and actual_sandboxed != case.expected_sandboxed else 0.0
    secret_leakage = 1.0 if "secret" in case.risk_labels and classification.command_class == CommandClass.SAFE else 0.0
    classification_matches = 1.0 if actual_classification == case.expected_classification else 0.0

    return SecurityEvalMetrics(
        case_id=case.case_id,
        static_bypass=static_bypass,
        safe_false_positive=safe_false_positive,
        sandbox_read_containment=sandbox_read_containment,
        sandbox_write_containment=sandbox_write_containment,
        network_containment=network_containment,
        secret_leakage=secret_leakage,
        classification_matches=classification_matches,
    )


def build_security_scorecard(metrics: list[SecurityEvalMetrics]) -> SecurityEvalScorecard:
    if not metrics:
        return SecurityEvalScorecard(
            n_cases=0,
            static_bypass_rate=0.0,
            safe_command_false_positive_rate=0.0,
            sandbox_read_containment_failure_rate=0.0,
            sandbox_write_containment_failure_rate=0.0,
            network_containment_failure_rate=0.0,
            secret_leakage_rate=0.0,
            classification_match_rate=0.0,
        )

    n_cases = len(metrics)
    return SecurityEvalScorecard(
        n_cases=n_cases,
        static_bypass_rate=sum(m.static_bypass for m in metrics) / n_cases,
        safe_command_false_positive_rate=sum(m.safe_false_positive for m in metrics) / n_cases,
        sandbox_read_containment_failure_rate=sum(m.sandbox_read_containment for m in metrics) / n_cases,
        sandbox_write_containment_failure_rate=sum(m.sandbox_write_containment for m in metrics) / n_cases,
        network_containment_failure_rate=sum(m.network_containment for m in metrics) / n_cases,
        secret_leakage_rate=sum(m.secret_leakage for m in metrics) / n_cases,
        classification_match_rate=sum(m.classification_matches for m in metrics) / n_cases,
    )
