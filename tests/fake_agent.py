#!/usr/bin/env python3
"""Deterministic stand-in for a reviewer/verifier agent.

Speaks the sieve agent protocol: prompt on stdin, JSON on stdout. Reviewer
mode when the prompt names a dimension; verifier mode when it carries a
finding. Wraps its answer in a Claude Code `--output-format json` envelope
half the time, so the parser is exercised both ways.
"""
import json
import re
import sys

prompt = sys.stdin.read()

if "Finding under test:" in prompt:
    finding = json.loads(re.search(r"Finding under test:\n(\{.*?\})\n", prompt, re.S).group(1))
    claim = finding["claim"]
    if "no CI" in claim:
        out = {"verdict": "CONFIRMED", "correction": "", "evidence": "$ ls .github/workflows\nls: cannot access '.github/workflows': No such file or directory"}
    elif "secret" in claim:
        out = {"verdict": "REFUTED", "correction": "", "evidence": "- ran: git grep -n AKIA\n(no matches)\nThe string in config.example is a placeholder."}
    else:
        out = {"verdict": "PARTLY", "correction": claim + " (in src/ only; tests/ is covered)", "severity": "low",
               "evidence": "$ grep -rL 'assert' src tests\nsrc/lib.py"}
    envelope = {"type": "result", "result": "Here is my verdict:\n" + json.dumps(out)}
    print(json.dumps(envelope))
    sys.exit(0)

dim = re.search(r"Dimension: (\S+)", prompt).group(1)
findings = {
    "docs": [
        {"claim": "README promises CI but the repo has no CI configuration", "evidence": "README.md:3 says 'CI on every push'; no .github/workflows", "classification": "OVERSTATING", "severity": "high"},
        {"claim": "README documents the install command accurately", "evidence": "README.md:7 matches Makefile:install", "classification": "HONEST", "severity": "info"},
    ],
    "security": [
        {"claim": "A hard-coded AWS secret appears in config.example", "evidence": "config.example:2 AKIA...", "classification": "THEATER", "severity": "high"},
        {"claim": "README promises CI but the repo has no CI configuration", "evidence": "README.md:3 says 'CI on every push'; no .github/workflows", "classification": "OVERSTATING", "severity": "high"},
    ],
    "tests": [
        {"claim": "No tests assert anything", "evidence": "grep -rL assert", "classification": "VACUOUS", "severity": "medium"},
    ],
}[dim]
print(json.dumps({"findings": findings, "summary": f"{dim}: {len(findings)} findings"}))
