# catbus audited by sieve, 2026-09-05

The first real run. Two dimensions of `dims/repo-honesty.json` (`readme-vs-code`,
`ci-and-badges`) against [catbus](https://github.com/nickjoven/catbus) at
`d6540d5f06e7`, with Claude as reviewer and verifier through the default
read-only agent command. Material findings (high/medium) were verified.

| | |
|---|---|
| findings | 17 |
| verified | 2 (1 confirmed, 1 partly → corrected) |
| cost | $3.64 |
| wall time | ~11 min |

`.ket/` is the store the run wrote: every finding, transcript, evidence blob and
verdict, plus the append-only log. `LEDGER.md` and `graph.mmd` are rendered from
it. Re-render at any time:

```sh
KET_HOME=$PWD/.ket python3 ../../sieve.py ledger d499407b5f97532e30208acafed2ce66825a6c09a8a48c57eb3633c379ec48c9
KET_HOME=$PWD/.ket ket verify   <any cid>          # bytes still match their name
```

What it found that mattered, and what happened next:

- **`catbus guard` never handed the handoff to the agent it wrapped.** The block
  went to the wrapper's stdout and the agent was exec'd with nothing. Confirmed.
  Fixed the same day: guard now exports `CATBUS_CID` and `CATBUS_HANDOFF` to the
  child and writes the block to `CATBUS_HANDOFF_FILE`.
- **`catbus validate` with no flags could not fail on any packet `pack` produces.**
  Reviewer said VACUOUS; verifier corrected it to OVERSTATING and confirmed the
  substance. `catbus-guard.sh` now validates with `--require-artifacts`.
- **The README's Cargo snippet was stale**: no tag pin, wrong URL form. Fixed.

Fifteen low/info findings were left unverified by the `--verify material`
policy; they are in the ledger with their evidence, addressable by CID.
