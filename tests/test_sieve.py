"""End-to-end: fake agents, a throwaway git repo, a throwaway ket store.

Asserts the DAG shape by kind, the ledger's content, and the projection audit
when Dolt is present. Needs `ket` (and `catbus` for the handoff test) on PATH.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SIEVE = [sys.executable, str(HERE.parent / "sieve.py")]
FAKE = f"{sys.executable} {HERE / 'fake_agent.py'}"


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

    def graph(self):
        return json.loads(sh("ket", "--json", "graph", "--format", "json", env=self.env).stdout)

    def test_summary_counts(self):
        s = self.summary
        # docs:2 + security:2 + tests:1 = 5 findings; one claim is shared between
        # docs and security — two nodes, one content blob (dedup is the point).
        self.assertEqual(s["findings"], 5)
        self.assertEqual(s["verified"], 4)  # 4 material (high/medium)
        self.assertEqual((s["confirmed"], s["refuted"], s["partly"]), (2, 1, 1))

    def test_dag_shape_by_edge_kind(self):
        g = self.graph()
        kinds = {}
        for e in g["edges"]:
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
        self.assertIn("**VACUOUS/low** (CONFIRMED) — No tests assert anything (in src/ only; tests/ is covered)", md,
                      "corrected claim and corrected severity are what the ledger shows")
        self.assertIn("superseded by corrections", md)
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

    @unittest.skipUnless(has_dolt(), "needs dolt")
    def test_projection_is_clean(self):
        sh("ket", "repair", env=self.env)
        p = sh("ket", "verify-projection", env=self.env, check=False)
        self.assertEqual(p.returncode, 0, p.stdout)

    @unittest.skipUnless(shutil.which("catbus"), "needs catbus")
    def test_handoff(self):
        p = sh(*SIEVE, "--json", "run", str(self.repo), "--dims", str(HERE / "dims.json"),
               "--agent", FAKE, "--verify", "none", "--handoff", env=self.env)
        s = json.loads(p.stdout)
        self.assertIn("handoff", s)
        block = sh("catbus", "handoff", s["handoff"], env=self.env).stdout
        self.assertIn("sieve audit of repo@", block)
        self.assertIn(f"- {s['root']}", block, "handoff lists the audit root as its parent")


if __name__ == "__main__":
    unittest.main()
