#!/usr/bin/env python3
"""sieve — audit a repository into a ket DAG.

Findings are content-addressed claims. Verification is a typed edge. The
ledger is a rendering. Re-auditing is a diff.

    sieve run <repo> --dims dims.json          # fan out reviewers, fan in verifiers
    sieve ledger <root-cid>                    # LEDGER.md rendered from the DAG
    sieve diff <root-a> <root-b>               # what changed between two audits

Every write goes through the `ket` CLI (and `catbus` for the optional handoff),
so sieve has no pin on ket's crates: it runs against whatever ket is on PATH.
Agents are commands that read a prompt on stdin and print JSON on stdout; the
default is `claude -p --output-format json`, and anything else that speaks the
same protocol works — including the fake reviewer in tests/.

DAG shape (child -> parent, edge kind):

    root (context, agent sieve)                      the audited tip
      <- finding (reasoning, agent audit:<dim>)      proposes
      <- transcript (memory, agent audit:<dim>)      derives
      <- evidence (memory, agent verify:<dim>)       derives
    finding <- verdict (reasoning, verify:<dim>)     confirms | refutes
    evidence <- verdict                              grounds
    finding <- corrected finding                     supersedes   (PARTLY)
    root    <- corrected finding                     proposes
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import os
import shlex
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

CLASSIFICATIONS = ["HONEST", "OVERSTATING", "VACUOUS", "THEATER", "NEUTRAL-FACT"]
SEVERITIES = ["high", "medium", "low", "info"]
VERDICTS = ["CONFIRMED", "REFUTED", "PARTLY"]
# Read-only tools only: an auditor that can edit the repo is not an auditor.
READ_ONLY_TOOLS = ",".join([
    "Read", "Glob", "Grep",
    "Bash(git log:*)", "Bash(git show:*)", "Bash(git diff:*)", "Bash(git status:*)",
    "Bash(git rev-parse:*)", "Bash(git ls-files:*)", "Bash(git blame:*)",
    "Bash(ls:*)", "Bash(cat:*)", "Bash(head:*)", "Bash(tail:*)", "Bash(wc:*)",
    "Bash(grep:*)", "Bash(find:*)", "Bash(sed -n:*)", "Bash(jq:*)",
])
DEFAULT_AGENT = f"claude -p --output-format json --allowedTools {READ_ONLY_TOOLS}"

# ----------------------------------------------------------------------------
# ket / catbus wrappers — every write is a CLI call, so it lands in .ket/log


class Ket:
    def __init__(self, home: str | None):
        self.home = home

    def _cmd(self, *args: str) -> list[str]:
        cmd = ["ket"]
        if self.home:
            cmd += ["--home", self.home]
        return cmd + list(args)

    def json(self, *args: str, stdin: str | None = None) -> dict | list:
        cmd = self._cmd("--json", *args)
        p = subprocess.run(cmd, input=stdin, capture_output=True, text=True)
        if p.returncode != 0:
            raise RuntimeError(f"{' '.join(cmd[:4])}… failed: {p.stderr.strip()}")
        return json.loads(p.stdout)

    def text(self, *args: str) -> str:
        p = subprocess.run(self._cmd(*args), capture_output=True, text=True)
        if p.returncode != 0:
            raise RuntimeError(f"ket {' '.join(args[:2])} failed: {p.stderr.strip()}")
        return p.stdout

    def put(self, content: str) -> str:
        return self.json("put", "-", stdin=content)["cid"]

    def get(self, cid: str) -> str:
        return self.text("get", cid)

    def node(self, content: str, kind: str, agent: str, parents: list[tuple[str, str]]) -> str:
        args = ["dag", "create", content, "--kind", kind, "--agent", agent]
        for cid, edge in parents:
            args += ["--parent", f"{cid}:{edge}"]
        return self.json(*args)["node_cid"]

    def graph(self) -> dict:
        return self.json("graph", "--format", "json")


def canonical(obj) -> str:
    """Same meaning, same bytes: sorted keys, no whitespace noise."""
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


# ----------------------------------------------------------------------------
# agents


def run_agent(command: str, prompt: str, cwd: str | None = None, timeout: int = 1800) -> tuple[dict, str]:
    """Run an agent command with the prompt on stdin. Returns (parsed JSON, raw stdout).

    Accepts bare JSON, or a Claude Code `--output-format json` envelope whose
    `result` field is text containing JSON.
    """
    # Claude Code refuses to start inside another Claude Code session; sieve is
    # often launched from one, and the agent is a separate process by design.
    env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
    p = subprocess.run(
        shlex.split(command), input=prompt, capture_output=True, text=True, cwd=cwd, timeout=timeout, env=env
    )
    raw = p.stdout
    if p.returncode != 0:
        raise RuntimeError(f"agent failed ({p.returncode}): {p.stderr.strip()[:500]}")
    return parse_agent_json(raw), raw


def envelope_cost(raw: str) -> float:
    """Dollars spent, if the agent output is a Claude Code envelope; else 0."""
    try:
        start, end = raw.find("{"), raw.rfind("}")
        obj = json.loads(raw[start : end + 1])
        return float(obj.get("total_cost_usd", 0.0)) if isinstance(obj, dict) else 0.0
    except (ValueError, TypeError):
        return 0.0


def parse_agent_json(raw: str) -> dict:
    def extract(text: str) -> dict:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end < 0:
            raise ValueError("no JSON object in agent output")
        return json.loads(text[start : end + 1])

    obj = extract(raw)
    if isinstance(obj, dict) and isinstance(obj.get("result"), str) and "findings" not in obj and "verdict" not in obj:
        obj = extract(obj["result"])
    return obj


FINDINGS_INSTRUCTIONS = """
Return ONLY a JSON object, no prose, of the form:
{"findings": [{"claim": "...", "evidence": "file:line refs, quotes, command output — concrete",
               "classification": "HONEST|OVERSTATING|VACUOUS|THEATER|NEUTRAL-FACT",
               "severity": "high|medium|low|info"}],
 "summary": "one paragraph"}
Do not modify the repository. Read-only commands only.
"""

VERIFY_INSTRUCTIONS = """
Adversarially verify the finding above against the repository. Try to refute it.
Return ONLY a JSON object: {"verdict": "CONFIRMED|REFUTED|PARTLY",
 "correction": "if PARTLY, the corrected claim; else empty string",
 "evidence": "the exact commands you ran and their output, or file:line quotes"}
Do not modify the repository. Read-only commands only.
"""


def normalize_finding(f: dict) -> dict:
    cls = str(f.get("classification", "NEUTRAL-FACT")).upper()
    sev = str(f.get("severity", "info")).lower()
    return {
        "claim": str(f.get("claim", "")).strip(),
        "evidence": str(f.get("evidence", "")).strip(),
        "classification": cls if cls in CLASSIFICATIONS else "NEUTRAL-FACT",
        "severity": sev if sev in SEVERITIES else "info",
    }


# ----------------------------------------------------------------------------
# run


@dataclass
class Finding:
    cid: str
    dim: str
    body: dict
    verdict: str | None = None
    verdict_cid: str | None = None
    evidence_cid: str | None = None
    corrected_cid: str | None = None


def git(repo: str, *args: str) -> str:
    return subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True).stdout.strip()


def cmd_run(a: argparse.Namespace) -> int:
    ket = Ket(a.ket_home)
    repo = str(Path(a.repo).resolve())
    dims = json.loads(Path(a.dims).read_text())
    context = dims.get("context", "")
    dimensions = dims["dimensions"]
    material = set(a.material.split(","))

    tip = git(repo, "rev-parse", "HEAD")
    tree = git(repo, "rev-parse", "HEAD^{tree}")
    url = git(repo, "remote", "get-url", "origin") or None
    target = {
        "kind": "sieve.audit.v1",
        "repo": os.path.basename(repo),
        "url": url,
        "tip": tip,
        "tree": tree,
        "dimensions": [d["key"] for d in dimensions],
        "started": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    root_parents = [(a.parent, "derives")] if a.parent else []
    root = ket.node(canonical(target), "context", "sieve", root_parents)
    log(f"root {root[:12]}  {target['repo']}@{tip[:12]}  {len(dimensions)} dimensions")

    # fan out: one reviewer per dimension, concurrently
    cost: list[float] = []

    def review(d: dict) -> tuple[str, list[Finding]]:
        prompt = f"{context}\nRepository: {repo}\nDimension: {d['key']}\n{d['prompt']}\n{FINDINGS_INSTRUCTIONS}"
        try:
            out, raw = run_agent(a.agent, prompt, cwd=repo, timeout=a.timeout)
        except Exception as e:  # noqa: BLE001 — a failed reviewer is a finding about the run
            raw = f"reviewer failed: {e}"
            out = {"findings": [], "summary": raw}
        cost.append(envelope_cost(raw))
        ket.node(raw, "memory", f"audit:{d['key']}", [(root, "derives")])
        found = []
        for f in out.get("findings", []):
            body = normalize_finding(f)
            if not body["claim"]:
                continue
            cid = ket.node(canonical(body), "reasoning", f"audit:{d['key']}", [(root, "proposes")])
            found.append(Finding(cid=cid, dim=d["key"], body=body))
        return d["key"], found

    findings: list[Finding] = []
    with cf.ThreadPoolExecutor(max_workers=a.jobs) as ex:
        for key, found in ex.map(review, dimensions):
            log(f"  audit:{key:<20} {len(found)} findings")
            findings.extend(found)

    # fan in: verify material findings, concurrently
    def verify(f: Finding) -> Finding:
        prompt = f"{context}\nRepository: {repo}\nFinding under test:\n{canonical(f.body)}\n{VERIFY_INSTRUCTIONS}"
        try:
            out, raw = run_agent(a.verifier, prompt, cwd=repo, timeout=a.timeout)
            cost.append(envelope_cost(raw))
        except Exception as e:  # noqa: BLE001
            out = {"verdict": "PARTLY", "correction": f.body["claim"], "evidence": f"verifier failed: {e}"}
        verdict = str(out.get("verdict", "")).upper()
        verdict = verdict if verdict in VERDICTS else "PARTLY"
        evidence_text = str(out.get("evidence", "")).strip() or "(no evidence returned)"
        f.evidence_cid = ket.node(evidence_text, "memory", f"verify:{f.dim}", [(root, "derives")])
        claim_cid = f.cid
        if verdict == "PARTLY":
            corrected = dict(f.body, claim=str(out.get("correction", "")).strip() or f.body["claim"])
            f.corrected_cid = ket.node(
                canonical(corrected), "reasoning", f"verify:{f.dim}", [(f.cid, "supersedes"), (root, "proposes")]
            )
            claim_cid, edge = f.corrected_cid, "confirms"
        else:
            edge = "confirms" if verdict == "CONFIRMED" else "refutes"
        f.verdict = verdict
        f.verdict_cid = ket.node(
            canonical({"verdict": verdict, "correction": out.get("correction", "")}),
            "reasoning",
            f"verify:{f.dim}",
            [(claim_cid, edge), (f.evidence_cid, "grounds")],
        )
        return f

    to_verify = [f for f in findings if a.verify == "all" or (a.verify == "material" and f.body["severity"] in material)]
    if to_verify:
        with cf.ThreadPoolExecutor(max_workers=a.jobs) as ex:
            for f in ex.map(verify, to_verify):
                log(f"  verify:{f.dim:<19} {f.verdict:<9} {f.body['claim'][:60]}")

    counts = {v: sum(1 for f in findings if f.verdict == v) for v in VERDICTS}
    summary = {
        "root": root,
        "repo": target["repo"],
        "tip": tip,
        "findings": len(findings),
        "verified": len(to_verify),
        **{v.lower(): n for v, n in counts.items()},
        "cost_usd": round(sum(cost), 4),
    }
    if a.handoff:
        text = (
            f"sieve audit of {target['repo']}@{tip[:12]}: {len(findings)} findings, "
            f"{counts['CONFIRMED']} confirmed, {counts['REFUTED']} refuted, {counts['PARTLY']} partly. "
            f"Ledger: sieve ledger {root}"
        )
        cmd = ["catbus"] + (["--ket-home", a.ket_home] if a.ket_home else []) + [
            "--json", "pack", "--title", f"audit {target['repo']}", "--summary", text,
            "--agent", "sieve", "--parent", root,
        ]
        p = subprocess.run(cmd, capture_output=True, text=True)
        if p.returncode == 0:
            summary["handoff"] = json.loads(p.stdout)["node_cid"]
        else:
            log(f"  catbus pack failed: {p.stderr.strip()}")
    if a.json:
        print(json.dumps(summary, indent=2))
    else:
        print(f"root: {root}")
        print(
            f"{len(findings)} findings · {len(to_verify)} verified · "
            f"{counts['CONFIRMED']} confirmed · {counts['REFUTED']} refuted · {counts['PARTLY']} partly"
        )
        if "handoff" in summary:
            print(f"handoff: {summary['handoff']}")
        if summary["cost_usd"]:
            print(f"cost: ${summary['cost_usd']:.2f}")
        print(f"ledger: sieve ledger {root}")
    return 0


def log(msg: str) -> None:
    print(msg, file=sys.stderr)


# ----------------------------------------------------------------------------
# ledger: the DAG rendered as prose


@dataclass
class Audit:
    root: str
    target: dict
    nodes: dict[str, dict]
    edges: list[dict]
    findings: list[dict] = field(default_factory=list)


def load_audit(ket: Ket, root: str) -> Audit:
    g = ket.graph()
    nodes = {n["cid"]: n for n in g["nodes"]}
    if root not in nodes:
        raise SystemExit(f"no node {root[:12]} in this store")
    # descendants of root: nodes that reach root by following child->parent edges
    children: dict[str, list[str]] = {}
    for e in g["edges"]:
        children.setdefault(e["parent"], []).append(e["child"])
    keep, stack = {root}, [root]
    while stack:
        for c in children.get(stack.pop(), []):
            if c not in keep:
                keep.add(c)
                stack.append(c)
    edges = [e for e in g["edges"] if e["child"] in keep and e["parent"] in keep]
    sub = {cid: nodes[cid] for cid in keep}
    target = json.loads(ket.get(node_output(ket, root)))
    a = Audit(root=root, target=target, nodes=sub, edges=edges)

    by_child: dict[str, list[dict]] = {}
    for e in edges:
        by_child.setdefault(e["child"], []).append(e)
    superseded = {e["parent"] for e in edges if e["kind"] == "supersedes"}
    for cid, n in sub.items():
        if n["kind"] != "reasoning" or not n["agent"].startswith(("audit:", "verify:")):
            continue
        parent_kinds = {e["kind"] for e in by_child.get(cid, [])}
        if "proposes" not in parent_kinds:
            continue  # verdict nodes have confirms/refutes + grounds, not proposes
        body = json.loads(ket.get(node_output(ket, cid)))
        verdict_edges = [e for e in edges if e["parent"] == cid and e["kind"] in ("confirms", "refutes")]
        verdict = None
        evidence = None
        if verdict_edges:
            v = verdict_edges[0]
            verdict = "CONFIRMED" if v["kind"] == "confirms" else "REFUTED"
            for e in edges:
                if e["child"] == v["child"] and e["kind"] == "grounds":
                    evidence = e["parent"]
        supersedes = [e["parent"] for e in by_child.get(cid, []) if e["kind"] == "supersedes"]
        a.findings.append(
            {
                "cid": cid,
                "dim": n["agent"].split(":", 1)[1],
                "agent": n["agent"],
                "timestamp": n["timestamp"],
                **body,
                "verdict": verdict,
                "evidence_cid": evidence,
                "superseded": cid in superseded,
                "supersedes": supersedes[0] if supersedes else None,
            }
        )
    a.findings.sort(key=lambda f: (f["dim"], SEVERITIES.index(f["severity"]), f["claim"]))
    return a


def node_output(ket: Ket, cid: str) -> str:
    return ket.json("dag", "show", cid)["output_cid"]


def render_mermaid(a: Audit) -> str:
    def sid(cid: str) -> str:
        return "n" + cid[:12]

    arrows = {
        "grounds": "==>|grounds|", "proposes": "-.->|proposes|", "confirms": "-->|confirms|",
        "refutes": "--x|refutes|", "supersedes": "--o|supersedes|", "derives": "-->",
    }
    colors = {"memory": "#E8F5E9", "reasoning": "#FFF3E0", "context": "#F1F8E9"}
    out = ["graph BT"]
    for cid, n in sorted(a.nodes.items(), key=lambda kv: (kv[1]["timestamp"], kv[0])):
        label = n["label"].replace('"', "#quot;")
        out.append(f'  {sid(cid)}["{cid[:12]}<br/>{n["kind"]} · {n["agent"]}<br/>{label}"]')
        out.append(f"  class {sid(cid)} {n['kind']}")
    for e in a.edges:
        out.append(f"  {sid(e['child'])} {arrows.get(e['kind'], '-->')} {sid(e['parent'])}")
    for k, c in colors.items():
        out.append(f"  classDef {k} fill:{c},stroke:#555,color:#111")
    return "\n".join(out) + "\n"


def render_ledger(ket: Ket, a: Audit) -> str:
    t = a.target
    live = [f for f in a.findings if not f["superseded"]]
    n_conf = sum(1 for f in live if f["verdict"] == "CONFIRMED")
    n_ref = sum(1 for f in live if f["verdict"] == "REFUTED")
    n_unv = sum(1 for f in live if f["verdict"] is None)
    lines = [
        f"# Audit ledger — {t['repo']} @ `{t['tip'][:12]}`",
        "",
        f"Root `{a.root}` · started {t['started']} · dimensions: {', '.join(t['dimensions'])}",
        "",
        f"**{len(live)} findings** · {n_conf} confirmed · {n_ref} refuted · {n_unv} unverified"
        + (f" · {len(a.findings) - len(live)} superseded by corrections" if len(a.findings) != len(live) else ""),
        "",
        "Every row below is a content-addressed node in the audit DAG. A verdict is a",
        "typed edge from a verification node that is itself grounded in captured",
        "evidence. Nothing here was edited after the fact: corrections are new nodes",
        "that supersede the originals, and the originals stay addressable.",
        "",
    ]
    for dim in t["dimensions"]:
        rows = [f for f in live if f["dim"] == dim]
        lines += [f"## {dim}", ""]
        if not rows:
            lines += ["_no findings_", ""]
            continue
        for i, f in enumerate(rows, 1):
            verdict = f["verdict"] or "unverified"
            lines.append(f"{i}. **{f['classification']}/{f['severity']}** ({verdict}) — {f['claim']}")
            lines.append(f"   - evidence: {f['evidence']}")
            if f["supersedes"]:
                lines.append(f"   - supersedes `{f['supersedes'][:12]}`")
            if f["evidence_cid"]:
                ev = ket.get(node_output(ket, f["evidence_cid"])).strip().splitlines()
                shown = "\n".join("     " + l for l in ev[:8])
                more = f"\n     … ({len(ev) - 8} more lines, `ket get {f['evidence_cid'][:12]}…`)" if len(ev) > 8 else ""
                lines.append(f"   - verification evidence `{f['evidence_cid'][:12]}`:\n\n{shown}{more}\n")
            lines.append(f"   - node `{f['cid']}`")
            lines.append("")
    lines += ["## Graph", "", "```mermaid", render_mermaid(a).rstrip(), "```", ""]
    return "\n".join(lines)


def cmd_ledger(a: argparse.Namespace) -> int:
    ket = Ket(a.ket_home)
    audit = load_audit(ket, a.root)
    if a.format == "json":
        print(json.dumps({"root": audit.root, "target": audit.target, "findings": audit.findings}, indent=2))
    elif a.format == "mermaid":
        print(render_mermaid(audit), end="")
    else:
        print(render_ledger(ket, audit), end="")
    return 0


# ----------------------------------------------------------------------------
# diff: two audits of (usually) two tips


def cmd_diff(a: argparse.Namespace) -> int:
    ket = Ket(a.ket_home)
    left, right = load_audit(ket, a.left), load_audit(ket, a.right)

    def index(audit: Audit) -> dict[str, dict]:
        return {canonical({k: f[k] for k in ("claim", "evidence", "classification", "severity")}): f
                for f in audit.findings if not f["superseded"]}

    li, ri = index(left), index(right)
    added = [ri[k] for k in ri.keys() - li.keys()]
    removed = [li[k] for k in li.keys() - ri.keys()]
    changed_verdict = [(li[k], ri[k]) for k in li.keys() & ri.keys() if li[k]["verdict"] != ri[k]["verdict"]]
    out = {
        "left": {"root": left.root, "tip": left.target["tip"]},
        "right": {"root": right.root, "tip": right.target["tip"]},
        "same_tip": left.target["tip"] == right.target["tip"],
        "added": [f["claim"] for f in added],
        "removed": [f["claim"] for f in removed],
        "verdict_changed": [{"claim": l["claim"], "from": l["verdict"], "to": r["verdict"]} for l, r in changed_verdict],
        "unchanged": len(li.keys() & ri.keys()) - len(changed_verdict),
    }
    if a.json:
        print(json.dumps(out, indent=2))
        return 0
    print(f"{left.target['repo']}: {left.target['tip'][:12]} -> {right.target['tip'][:12]}"
          + ("  (same tip)" if out["same_tip"] else ""))
    for label, items in (("added", out["added"]), ("removed", out["removed"])):
        if items:
            print(f"{label} ({len(items)}):")
            for c in items:
                print(f"  {'+' if label == 'added' else '-'} {c}")
    if out["verdict_changed"]:
        print(f"verdict changed ({len(out['verdict_changed'])}):")
        for v in out["verdict_changed"]:
            print(f"  ~ {v['claim']}  {v['from']} -> {v['to']}")
    print(f"unchanged: {out['unchanged']}")
    return 0


# ----------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="sieve", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--ket-home", default=os.environ.get("KET_HOME"), help="ket store (env KET_HOME; default ./.ket)")
    p.add_argument("--json", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="audit a repository into the DAG")
    r.add_argument("repo")
    r.add_argument("--dims", required=True, help="JSON: {context, dimensions:[{key, prompt}]}")
    r.add_argument("--agent", default=DEFAULT_AGENT, help="reviewer command; prompt on stdin, JSON on stdout")
    r.add_argument("--verifier", default=None, help="verifier command (default: same as --agent)")
    r.add_argument("--jobs", type=int, default=4, help="concurrent agents")
    r.add_argument("--verify", choices=["material", "all", "none"], default="material")
    r.add_argument("--material", default="high,medium", help="severities that count as material")
    r.add_argument("--parent", default=None, help="root CID of a previous audit to chain from")
    r.add_argument("--handoff", action="store_true", help="also pack a catbus handoff pointing at the root")
    r.add_argument("--timeout", type=int, default=1800, help="seconds per agent")
    r.set_defaults(fn=cmd_run)

    l = sub.add_parser("ledger", help="render an audit as LEDGER.md (or json / mermaid)")
    l.add_argument("root")
    l.add_argument("--format", choices=["md", "json", "mermaid"], default="md")
    l.set_defaults(fn=cmd_ledger)

    d = sub.add_parser("diff", help="compare two audits")
    d.add_argument("left")
    d.add_argument("right")
    d.set_defaults(fn=cmd_diff)

    a = p.parse_args(argv)
    if a.cmd == "run" and a.verifier is None:
        a.verifier = a.agent
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
