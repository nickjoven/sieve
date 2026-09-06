"""End-to-end: fake agents, a throwaway git repo, a throwaway ket store.

Asserts the DAG shape by kind, the ledger's content, and the projection audit
when Dolt is present. Needs `ket` (and `catbus` for the handoff test) on PATH.
"""
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SIEVE = [sys.executable, str(HERE.parent / "sieve.py")]
FAKE = f"{sys.executable} {HERE / 'fake_agent.py'}"

sys.path.insert(0, str(HERE.parent))
import sieve  # noqa: E402


def sh(*cmd, cwd=None, env=None, check=True):
    p = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True)
    if check and p.returncode != 0:
        raise AssertionError(f"{cmd[:3]} failed:\n{p.stdout}\n{p.stderr}")
    return p


def has_dolt():
    return shutil.which("dolt") is not None


class SieveEndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="sieve-test-"))
        cls.repo = cls.tmp / "repo"
        cls.repo.mkdir()
        (cls.repo / "README.md").write_text("# demo\n\nA tiny repo.\nCI on every push.\n")
        (cls.repo / "config.example").write_text("region=us-east-1\nkey=AKIA_PLACEHOLDER\n")
        sh("git", "init", "-q", cwd=cls.repo)
        sh("git", "-c", "user.name=t", "-c", "user.email=t@t", "add", ".", cwd=cls.repo)
        sh("git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init", cwd=cls.repo)
        cls.env = dict(os.environ, KET_HOME=str(cls.tmp / ".ket"))
        sh("ket", "init", env=cls.env)
        p = sh(*SIEVE, "--json", "run", str(cls.repo), "--dims", str(HERE / "dims.json"),
               "--agent", FAKE, "--jobs", "3", env=cls.env)
        cls.summary = json.loads(p.stdout)
        cls.root = cls.summary["root"]

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def graph(self, env=None):
        return json.loads(sh("ket", "--json", "graph", "--format", "json", env=env or self.env).stdout)

    def audit_edges(self, root, env=None):
        """Edges of one audit: descendants of root, stopping at other context nodes
        (a chained audit's root, a handoff packet). Other tests write more audits
        into the same store, so whole-store counts would depend on test order."""
        g = self.graph(env)
        kind = {n["cid"]: n["kind"] for n in g["nodes"]}
        children = {}
        for e in g["edges"]:
            children.setdefault(e["parent"], []).append(e["child"])
        keep, stack = {root}, [root]
        while stack:
            for c in children.get(stack.pop(), []):
                if c not in keep and kind[c] != "context":
                    keep.add(c)
                    stack.append(c)
        return [e for e in g["edges"] if e["child"] in keep and e["parent"] in keep]

    def fresh_store(self, name):
        env = dict(os.environ, KET_HOME=str(self.tmp / f"{name}.ket"))
        sh("ket", "init", env=env)
        return env

    def test_summary_counts(self):
        s = self.summary
        # docs:2 + security:2 + tests:1 = 5 findings; one claim is shared between
        # docs and security — two nodes, one content blob (dedup is the point).
        self.assertEqual(s["findings"], 5)
        self.assertEqual(s["verified"], 4)  # 4 material (high/medium)
        self.assertEqual((s["confirmed"], s["refuted"], s["partly"], s["error"]), (2, 1, 1, 0))

    def test_dag_shape_by_edge_kind(self):
        kinds = {}
        for e in self.audit_edges(self.root):
            kinds[e["kind"]] = kinds.get(e["kind"], 0) + 1
        # 5 findings + 1 corrected finding propose against root
        self.assertEqual(kinds["proposes"], 6)
        # 2 confirmed + 1 confirmed-after-correction
        self.assertEqual(kinds["confirms"], 3)
        self.assertEqual(kinds["refutes"], 1)
        self.assertEqual(kinds["supersedes"], 1)
        self.assertEqual(kinds["grounds"], 4)  # every verdict grounded in evidence
        # 3 transcripts + 4 evidence nodes derive from root
        self.assertEqual(kinds["derives"], 7)

    def test_identical_findings_share_one_blob(self):
        j = json.loads(sh(*SIEVE, "ledger", self.root, "--format", "json", env=self.env).stdout)
        shared = [f for f in j["findings"] if f["claim"].startswith("README promises CI")]
        self.assertEqual(sorted(f["dim"] for f in shared), ["docs", "security"])
        outs = {json.loads(sh("ket", "--json", "dag", "show", f["cid"], env=self.env).stdout)["output_cid"]
                for f in shared}
        self.assertEqual(len(outs), 1, "two nodes, one content blob: same bytes, same CID")

    def test_ledger_renders_from_the_dag(self):
        md = sh(*SIEVE, "ledger", self.root, env=self.env).stdout
        self.assertIn("# Audit ledger — repo @", md)
        self.assertIn("**OVERSTATING/high** (CONFIRMED)", md)
        self.assertIn("**THEATER/high** (REFUTED)", md)
        self.assertIn("**VACUOUS/low** (CORRECTED) — No tests assert anything (in src/ only; tests/ is covered)", md,
                      "corrected claim and corrected severity are what the ledger shows")
        self.assertIn("**5 findings** · 2 confirmed · 1 corrected · 1 refuted · 1 unverified · 1 superseded by corrections", md,
                      "the header counts agree with the run summary: a correction is not a plain confirmation")
        self.assertIn("ls: cannot access '.github/workflows'", md, "evidence is quoted from its blob")
        self.assertIn("```mermaid", md)
        self.assertIn("--x|refutes|", md)
        self.assertIn("--o|supersedes|", md)
        j = json.loads(sh(*SIEVE, "ledger", self.root, "--format", "json", env=self.env).stdout)
        live = [f for f in j["findings"] if not f["superseded"]]
        self.assertEqual(len(live), 5)

    def test_rerun_chains_and_diffs(self):
        # Change the repo so the "no CI" finding would go away, re-audit with a
        # reviewer whose docs findings drop it, chain from the first root.
        (self.repo / ".github" / "workflows").mkdir(parents=True, exist_ok=True)
        (self.repo / ".github" / "workflows" / "ci.yml").write_text("on: push\n")
        sh("git", "-c", "user.name=t", "-c", "user.email=t@t", "add", ".", cwd=self.repo)
        sh("git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "ci", cwd=self.repo)
        dims2 = self.tmp / "dims2.json"
        dims2.write_text(json.dumps({"context": "", "dimensions": [{"key": "tests", "prompt": "x"}]}))
        p = sh(*SIEVE, "--json", "run", str(self.repo), "--dims", str(dims2), "--agent", FAKE,
               "--parent", self.root, env=self.env)
        root2 = json.loads(p.stdout)["root"]
        lineage = sh("ket", "dag", "lineage", root2, env=self.env).stdout
        self.assertIn(self.root[:12], lineage, "second audit points at the first")
        d = json.loads(sh(*SIEVE, "--json", "diff", self.root, root2, env=self.env).stdout)
        self.assertFalse(d["same_tip"])
        self.assertEqual(d["added"], [])
        # diff is by content: the claim docs and security both made counts once
        self.assertEqual(len(d["removed"]), 3, "docs + security findings gone; tests finding stays")
        self.assertEqual(d["unchanged"], 1)
        # The first audit's ledger is unchanged by the chained one: the walk
        # from root1 stops at root2 instead of absorbing its findings.
        j = json.loads(sh(*SIEVE, "ledger", self.root, "--format", "json", env=self.env).stdout)
        self.assertEqual(len([f for f in j["findings"] if not f["superseded"]]), 5)
        mmd = sh(*SIEVE, "ledger", self.root, "--format", "mermaid", env=self.env).stdout
        self.assertNotIn(root2[:12], mmd)
        # And the reverse diff does report the second audit's extra finding as removed only once each way.
        d2 = json.loads(sh(*SIEVE, "--json", "diff", root2, self.root, env=self.env).stdout)
        self.assertEqual(len(d2["added"]), 3)

    def test_verifier_failure_is_an_error_not_a_verdict(self):
        env = self.fresh_store("verr")
        p = sh(*SIEVE, "--json", "run", str(self.repo), "--dims", str(HERE / "dims.json"), "--agent", FAKE,
               "--verifier", f"{sys.executable} -c 'import sys; sys.exit(1)'", env=env)
        s = json.loads(p.stdout)
        self.assertEqual((s["confirmed"], s["refuted"], s["partly"], s["error"]), (0, 0, 0, 4))
        kinds = {e["kind"] for e in self.audit_edges(s["root"], env)}
        self.assertFalse(kinds & {"confirms", "refutes", "supersedes"}, "no verdict edges from a failed verifier")
        md = sh(*SIEVE, "ledger", s["root"], env=env).stdout
        self.assertIn("· 0 confirmed · 0 refuted · 1 unverified · 4 verifier errors", md)
        self.assertEqual(md.count("(VERIFIER ERROR)"), 4)
        self.assertIn("verifier failed: agent failed (1)", md)

    def test_bad_reviewer_output_is_recorded_not_fatal(self):
        env = self.fresh_store("badrev")
        bad = self.tmp / "bad_agent.py"
        bad.write_text('import sys\nprint(\'{"findings": ["x", 3], "summary": "s"}\')\n')
        p = sh(*SIEVE, "--json", "run", str(self.repo), "--dims", str(HERE / "dims.json"),
               "--agent", f"{sys.executable} {bad}", env=env)
        self.assertEqual(json.loads(p.stdout)["findings"], 0)
        g = self.graph(env)
        transcripts = [n for n in g["nodes"] if n["agent"].startswith("audit:")]
        self.assertEqual(len(transcripts), 3, "one transcript per dimension even when the reviewer misbehaves")
        blob = sh("ket", "get", json.loads(sh("ket", "--json", "dag", "show", transcripts[0]["cid"], env=env).stdout)["output_cid"], env=env).stdout
        self.assertIn("reviewer failed:", blob)
        self.assertIn('"findings": ["x", 3]', blob, "the raw output is kept with the failure")

    def test_cost_counted_once_on_unrecognized_verdict(self):
        # D4: a verifier that returns a well-formed envelope (carrying a cost)
        # but a verdict sieve does not recognize must contribute its cost once,
        # not twice. Reviewers here emit no envelope, so every dollar in the
        # summary is a verifier's, one per material finding.
        env = self.fresh_store("costonce")
        vfy = self.tmp / "cost_verifier.py"
        vfy.write_text(
            "import sys, json\n"
            "sys.stdin.read()\n"
            "print(json.dumps({'type': 'result', 'total_cost_usd': 0.01,\n"
            "    'result': 'my verdict: ' + json.dumps({'verdict': 'MAYBE', 'evidence': 'e'})}))\n"
        )
        p = sh(*SIEVE, "--json", "run", str(self.repo), "--dims", str(HERE / "dims.json"),
               "--agent", FAKE, "--verifier", f"{sys.executable} {vfy}", env=env)
        s = json.loads(p.stdout)
        # every material finding hit the unrecognized-verdict path
        self.assertEqual((s["confirmed"], s["refuted"], s["partly"], s["error"]),
                         (0, 0, 0, s["verified"]))
        self.assertEqual(s["cost_usd"], round(0.01 * s["verified"], 4),
                         "each verifier call's envelope cost is counted exactly once")

    def test_timeout_kills_the_agent_and_the_run_continues(self):
        env = self.fresh_store("timeout")
        p = sh(*SIEVE, "--json", "run", str(self.repo), "--dims", str(HERE / "dims.json"),
               "--agent", f"{sys.executable} -c 'import time; time.sleep(30)'", "--timeout", "1", env=env)
        self.assertEqual(json.loads(p.stdout)["findings"], 0)

    def test_run_refuses_a_non_repo_and_a_bad_parent(self):
        env = self.fresh_store("refuse")
        p = sh(*SIEVE, "run", str(self.tmp), "--dims", str(HERE / "dims.json"), "--agent", FAKE, env=env, check=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("git rev-parse failed", p.stderr)
        for parent in ("deadbeef", "0" * 64):
            p = sh(*SIEVE, "run", str(self.repo), "--dims", str(HERE / "dims.json"), "--agent", FAKE,
                   "--parent", parent, env=env, check=False)
            self.assertNotEqual(p.returncode, 0, parent)
            self.assertIn("--parent", p.stderr)

    @unittest.skipUnless(has_dolt(), "needs dolt")
    def test_projection_is_clean(self):
        sh("ket", "repair", env=self.env)
        p = sh("ket", "verify-projection", env=self.env, check=False)
        self.assertEqual(p.returncode, 0, p.stdout)

    @unittest.skipUnless(shutil.which("catbus"), "needs catbus")
    def test_handoff(self):
        env = self.fresh_store("handoff")
        p = sh(*SIEVE, "--json", "run", str(self.repo), "--dims", str(HERE / "dims.json"),
               "--agent", FAKE, "--verify", "none", "--handoff", env=env)
        s = json.loads(p.stdout)
        self.assertIn("handoff", s)
        block = sh("catbus", "handoff", s["handoff"], env=env).stdout
        self.assertIn("sieve audit of repo@", block)
        self.assertIn(f"- {s['root']}", block, "handoff lists the audit root as its parent")


class DefaultAgentTokenization(unittest.TestCase):
    """D7: DEFAULT_AGENT must survive shlex.split — the value the code hands to
    subprocess. A tool spec with spaces (e.g. Bash(git branch -v)) may not be
    shattered, or claude sees a bare -v, prints its version and exits 0, and
    every reviewer silently 'passes' with no findings."""

    def argv(self):
        return shlex.split(sieve.DEFAULT_AGENT)

    def _value_after(self, flag):
        argv = self.argv()
        self.assertIn(flag, argv, f"{flag} present in DEFAULT_AGENT")
        return argv[argv.index(flag) + 1]

    def test_allowed_tools_round_trips(self):
        self.assertEqual(self._value_after("--allowedTools"), sieve.READ_ONLY_TOOLS,
                         "--allowedTools reconstructs to the intended comma-joined list")

    def test_disallowed_tools_round_trips(self):
        self.assertEqual(self._value_after("--disallowedTools"), sieve.DENIED_TOOLS,
                         "--disallowedTools reconstructs to the intended comma-joined list")

    def test_no_tool_spec_is_shattered(self):
        argv = self.argv()
        # The classic breakage: Bash(git branch -v) split into pieces so claude
        # sees a bare version flag. None of these fragments may be an argv element.
        for fragment in ("-v", "-v)", "branch", "-a", "-r", "Bash(git"):
            self.assertNotIn(fragment, argv, f"{fragment!r} is a shattered tool spec")

    def test_space_bearing_specs_appear_whole(self):
        allowed = self._value_after("--allowedTools")
        specs = allowed.split(",")
        for spec in ("Bash(git branch -v)", "Bash(git status)", "Bash(git remote -v)"):
            self.assertIn(spec, specs, f"{spec!r} present whole in the allowed-tools list")


if __name__ == "__main__":
    unittest.main()
