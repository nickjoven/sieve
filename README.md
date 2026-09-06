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
finding  <- evidence (memory, verify:<dim>)      derives      verifier failed: no verdict edge at all
```

Three properties fall out of this shape:

- **Identical findings dedup.** Finding content is canonical JSON of `{claim, evidence, classification, severity}`. Two reviewers who reach the same finding produce two nodes pointing at one blob. `sieve diff` compares by content, so the same claim counts once.
- **Corrections never overwrite.** A PARTLY verdict writes a corrected finding that `supersedes` the original. The original stays addressable and stays in the graph; the ledger shows the correction and marks the original superseded.
- **Every verdict is grounded.** A verdict node has two parents: the claim it confirms or refutes, and the evidence blob it rests on. `ket verify-projection` checks the whole thing rebuilds.
- **A failed verification is not a verdict.** If the verifier crashes, times out, or answers with something that is not a verdict, sieve stores what it did say as evidence deriving from the finding and writes no verdict edge. The ledger shows `VERIFIER ERROR`, the summary counts it under `error`, and nothing reads as confirmed that was never checked.

## Agent protocol

An agent is any command that reads a prompt on stdin and prints JSON on stdout. sieve accepts bare JSON or a Claude Code result envelope whose `result` text contains JSON. Content reaches ket over stdin, so an answer may be any size and may start with a dash.

The default agent is `claude -p --output-format json` with:

- `--setting-sources user --strict-mcp-config`: the audited repository is data. Its `.claude/settings.json` (hooks, permission grants) and `.mcp.json` do not load. Its `CLAUDE.md` still auto-loads; pass your own `--agent "claude -p --bare ..."` to skip that as well (`--bare` also restricts auth to `ANTHROPIC_API_KEY`).
- `--allowedTools`: `Read`, `Glob`, `Grep`, and git subcommands in their read-only spellings (`git log`, `git show`, `git diff`, `git grep`, `git branch` without a target, ...), plus `ls` and `wc`. `--allowedTools` pre-approves; anything else prompts, and in `-p` mode a prompt is a denial.
- `--disallowedTools`: the write tools, `WebFetch`, `WebSearch`, `curl`, `wget`, `git push`, `git commit`.

`Read` is not scoped to the repository, so a prompt injection in the audited code could still read files the auditor can read. Audit repositories you do not trust in a container with nothing else in it.

Each agent call has its own process group; `--timeout` kills the agent and everything it spawned.

Reviewer output: `{"findings": [{"claim", "evidence", "classification", "severity"}], "summary"}`
Verifier output: `{"verdict": "CONFIRMED|REFUTED|PARTLY", "correction", "evidence"}`

Output that does not fit is kept as the transcript, marked failed, and the run continues; it is never turned into a finding or a verdict.

`tests/fake_agent.py` is a deterministic reviewer and verifier; the end-to-end test runs the whole pipeline against it in about thirty seconds with no API calls.

```sh
python3 -m unittest -v        # from the repo root; needs ket (and catbus, for the handoff test) on PATH
```

## Why ket underneath

sieve writes only through the `ket` CLI, so it has no pin on ket's crates and runs against whatever ket is on PATH. Everything it produces passes ket's design test: throw away the SQL projection and `ket rebuild-projection` recovers it from the blobs and the log. The audit of an audit is `ket verify-projection`.

MIT.
