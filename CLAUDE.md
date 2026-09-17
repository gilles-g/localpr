# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Claude Code plugin (`.claude-plugin/plugin.json` + `skills/review/SKILL.md`, invoked as
`/localpr:review`) whose entire product is **one standard-library Python script**: `scripts/localpr.py`.
It renders a git diff as a GitHub pull-request-style page, collects comments anchored to single
lines through a locked-down local server, then writes a `TODO.md` that Claude reads back to apply
the review.

Non-negotiable constraints — they shape every change:

- **stdlib only** — no pip, no node, no build step;
- **no network, no external resource** — CSS and JS are *inlined* into the HTML, never linked; the
  page must open over `file://`;
- **nothing is written inside the reviewed repository, and no git write command is ever run** (not
  even `add -N`): every artefact goes to `--out`.

### Two copies of the script

`scripts/localpr.py` (the publishable plugin) and the developer's local copy under
`~/.claude/tools/localpr/localpr.py` differ **by the `ASSETS` line alone**: `parent.parent / "assets"`
in the plugin, `parent / "assets"` locally. A change to one is resynchronised into the other, and
checked with `diff`: a single line must come out.

## Commands

No build, no configured linter. Python 3.9+ and git. The test suite is the GitHub Actions
workflow (`.github/workflows/ci.yml`), which replays by machine what this file asks for by hand:
`.github/scripts/fixture.sh` builds the booby-trapped repository (untracked files and directory,
nested repository, submodule bump, binary, mode alone and mode + content, path with a space,
latin-1, `\ No newline`, pure rename, rename + edit, names git quotes C-style) and `--check` runs
against it in both modes on Python 3.9 and 3.13; `check_assets.py` verifies what neither
`py_compile` nor `--check` sees — the three theme blocks declare the same tokens, the dark block
`auto_dark_block` looks for exists, `render.js` and the `JS` string parse under `node --check`,
the two manifests agree on the version; `drive.cjs` drives the served page in Chromium (comment
on a new line, a deleted line and globally, split view, *Finish review*, `TODO.md` written, server
gone). Locally: `sh .github/scripts/fixture.sh /tmp/fx` then the commands below on `/tmp/fx/repo`;
the drive needs `playwright` on `NODE_PATH`.

```bash
python3 scripts/localpr.py <repo> --check        # the only test: parser vs git diff --numstat
python3 scripts/localpr.py <repo> --dump-json    # the model, the contract every other stage consumes
python3 scripts/localpr.py <repo>                # static page, prints a file:// URL
python3 scripts/localpr.py <repo> --serve        # serve the page and collect comments
python3 scripts/localpr.py <repo> --base develop # review a whole branch, not just the working tree
python3 scripts/localpr.py --list --stop-all     # review servers still alive
```

`--check` stands in for a test suite: it compares the parsed `+/-` against `git diff --numstat` and
reports any file present on one side only. Its reference is rebuilt by `numstat_totals`, which
replays **the sources of `collect_diff`** — hence `verify(repo, model, base)`, which takes the base
rather than reading `model["base"]` (a display value, `"HEAD"` when there is none) — and sums them
**per path**: with `--base`, a file committed in the branch and modified again in the working tree
holds two records, one per source. Comparing record by record made every branch commit a false
positive. **Any change to `parse_diff` / `collect_diff` /
`build_model` is validated by a `--check` on a real repository**, ideally one holding untracked
files, binaries, renames, paths with spaces and non-UTF-8 files. Exit code 1 on divergence.

To iterate on the rendering: generate the static page against a test repository and open the
printed `file://` URL — the static page works with no server (fallback mode, comments in
localStorage).

Anything touching the page's cost is measured on a **generated repository of several hundred
files** (the fixture is tiny, and nothing about the load shows on it), by driving Chromium with a
real wheel scroll *and* real mouse moves: without the moves nothing scrolls under the cursor, and
the whole hover cost disappears from the measurement. `Performance.getMetrics` over that sequence
gives the useful numbers (`TaskDuration`, `LayoutCount`, `Nodes`); a `devtools.timeline` trace
aggregated by event name says which of prepaint, paint or hit-test is paying. The reference to
beat is the same page with JavaScript disabled.

Neither `python3 -m py_compile` nor `--check` (beyond the parser) **proves anything here**: both
went green on a version that raised a `NameError` on the first diff, and on desynchronised JS keys.
A change is validated by **executing** the whole chain — static render, then the page actually
driven (form opened, comment submitted, thread displayed).

The plugin is installed locally from a *directory* marketplace (`claude plugin marketplace add <this
directory>`, then `claude plugin install localpr@localpr`). The marketplace points at this tree, but
installing **copies the plugin** into `~/.claude/plugins/cache/localpr/localpr/<version>/`, and that
cache is keyed by the version in `plugin.json`: `claude plugin marketplace update` does not refresh
it, and `claude plugin update` short-circuits on an unchanged version. So an edit made here does not
reach the installed plugin. To test the working tree, run `claude --plugin-dir <this directory>`; to
refresh the installed copy, uninstall and reinstall, or bump the version.

## Architecture

A single pass, from raw git to the page:

`collect_diff` (hardened git: `GIT_HARDENING`, `DIFF_OPTS`, untracked files through
`ls-files --others`) → `parse_diff` (state machine) → `build_model` (`diff.json`) → `render` (one
HTML string) → `serve` (`Review` + local handler).

The parts that cannot be deduced from a single file:

- **A status the header alone cannot decide.** git writes `old mode`/`new mode` in the header of a
  file whose contents changed too, and a header never holds its own `@@`: `status_from_header` used
  to badge such a file `mode` — "permission change only" over a visible hunk — and `verify` skips
  `mode` records, so its counters went unchecked. `clore()` downgrades it to `modif` once the record
  turns out to carry hunks. Symmetrically, a submodule bump writes its `Subproject commit` lines
  *inside* a hunk, never in the header: it comes out `modif`, showing both SHAs, and `--numstat`
  confirms its 1/1 — the `sousmodule` status is in practice what `nested_repo_record` uses.
  Same family: `STATUS_NOTES` is only attached to a record **without hunks**, and `render_file`
  shows the note *instead of* the table — a renamed file whose contents changed too used to
  render "rename detected" under a header announcing its `+/-`, its diff hidden. `--check` cannot
  see that: it is a rendering fault.
- **A quoted path is C-escaped.** `core.quotepath=false` leaves non-ASCII alone, but a quote, a
  backslash or a control character still gets the path quoted on `diff --git`, `---`/`+++`,
  `rename from/to` *and* `--numstat`: `unquote` / `split_quoted` undo it everywhere a path is
  read. Stripping the quotes alone left `we\"ird.txt`, a file that does not exist.
- **A nested repository is not enumerable.** `ls-files --others` expands an untracked directory —
  except one holding its own `.git`, returned as a single entry with a trailing `/` that
  `git diff --no-index` refuses (`Could not access 'nested/null'`), silently, since `git()` is
  tolerant. `untracked()` therefore returns the files and those folded directories apart, and
  `build_model` gives each of the latter a `nested_repo_record` — status `sousmodule`, its own
  `note`, no hunk. Never fabricate diff text to feed `parse_diff`: the parser translates what git
  actually wrote, nothing else.
- **`parse_diff` is driven by the `@@` counters**, not by the shape of the lines: a fixture file
  containing `diff --git` therefore cannot break a record. A record with no hunk (binary, mode only,
  pure rename, submodule) is opened by `finalise()`, without which it silently vanishes from the
  review.
- **`diff.json` is the contract** (`MODEL_VERSION`): the source of truth for anchors. `window.LOCALPR`
  carries only an extract of it (path + fingerprint per file, plus comments/findings/replies/token).
- **An anchor is a window, not a line number**: the commented line ±2, with `anchorOffset`. The
  `fingerprint` (truncated sha256 of the file) says whether line numbers are still trustworthy.
  Comments with `side: "old"` (a deleted line) cannot be re-anchored: the `hunk` travels with them.
- **`sanitize_state` is the trust boundary**: `COMMENT_KEYS` whitelist, constrained types (`SCOPES`,
  `TYPES`, `COMMENT_ID`, `FIELD_TYPES`), bounded lengths. A field added to a comment on the page but
  missing from `COMMENT_KEYS` is **silently dropped** on save. `FIELD_TYPES` is what keeps a wrong
  type out of `write_todo`, which assumes `anchor` is a string: without it, a `/done` crashed
  server-side, answering the page nothing and writing no `TODO.md`. A findings file is external
  input on that same path — taking a finding over copies it into a comment — so `load_findings`
  passes every field through `typed()`: a type refused only later would stop the review saving at
  all. The range matters as much as the type: `anchorOffset` indexes the anchor window in
  `write_todo`, and an out-of-range value was the same server-side crash on `/done`.
- **`PROTOCOL` + `write_todo` are the executable specification of the next stage**: the instructions
  travel with the data (knowledge filed away in a skill only loads if someone invokes it). Editing
  that text means editing the instructions given to the agent that will apply the review — notably
  the three types (`fix` to the code, `followUp` reported back, `workflowNote` as a lesson) and the
  explicit right to **refuse** a comment.
- **The served HTML lives in memory** (`Review.blob`): re-reading the file on every request would
  expose the page truncated mid-rewrite. Every write goes through `write_atomic`.
- **Three themes, no duplication**: `primer-like.css` holds one `:root` block per theme -
  light, `[data-theme='dark']` (GitHub's dark) and `[data-theme='dimmed']` (its dark dimmed), all
  three transcribed from `@primer/primitives`, translucent diff backgrounds included. A new colour
  is a token declared in the **three** blocks. `auto_dark_block` re-extracts the dark block to
  derive the auto mode, guarded on the **absence** of `data-theme`: `:not([data-theme='light'])`
  would have the same specificity as the dimmed block while sitting later in the stylesheet, so on
  a dark OS it would override a theme chosen by hand. Never hand-copy dark rules.
- **Three assets, three jobs**: `primer-like.css` holds the design tokens and the generic
  components, `review.css` the review page itself (app shell, sidebar, diff tables, threads),
  `render.js` the highlighting. A layout rule goes in `review.css`, never in the Python.
  Watch specificity: `.diff-table td` beats a bare `.line-num`, so the cell rules are written
  `.diff-table td.line-num`.
- **`assets/render.js`**: dependency-free highlighting for php, javascript/ts, python, go, rust,
  java, c-family, sql, shell, json, yaml, ini/toml, css, html/xml/twig and markdown, plus minimal
  markdown rendering (used for comment bodies). One configurable tokenizer (`clike`) covers the
  brace languages; the shape-driven ones (yaml, json, css, markup, markdown, ini) have their own.
  `highlighterFor` returns a **closure whose state survives from one line to the next** (a string or
  comment left open across lines) — one highlighter per file section, never shared.
- **Only the unified table is rendered by Python.** The split view is rebuilt on the client from
  `modele(section)`, an array read once from that table *after* colouring; switching views throws
  the split table away and puts the kept unified one back. Consequence: the side and the line
  number live on the **cells** (`data-side` / `data-line`), not on the row — a split row holds one
  line of each side. A comment anchor is computed from that array, so it does not depend on the
  view showing.
- **The page is sized for a review of hundreds of files, and every per-file cost is paid on
  approach, not at load.** An `IntersectionObserver` (`rootMargin: 1500px`) is what calls
  `preparer` — colouring, then the split rebuild; `prepares` holds what has been prepared, and
  that set, not the whole document, is what a view switch replays. Colouring the whole review at
  load doubled the node count of a 5 MB page, and the browser then paid for it on every frame.
  Consequence: a section rebuilt *after* the threads were rendered loses the rows they sat in —
  hence `rendreFilsDe(section)`, which re-renders that one file rather than the review.
- **`content-visibility: auto` on `.file-diff-body` is what makes scrolling cheap**: off-screen
  files are neither laid out nor painted nor hit-tested (a full scroll of a 600-file page went
  from ~3.3 s to ~0.6 s of main-thread work, below what the same page costs with JS disabled).
  It only holds because Python emits the exact height of each body — `--body-h`, rendered rows ×
  `LINE_HEIGHT` — in `contain-intrinsic-block-size`: on a stylesheet guess the scrollbar jumps at
  every file the scroll reaches. Placed on the *body*, never on the section, so a collapsed file
  (body `display: none`) reserves nothing and the sticky header keeps working.
- **The `+` button is a single floating element** parked on `<body>`, moved by `transform` and
  dimmed by `.off` — never inserted into a cell, never `hidden`. Both mutate the layout tree of a
  table holding thousands of rows, and the browser replays that walk at every hover: it was the
  single biggest cost on the page (~900 ms of prepaint per 40 mouse moves). It is repositioned on
  `#main`'s scroll, throttled by `requestAnimationFrame`, and its cell lives in
  `boutonAjout.celluleAncree` — the DOM no longer says which line is hovered.

### Invariants not to be "repaired"

They are stated in the module docstring, and read like bugs to anyone who does not know why:

- **no `--watch` loop, no reload, no `<meta refresh>`**: the diff is frozen at generation time and
  the only mutable state on the page belongs to the reviewer; a refresh would destroy the comment
  being typed. The **Regenerate** button is the way to re-collect a diff.
- the page's only `setInterval` is a 30 s **presence heartbeat** to `/ping`: it renders nothing, it
  exists so the server shuts itself down once the tab is closed. It is not a refresh.

### Local server

Ephemeral port on `127.0.0.1`, mandatory token (`?t=` query on GET, `X-Localpr-Token` header on POST),
`Host` **and** `Origin` checked (DNS rebinding), `OPTIONS` always answers 403 — the absence of a CORS
header makes the preflight a third-party page would trigger fail — body capped at `MAX_PAYLOAD`.
Routes: `GET /review.html`, then `POST` on `/ping`, `/prefs`, `/regenerate`, `/comments` and
`/done`. Three shutdown paths: *Finish review*, `SILENCE_MAX` (300 s) with no request at all, `--max-minutes`
(60 by default). `server.json` is deleted on clean shutdown: a `server.json` with no live process is
the record of a server that was killed, not of one that is running. A live pid proves nothing
either — the number goes to the next process to start — so `live_servers` only calls a server
*alive* once a `POST /ping` with the recorded token answers 200, and `--stop-all` never SIGTERMs
anything else.

## Conventions

- **Python and the embedded JS share keys, and nothing checks it.** A comment's type
  (`fix` / `followUp` / `workflowNote`), a reply's `verdict`
  (`fixed` / `refused` / `out-of-scope` / `anchor-lost`), a finding's `origin.state`
  (`applied` / `dropped`) and a pref's `theme` (`auto` / `light` / `dark` / `dimmed`, in `THEMES`,
  in the toolbar's `<option>` values and in the CSS selectors) are produced on the Python side and
  **compared as literals inside the `JS` string** of the same file. A divergence breaks nothing: it silently degrades the rendering
  (`undefined` label, severity falling back to `nitpick`, a correct reply displayed as a failure).
  Neither `--check` nor a CLI run sees it — **only the real rendering does**. After touching one of
  those enumerations, grep the value across the whole file, on both sides.
- **The English-only rewrite is not finished.** Still French: the persisted keys `fichier_index`,
  `deposeA`, `reprisDe`, `sansNewline`; the `status` values (`modif` / `suppression` / `binaire` /
  `mode` / `sousmodule` / `renommage`), **displayed as badges verbatim**; and a number of internal
  variable names. The persisted keys are **contract** (`diff.json`, `comments.json`, `TODO.md`,
  `replies/`): renaming them breaks reviews already on disk.
- **Never translate by substring replacement.** An earlier pass produced 37 orphaned calls and
  half-translated strings where a French word had been overwritten mid-sentence. Word boundaries
  (`\bword\b`), and execution after every pass.
- **Comments and docstrings state only the trap being avoided**, never what the code does: an
  absolute path that would break a file's identity, a `git status --porcelain` that folds a
  directory, a `latin-1` file that would kill the whole page. Write in that register, or write
  nothing.
- The HTML is assembled by concatenating f-strings; everything coming from the data goes through
  `esc()`.
- Threads, the comment form, the file tree's search and the comment tracker are rendered
  **client-side** (`JS`, a string inside `localpr.py`). Do not mirror their labels on the Python
  side: an earlier `SEV` / `TYPE_LABEL` pair sat there dead, and diverged.
- **What the reviewer chose is not part of the contract**, and never goes into `comments.json` —
  the agent that applies the review has no use for it. But it is *global to the machine*: theme,
  unified/split, soft wrap, tab size and sidebar width go to `~/.config/localpr/prefs.json`
  (`XDG_CONFIG_HOME` honoured) through `POST /prefs`, **merged** into the file and filtered by
  `sanitize_prefs` — the same trust boundary as `sanitize_state`, with the same trap: a key
  missing from it is silently dropped. `render` inlines that file into `window.LOCALPR_PREFS`, read
  by a script placed before the first paint so the theme does not flash. `localStorage`
  (`localpr:prefs`) is kept as a mirror, and the newest of the two stores wins on an `at` stamp: a
  page opened with no server can only write localStorage. The files ticked *viewed*
  (`localpr:viewed:<repo>:<base>`) stay per-browser.

## Still undecided

`claude plugin validate --strict` passes: MIT `LICENSE`, `author` in `plugin.json` (name + GitHub
URL, no email), and the two version fields aligned on `0.5.0` — `plugin.json` wins at install time.
The install snippet points at `gilles-g/localpr`, the repository's own remote.

**The `marketplace.json` entry is no longer read for display alone.** Its `source` is pinned
(`source: github`, `repo`, `ref: v<version>`) instead of the `./` that served whatever sat on the
default branch: people install this now, and a broken commit on `main` used to reach them within
the second, with no release to roll back to. Consequence — **`ref` is a dangling pointer until the
tag is pushed**: bumping the version means bumping `ref` *and* pushing the annotated tag it names,
in that order. A `marketplace.json` on `main` naming a tag that does not exist on the remote
breaks every installation, and `check_assets.py` does not see it — it only compares the two
version fields to each other.

What remains open: the plugin has never been published, so everything but `README.md` is still
untracked, and whether `CLAUDE.md` itself belongs in the published tree is the developer's call.
**Nothing is ever committed without an explicit request.**
