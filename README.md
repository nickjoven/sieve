# sieve

**Purpose:** audit a repository into a [ket](https://github.com/nickjoven/ket) DAG. Findings are content-addressed claims. Verification is a typed edge. The ledger is a rendering. Re-auditing is a diff.

sieve generalizes the method behind [crouzeix-audit](https://github.com/nickjoven/crouzeix-audit): fan out independent reviewers by dimension, fan in adversarial verifiers, keep the evidence. The difference is where the result lives. There, the ledger was a hand-maintained Markdown file. Here, every finding, every verdict, and every piece of evidence is a node in a content-addressed graph, and the ledger is generated from it.

## Quickstart

```sh
# needs: python3, ket on PATH (and claude, or any agent speaking the protocol below)
ket init
python3 sieve.py run ~/code/some-repo --dims dims/repo-honesty.json --jobs 6
python3 sieve.py ledger <root-cid> > LEDGER.md
python3 sieve.py ledger <root-cid> --format mermaid     # just the graph
```

Re-audit after the repo changes, chained to the first run, and diff:

```sh
python3 sieve.py run ~/code/some-repo --dims dims/repo-honesty.json --parent <root-cid>
python3 sieve.py diff <root-cid> <new-root-cid>
```

Add `--handoff` to `run` and sieve packs a [catbus](https://github.com/nickjoven/catbus) handoff whose parent is the audit root, so the next agent starts from the ledger without being told anything.

## The DAG

```
root (context, agent sieve)                      the audited tip: repo, sha, tree, dimensions
  <- finding    (reasoning, audit:<dim>)         proposes
  <- transcript (memory,    audit:<dim>)         derives      the reviewer's raw output
  <- evidence   (memory,    verify:<dim>)        derives      commands run, output captured
finding  <- verdict (reasoning, verify:<dim>)    confirms | refutes
evidence <- verdict                              grounds
finding  <- corrected finding                    supersedes   (verdict PARTLY)
root     <- corrected finding                    proposes
```

Three properties fall out of this shape:

- **Identical findings dedup.** Finding content is canonical JSON of `{claim, evidence, classification, severity}`. Two reviewers who reach the same finding produce two nodes pointing at one blob. `sieve diff` compares by content, so the same claim counts once.
- **Corrections never overwrite.** A PARTLY verdict writes a corrected finding that `supersedes` the original. The original stays addressable and stays in the graph; the ledger shows the correction and marks the original superseded.
- **Every verdict is grounded.** A verdict node has two parents: the claim it confirms or refutes, and the evidence blob it rests on. `ket verify-projection` checks the whole thing rebuilds.

## Agent protocol

An agent is any command that reads a prompt on stdin and prints JSON on stdout. Default: `claude -p --output-format json`. sieve accepts bare JSON or a Claude Code result envelope whose `result` text contains JSON.

Reviewer output: `{"findings": [{"claim", "evidence", "classification", "severity"}], "summary"}`
Verifier output: `{"verdict": "CONFIRMED|REFUTED|PARTLY", "correction", "evidence"}`

`tests/fake_agent.py` is a deterministic reviewer and verifier; the end-to-end test runs the whole pipeline against it in about thirty seconds with no API calls.

```sh
PATH=$PATH python3 -m unittest -v
```

## Why ket underneath

sieve writes only through the `ket` CLI, so it has no pin on ket's crates and runs against whatever ket is on PATH. Everything it produces passes ket's design test: throw away the SQL projection and `ket rebuild-projection` recovers it from the blobs and the log. The audit of an audit is `ket verify-projection`.

MIT.
