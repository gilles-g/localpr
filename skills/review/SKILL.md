---
description: Open a local pull-request-style page to review the current git diff. Use when the developer wants to read a diff as a PR, comment on specific lines, or hand a review back to Claude. Triggers: "/localpr:review", "review this diff", "open the review page", "let me comment on these changes".
argument-hint: "[repo path | nothing = current repo] [--base <ref>]"
disable-model-invocation: true
---

Launch the localpr review page on the target repository — `$ARGUMENTS` if given, otherwise the
current repository — then hand control straight back.

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/localpr.py" <repo> --serve
```

The process holds the terminal: run it **in the background**, wait for the URL to be printed, then
give the developer two lines:

1. the **clickable URL exactly as printed** — it carries the token; without it the page answers 403;
2. a ready-to-paste open command: `! xdg-open "<url>"` (or `open` on macOS).

Do not open the browser for them.

State these two points, one line each, because neither is guessable:

- the page **does not refresh** — the diff is frozen; the `↻` button, top right, re-collects it;
- **Send comments** hands over the comments written so far and **keeps the server running**: the
  review goes on while they are handled, and `↻` shows the replies. **Finish review** hands over
  the rest, writes a `TODO.md` and **shuts the server down**. Without that click, the server stops
  on its own 5 minutes after the tab is closed, and in any case after an hour.
- **Ask Claude now**, ticked in a comment (optionally naming a skill), sends that comment alone, at
  once: the answer shows up in its thread without `↻`.

Then arm a **Monitor** on the output directory's event log, and nothing else — no polling, no
relaunch. This is a human review and it takes as long as it takes:

```bash
tail -n 0 -F "<out>/events.log"
```

with the maximum timeout, re-armed on each expiry while the server is alive (`--list`). Every line
is an event:

- `batch <n>: … - to handle: <path>` — read that `batch-<n>.md` and handle it now: it carries the
  comments grouped by file **and** the protocol for handling them. The developer is still reading;
  leave the page and the server alone.
- `ask <n>: … - to handle: <path>` — the developer is **waiting on screen**: drop everything else,
  read that `ask-<n>.md` and write each answer to `replies/<id>.json` as it is ready. Answer, do not
  change the code; a named skill is invoked first.
- `done: … - to handle: <path>` — read `TODO.md`, the comments no batch carried; stop the monitor.
- `stopped: …` — the server died without *Finish review*; stop the monitor and say so.

When the developer comes back instead ("I'm done", "I sent a batch", or a pasted JSON blob), read
the newest `batch-<n>.md` or the `TODO.md` in the output directory. The directory is announced at
launch, and `--list` finds it again.

Options worth knowing, to be passed only when the request calls for them:

| | |
|---|---|
| `--base develop` | review a whole branch instead of the working tree alone |
| `--out <dir>` | defaults to `~/.claude/reviews/<project>/<timestamp>/` |
| `--findings f.json` | show findings from an automated review as a second reviewer |
| `--check` | verify the parser against `git diff --numstat`, serving nothing |
| `--list` / `--stop-all` | see or stop review servers still alive |

Before running against an unfamiliar repository, `--check` costs two seconds and avoids reviewing a
wrong diff.

localpr runs **no** git write command, and never writes inside the repository: every artefact goes to
its output directory.
