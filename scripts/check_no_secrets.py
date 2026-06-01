#!/usr/bin/env python3
"""Fail CI when tracked files contain likely provider credentials.

The scanner intentionally reports only the pattern name and location, never the
matched value. It scans Git-tracked files by default so local ignored .env files
can contain developer secrets without making routine checks noisy.
"""

from __future__ import annotations

import argparse
import math
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


TOKEN_PATTERNS = [
    ("github_pat", re.compile(r"\bghp_[A-Za-z0-9_]{20,}\b")),
    ("resend_api_key", re.compile(r"\bre_[A-Za-z0-9_]{20,}\b")),
    ("razorpay_key_id", re.compile(r"\brzp_(?:test|live)_[A-Za-z0-9]{10,}\b")),
    ("twilio_sid_or_api_key", re.compile(r"\b(?:AC|SK)[0-9a-fA-F]{32}\b")),
    ("openai_api_key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b")),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
]

SENSITIVE_ASSIGNMENT = re.compile(
    r"""
    \b[A-Z0-9_.-]*
    (?:API[_-]?KEY|TOKEN|SECRET|PASSWORD|AUTH[_-]?TOKEN|WEBHOOK[_-]?SECRET)
    [A-Z0-9_.-]*\b
    \s*[:=]\s*
    (?P<value>[^"'#\s,}]+|"[^"]+"|'[^']+')
    """,
    re.VERBOSE,
)
EMAIL_LIKE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

ALLOW_MARKER = "allow-secret-pattern"
PLACEHOLDER_PARTS = (
    "${{",
    "${",
    "$env:",
    "change-this",
    "placeholder",
    "example",
    "dummy",
    "fake",
    "sample",
    "local",
    "ci-",
    "test-",
    "test_",
    "your-",
    "your_",
    "<",
    "...",
    "os.getenv",
)

SKIP_DIRS = {".git", "__pycache__", "node_modules", "dist", "staticfiles", ".pytest_cache", ".mypy_cache"}


@dataclass(frozen=True)
class Finding:
    path: Path
    line_number: int
    kind: str


def entropy(value: str) -> float:
    if not value:
        return 0.0
    counts = {char: value.count(char) for char in set(value)}
    length = len(value)
    return -sum((count / length) * math.log2(count / length) for count in counts.values())


def character_classes(value: str) -> int:
    classes = [
        any(char.islower() for char in value),
        any(char.isupper() for char in value),
        any(char.isdigit() for char in value),
        any(not char.isalnum() for char in value),
    ]
    return sum(1 for present in classes if present)


def normalized_value(value: str) -> str:
    return value.strip().strip("'\"").strip().strip(",;")


def looks_like_placeholder(value: str) -> bool:
    lowered = value.lower()
    return any(part in lowered for part in PLACEHOLDER_PARTS)


def looks_like_secret_assignment(value: str) -> bool:
    cleaned = normalized_value(value)
    if len(cleaned) < 20:
        return False
    if looks_like_placeholder(cleaned):
        return False
    if cleaned.startswith(("http://", "https://")) or EMAIL_LIKE.match(cleaned):
        return False
    return entropy(cleaned) >= 3.5 and character_classes(cleaned) >= 3


def scan_text(path: Path, text: str) -> list[Finding]:
    findings: list[Finding] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        if ALLOW_MARKER in line:
            continue
        for kind, pattern in TOKEN_PATTERNS:
            if pattern.search(line):
                findings.append(Finding(path, line_number, kind))
        for match in SENSITIVE_ASSIGNMENT.finditer(line):
            if looks_like_secret_assignment(match.group("value")):
                findings.append(Finding(path, line_number, "high_entropy_sensitive_assignment"))
    return findings


def git_tracked_files(root: Path) -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=root,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=False,
    )
    if result.returncode != 0:
        return []
    files = [root / item.decode("utf-8", errors="replace") for item in result.stdout.split(b"\0") if item]
    return [path for path in files if path.is_file()]


def walked_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for path in root.rglob("*"):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.is_file():
            files.append(path)
    return files


def scan_files(root: Path) -> list[Finding]:
    findings: list[Finding] = []
    files = git_tracked_files(root) or walked_files(root)
    for path in files:
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        findings.extend(scan_text(path.relative_to(root), text))
    return findings


def run_self_test() -> None:
    samples = [
        ("github", "GITHUB_TOKEN=" + "ghp_" + ("A" * 36), True),
        ("provider", "RESEND_API_KEY=" + "re_" + ("A" * 25), True),
        ("razorpay", "RAZORPAY_KEY_ID=" + "rzp_test_" + ("A" * 14), True),
        ("twilio", "TWILIO_ACCOUNT_SID=" + "SK" + ("a" * 32), True),
        ("generic", "SHIPROCKET_PASSWORD=\"" + "Ab3#Zy9$Qw8!Lm7@Np6%Rt5^" + "\"", True),
        ("placeholder", "RESEND_API_KEY=change-this-resend-key", False),
        ("ci", "RAZORPAY_KEY_SECRET=ci-razorpay-secret", False),
    ]
    failures = []
    for name, text, should_flag in samples:
        flagged = bool(scan_text(Path(name), text))
        if flagged != should_flag:
            failures.append(name)
    if failures:
        raise SystemExit(f"Secret scanner self-test failed: {', '.join(failures)}")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scan tracked files for likely committed secrets.")
    parser.add_argument("--root", default=".", help="Repository root to scan.")
    parser.add_argument("--self-test", action="store_true", help="Run scanner self-tests before scanning.")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    if args.self_test:
        run_self_test()

    root = Path(args.root).resolve()
    findings = scan_files(root)
    if not findings:
        print("Secret scan passed: no likely credentials found in tracked files.")
        return 0

    print("Secret scan failed. Rotate the credential, remove it from tracked files, and commit the sanitized change.")
    for finding in findings:
        print(f"- {finding.path}:{finding.line_number} matched {finding.kind}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
