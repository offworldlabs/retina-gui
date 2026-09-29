# CLAUDE.md - retina-gui

## Documentation is part of every change

retina-gui's design and behaviour are documented in [docs/](docs/README.md), not in long
docstrings. **Every change, by any session or agent, must check the docs and update them in
the same change.** A change is not finished while the docs describe the old behaviour.

Before you finish any change:

1. Find the docs that cover the files you touched. [docs/README.md](docs/README.md) maps each
   source file to its doc.
2. Update any statement the change makes wrong: behaviour, flows, file paths, config keys,
   limits, timeouts, cross-repo couplings.
3. If you add a feature or module, add it to an existing doc or write a new one under
   `docs/features/`, and add it to the map in `docs/README.md`.
4. If you rename or delete a doc heading, update every `See docs/...#anchor` pointer that uses it
   (`grep -rn "docs/" src static templates tests`).
5. Say in your summary (and PR description) which docs you updated, or why none needed to change.

## What goes in code and what goes in docs

In code, keep it short:
- A one-line docstring summary, and non-obvious arguments, return values and exceptions.
- Invariants the next editor would break without seeing them right there (ordering, locking,
  safety limits, values that must match another file or repo). One to three lines, plus a pointer.
- Short comments explaining a non-obvious line.

In `docs/`:
- How a feature works end to end, design reasoning, rejected alternatives.
- History and incident lessons, hardware and protocol background.

Pointers from code to docs use the doc path and a heading anchor, for example
`See docs/features/auto-calibrate.md#safe-end-first.` Docs refer to code by file and
function name, never by line number.

`docs/history/` holds the original pre-implementation plans. It is an archive: do not update it
to match current behaviour, write current behaviour in `docs/features/` or `docs/architecture.md`.

## Branches, commits and pull requests

### Branches

- Name: `YYYYMMDD-short-kebab-summary`, dated the day the branch is created, e.g.
  `20260928-wizard-claim-step`, `20260925-tower-selection-fixes`.
- Branch from `main`, one topic per branch. Never commit to `main` directly and never force-push it.
- Force-pushing your own unmerged feature branch is fine (for example after amending a commit).

### Commits

- Subject: `YYYYMMDD - <Summary>`, a space-hyphen-space after the date, summary capitalised, no
  trailing full stop. Examples from `main`:
  - `20260928 - Show the server's reason when the contact step is refused`
  - `20260925 - Look up the receiver location from a typed address`
- **Run `date +%Y%m%d` immediately before writing every commit message.** Never take the date from
  the conversation, the branch name or an earlier commit. The date is how history is navigated,
  so a wrong one is a real mistake.
- The summary says what changes for the node or its owner, in plain words ("Give the Tracker page
  the whole window"), not which files were edited.
- Body: plain prose paragraphs wrapped at about 76 columns, no bullet lists. Explain why, what was
  wrong before, and any non-obvious decision. Reference PRs as `#102` and other repos as
  `owl-os#63`. Say what tests cover it.
- A change meant to be reverted later starts its summary with `TEMPORARY:`.
- Usually one commit per PR. The docs update (see above) goes in the same commit as the change.
- Commits written with Claude end with the `Co-Authored-By` trailer.
- **Never commit, push or open a PR without first showing the change to the user and getting a
  yes.** Creating a branch and editing the working tree need no permission.

### Pull requests

- Title: the same as the commit subject.
- Body in Markdown, with sections that fit the change. The usual set is:
  - `## What`: the problem or feature in a paragraph.
  - `## Why`: the cause or motivation.
  - `## Fix`: what changed, for a bug fix. For a larger change, use headings for each topic instead.
  - `## Testing` or `## Verification`: what was run (full suite count, ruff) and, stated plainly,
    what was **not** verified, such as "not run in a browser" or "not tried on hardware".
  - `## After this`: follow-up work in other repos or releases, when there is any.
- Also say which docs were updated, or why none needed to change.
- PRs written with Claude end with the Claude Code line.
- Merged on GitHub with a merge commit (`Merge pull request #N from offworldlabs/<branch>`), not
  squashed. After merging, check the change really is on `origin/main` (`git grep`), not only that
  GitHub says MERGED.

Commit messages and PR descriptions are public too: the rules below apply to them.

## This repo is public

Never put sensitive information in code, comments, docs, **tests or test data**, commit
messages or PR descriptions: real node names or IDs, IP addresses, internal hostnames or tunnel
names, deployment sites, addresses or coordinates, people's or customers' names, emails,
credentials, keys, or identifiers of real accounts (such as a Cloudflare Access team domain or
application AUD tag). Describe the lesson generically ("a node near a strong broadcast tower",
"a test node") instead.

### Tests and test data must use invented values

A test or fixture that carries a real value is not allowed, however convenient it was to copy
from a live node or a real JWT. This covers `tests/`, `tests/conftest.py` and `test-data/`.
When writing or reviewing a test, use obviously fake values:

| Kind | Use |
| --- | --- |
| Node IDs | `ret00000000`, `ret4a000001`, `ret7a000001` (keep the `ret` + 8 hex shape) |
| Node and site names | `test-node-1`, `Sample Rooftop`, `Example RX` |
| IP addresses | `192.0.2.x`, `198.51.100.x`, `203.0.113.x` (RFC 5737, reserved for docs) |
| Hostnames and emails | `example.com`, `example-team.cloudflareaccess.com`, `engineer@example.com` |
| Tokens, keys, AUD tags | Generated or patterned values such as `"0123456789abcdef" * 4`, never a captured real one |
| Coordinates | A public landmark or round numbers, never where a node really sits |
| Tower callsigns | Invented (`KTST-TV`), unless the test is about public tower-finder data |

If a bug reproduces only with a real node's data, reduce it to invented values before it goes in
a test. If you find a real value already in a test, replace it and tell the user.
