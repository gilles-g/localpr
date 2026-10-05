# localpr

Review Claude's uncommitted changes in a local GitHub-style pull request page: comment on any
line, send comments in batches while Claude keeps working, ask Claude questions answered inline in
the thread, and get each comment back marked fixed or refused.

```
/localpr:review
```

One Python script, standard library only. No service, no account, no network: the page opens
offline.

![A branch under review: file tree on the left, unified diff on the right, review progress and Finish review in the toolbar](docs/review-light.png)

## Why

`git diff` shows the change but gives you nowhere to write. A large diff is unreadable in a
terminal, and "this line is wrong" has to be retyped into a chat. That hurts most right after
Claude has written a few hundred lines you are about to commit.

localpr renders the diff as a page, anchors each comment to a line, and on **Finish review** writes
a `TODO.md` that Claude Code reads back. **Send comments** hands over what is written so far without
ending the review: Claude handles that batch while you keep reading. Each comment carries its kind:

| kind | what happens to it |
|---|---|
| **Must fix** | applied to the code |
| **Follow-up** | reported back, nothing written |
| **Workflow note** | recorded as a lesson about the way of working |

Claude keeps the explicit right to refuse a comment it believes is wrong, with its reason. Each
comment comes back into its thread with a verdict: **fixed**, **refused**, **out-of-scope**, or
for a line that no longer exists, **anchor-lost**.

![Commenting on a line: the form opens under it, with the three kinds](docs/comment-form.png)

## Talk to Claude in the thread

- **Ask Claude now** — tick it on a comment to send that question alone, at once, optionally
  through a skill (`dev-gourou`, …). Claude answers in the thread and does not touch the code.
- **Reply** — under any sent thread, answer Claude back ("do what you suggested", "no, keep the
  old name"). The thread so far travels with your reply, so Claude knows what you are talking
  about.

## Reading a large diff

Unified or split view, light / dark / dark dimmed themes, context expanded around any hunk, files
ticked **Viewed**, a searchable file tree, soft wrap and tab size. The page stays fluid on reviews
of several hundred files.

## Install

As a Claude Code plugin:

```
/plugin marketplace add gilles-g/localpr
/plugin install localpr@localpr
```

Requires Python 3.9+ and git.

## Run

In Claude Code, from any repository:

```
/localpr:review                   # review the working tree
/localpr:review --base develop    # review the whole branch against develop
/localpr:review ../other-repo     # review another repository
```

Claude prints the page URL and hands control back; open it, comment, then use
either **Send comments** or **Finish review**.

## What it writes

Everything lands in `~/.claude/reviews/<project>/<timestamp>/`:

| file | what it is |
|---|---|
| `review.html` | the page |
| `diff.json` | the data model — the source of truth for comment anchors |
| `comments.json` | the review, rewritten atomically on every save |
| `batch-<n>.md` | the comments handed over by **Send comments**, the review still going on |
| `ask-<n>.md` | a question put to Claude from a comment, sent alone and at once |
| `batches.json` | which comment went in which batch |
| `events.log` | one line per batch, question and finish, then one when the server stops — what Claude watches |
| `TODO.md` | the comments no batch carried, grouped by file, plus how to handle them |
| `replies/<id>.json` | one reply per comment, written when they are applied |
| `done` | sentinel: the review is over |

## Guarantees

- **Your repository is never written to**, and no git write command is run — not even `add -N`.
- **No network.** CSS and JS are inlined; the page works over `file://`, where comments stay in
  the browser and can be copied as JSON.
- **The diff is frozen** at generation time and the page never auto-refreshes: a refresh would
  destroy the comment you are typing. **↻** re-collects it.
- **The server is local and locked down**: ephemeral port on `127.0.0.1`, token in a custom
  header, `Origin` and `Host` checked, bodies capped. It stops on **Finish review**, five minutes
  after the tab closes, or after an hour.

## What it handles

Untracked files and directories, staged changes, paths with spaces, quotes or non-UTF-8 bytes,
renames, `\ No newline at end of file`. Binary files, mode changes, submodules and nested
repositories degrade to a one-line notice instead of rendering empty.

## Development

No build, no dependency. `--check` compares the parsed `+/-` against `git diff --numstat`.

An anchor is a window, not a line number: the commented line ±2. If the file changed since the
review, the fingerprint says so and the window is searched again — exact, `rstrip`, `strip`,
normalised whitespace — rather than trusted blindly. A comment on a deleted line cannot be
re-anchored: the hunk travels with it.

CI (`.github/workflows/ci.yml`) builds a fixture repository holding every shape that once broke
the parser, runs `--check` on Python 3.9 and 3.13, verifies the assets, and drives the served
page in Chromium through Finish review.

```bash
sh .github/scripts/fixture.sh /tmp/fx
python3 scripts/localpr.py /tmp/fx/repo --check --base main
```

## Not affiliated with GitHub

localpr is not affiliated with, endorsed by, or sponsored by GitHub, Inc. The page resembles a
pull request because that is the interface reviewers already know: a hand-written approximation
inspired by [Primer](https://primer.style) (MIT), bundling no GitHub trademark, logo or asset.

## Licence

MIT — see `LICENSE`.
