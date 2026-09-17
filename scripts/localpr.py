#!/usr/bin/env python3
"""localpr - GitHub-style pull request review of a local diff, in one self-contained page.

    python3 localpr.py <repo>                    # static render, prints file://.../review.html
    python3 localpr.py <repo> --serve            # serve the page and collect comments
    python3 localpr.py <repo> --dump-json        # the data model, the contract other stages consume
    python3 localpr.py <repo> --check            # parser integrity against git diff --numstat

Outputs, under --out (default ~/.claude/reviews/<project>/<timestamp>/):
    diff.json           model snapshot - the source of truth for comment anchors
    review.html         the page
    comments.json       full state, rewritten atomically on every POST
    replies/<id>.json   one reply per comment, written by the agents
    findings.json       optional, imported through --findings
    server.json         {url, port, token, pid} in --serve mode
    TODO.md             the comments grouped by file, plus how to handle them
    done                sentinel meaning the review is over

The reviewer's display preferences (theme, layout, sidebar) go to ~/.config/localpr/prefs.json,
global to the machine and the only thing written outside --out: localStorage cannot hold them,
the served page changes origin with every ephemeral port.

Standard library only, no external resource: the page must open offline.

Two deliberate departures from its sibling script render_pipeline.py, not to be "repaired":
there is NO --watch loop, NO page reload, NO <meta refresh>. The diff is frozen at generation
time and the only mutable state on the page belongs to the client: a refresh would destroy the
comment being typed, the selection and the expanded panels. To see a diff that has grown, the
page has a Regenerate button. And the HTML served comes from an in-memory blob rather than a
file re-read on every request: a page rewritten while being served would arrive truncated.

The page's only setInterval is a 30s presence heartbeat to /ping: it renders nothing and
overwrites nothing, it exists so the server shuts itself down once the tab is closed. Do not
mistake it for a refresh, and do not remove it in the name of the paragraph above.
"""

import argparse
import hashlib
import html
import json
import os
import re
import secrets
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs
from urllib.request import ProxyHandler, Request, build_opener

MODEL_VERSION = 1

GIT_HARDENING = [
    "--no-pager",
    "-c", "color.ui=false",
    "-c", "core.quotepath=false",
    "-c", "diff.noprefix=false",
    "-c", "diff.mnemonicPrefix=false",
]
DIFF_OPTS = ["--no-color", "--no-ext-diff", "--no-textconv", "-U3"]

HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")

GENERATED = re.compile(
    r"(^|/)(composer\.lock|package-lock\.json|yarn\.lock|pnpm-lock\.yaml)$"
    r"|\.min\.(js|css)$|\.(svg|map)$|(^|/)migrations/"
)

FILE_CAP = 400
LINE_HEIGHT = 20
GLOBAL_CAP = 15000
MAX_PAYLOAD = 256 * 1024


class GitUnavailable(RuntimeError):
    pass


def git(repo, *args, tolerant=True):
    """Output decoded with invalid bytes replaced.

    A legacy latin-1 file would raise UnicodeDecodeError under subprocess(text=True), killing the
    whole page instead of degrading a single file.
    """
    try:
        r = subprocess.run(
            ["git", *GIT_HARDENING, *args],
            cwd=str(repo),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError as e:
        raise GitUnavailable(f"git not found: {e}") from e
    if not tolerant and r.returncode != 0:
        raise GitUnavailable(r.stderr.decode("utf-8", errors="replace").strip())
    return r.stdout.decode("utf-8", errors="replace")


def repo_root(path):
    out = git(path, "rev-parse", "--show-toplevel", tolerant=False).strip()
    if not out:
        raise GitUnavailable(f"{path} is not a git repository")
    return Path(out)


def has_head(repo):
    try:
        git(repo, "rev-parse", "--verify", "HEAD", tolerant=False)
        return True
    except GitUnavailable:
        return False


def short_head(repo):
    return git(repo, "rev-parse", "--short", "HEAD").strip() if has_head(repo) else ""


def untracked(repo):
    """Untracked files, then the directories git refuses to expand.

    `git status --porcelain` folds an entirely untracked directory into ONE entry with a trailing
    "/": the files inside it would be missing from the review without a single message.
    `ls-files --others` expands them - except a nested git repository, which comes back as that
    same folded entry and whose contents git will not enumerate at all. Returned apart, because
    it is diffed as a whole instead of file by file.
    """
    raw = git(repo, "ls-files", "--others", "--exclude-standard", "-z")
    paths = [p for p in raw.split("\0") if p]
    return ([p for p in paths if not p.endswith("/")],
            [p for p in paths if p.endswith("/")])


def nested_repo_record(path):
    """A nested repository, degraded to a one-line notice.

    `git diff --no-index /dev/null <dir>` fails, and git() is tolerant: without this record the
    whole directory would leave the review without a message.
    """
    return {
        "path": path,
        "pathBefore": None,
        "status": "sousmodule",
        "hunks": [],
        "note": "nested git repository, not tracked here - contents not shown",
    }


def untracked_diff(repo, path):
    """Diff of an untracked file, never writing to the index.

    The path must stay RELATIVE: given an absolute path, git emits "+++ b/home/..." (the leading
    "/" is eaten by the "b/" prefix) and the file's identity becomes wrong.
    """
    return git(repo, "diff", "--no-index", *DIFF_OPTS, "/dev/null", path)


def collect_diff(repo, base=None):
    parts = []
    if base:
        parts.append(git(repo, "diff", *DIFF_OPTS, f"{base}...HEAD"))
        parts.append(git(repo, "diff", *DIFF_OPTS, "HEAD"))
    elif has_head(repo):
        parts.append(git(repo, "diff", *DIFF_OPTS, "HEAD"))
    tracked = {f["path"] for f in parse_diff("".join(parts))}
    fichiers, _ = untracked(repo)
    for path in fichiers:
        if path not in tracked:
            parts.append(untracked_diff(repo, path))
    return "".join(parts)


def paths_from_header(lines):
    """File path, read from --- / +++ and never from "diff --git".

    For a path containing a space, git escapes nothing on the "diff --git" line (ambiguous by
    construction) but terminates the --- / +++ lines with a TAB. Four cases have neither --- nor
    +++: binary, mode change alone, pure rename, submodule - hence the fallback on "diff --git",
    where the ambiguity is resolved by finding the split whose two halves are identical.
    """
    before = after = None
    for line in lines:
        if line.startswith("--- ") and before is None:
            before = strip_path(line[4:])
        elif line.startswith("+++ ") and after is None:
            after = strip_path(line[4:])
        elif line.startswith("rename from "):
            before = strip_path(line[len("rename from "):], prefix=False)
        elif line.startswith("rename to "):
            after = strip_path(line[len("rename to "):], prefix=False)
    if before is None and after is None:
        before = after = path_from_git_header(lines[0] if lines else "")
    return before, after


C_ESCAPES = {"a": 7, "b": 8, "t": 9, "n": 10, "v": 11, "f": 12, "r": 13, '"': 34, "\\": 92}


def unquote_c(path):
    """core.quotepath=false leaves non-ASCII alone, but a quote, a backslash or a control
    character still gets the path quoted C-style: read back verbatim, we\\"ird.txt is a file that
    does not exist, with no fingerprint and a red --check."""
    out, i = bytearray(), 0
    while i < len(path):
        c = path[i]
        if c != "\\" or i + 1 >= len(path):
            out += c.encode("utf-8")
            i += 1
            continue
        n = path[i + 1]
        if n in C_ESCAPES:
            out.append(C_ESCAPES[n])
            i += 2
        elif n in "01234567":
            j = i + 1
            while j < len(path) and j < i + 4 and path[j] in "01234567":
                j += 1
            out.append(int(path[i + 1:j], 8) & 0xFF)
            i = j
        else:
            out += n.encode("utf-8")
            i += 2
    return out.decode("utf-8", errors="replace")


def unquote(path):
    if path.startswith('"') and path.endswith('"') and len(path) > 1:
        return unquote_c(path[1:-1])
    return path


def split_quoted(rest):
    """The two halves of a "diff --git" line when git quoted them: the closing quote is the
    first one not escaped, a space inside the name splits nothing."""
    i = 1
    while i < len(rest):
        if rest[i] == "\\":
            i += 2
            continue
        if rest[i] == '"':
            return unquote(rest[:i + 1]), unquote(rest[i + 2:])
        i += 1
    return unquote(rest), ""


def strip_path(rest, prefix=True):
    path = rest.split("\t", 1)[0].rstrip("\n")
    if path == "/dev/null":
        return None
    path = unquote(path)
    if prefix and len(path) > 2 and path[1] == "/":
        path = path[2:]
    return path


def path_from_git_header(line):
    rest = line[len("diff --git "):].rstrip("\n") if line.startswith("diff --git ") else ""
    if rest.startswith('"'):
        left, right = split_quoted(rest)
        if left.startswith("a/") and right.startswith("b/") and left[2:] == right[2:]:
            return left[2:]
        return left[2:] if left.startswith("a/") else left
    for i, c in enumerate(rest):
        if c != " ":
            continue
        left, right = rest[:i], rest[i + 1:]
        if left.startswith("a/") and right.startswith("b/") and left[2:] == right[2:]:
            return left[2:]
    if rest.startswith("a/"):
        return rest[2:].split(" b/", 1)[0]
    return rest or "(unknown path)"


def status_from_header(lines, before, after):
    text = "\n".join(lines)
    if "GIT binary patch" in text or re.search(r"^Binary files .* differ$", text, re.M):
        return "binaire"
    if "rename from " in text:
        return "renommage"
    if before is None:
        return "added"
    if after is None:
        return "suppression"
    if re.search(r"^new file mode ", text, re.M):
        return "added"
    if re.search(r"^deleted file mode ", text, re.M):
        return "suppression"
    if re.search(r"^Subproject commit ", text, re.M):
        return "sousmodule"
    if re.search(r"^(old|new) mode ", text, re.M):
        return "mode"
    return "modif"


def parse_diff(text):
    """Unified diff -> list of files, each with its hunks and typed lines.

    State machine: inside a hunk, consumption is driven by the @@ header counters, not by the
    shape of the lines. A fixture file that CONTAINS "diff --git" therefore cannot cause a false
    record break.

    Split on "\\n" and never splitlines(): the latter also breaks on CR, form feed and U+2028,
    which a diff line may legitimately contain - the hunk counters then drift and the rest of
    the file is read as a new record with no path.
    """
    files = []
    header, courant, hunk = [], None, None
    reste_avant = reste_apres = 0

    def clore():
        """A mode change is only "mode" once the record turns out to carry no hunk.

        git writes "old mode"/"new mode" in the header of a file whose contents changed too, and a
        header never holds its own "@@": read there alone, such a file was badged "permission
        change only" over a visible hunk, and --check skipped its counters.
        """
        nonlocal courant, hunk
        if courant is not None:
            if hunk is not None and hunk["lines"]:
                courant["hunks"].append(hunk)
            if courant["status"] == "mode" and courant["hunks"]:
                courant["status"] = "modif"
            files.append(courant)
        courant, hunk = None, None

    def ouvrir():
        nonlocal courant, header
        before, after = paths_from_header(header)
        path = after or before or "(unknown path)"
        courant = {
            "path": path,
            "pathBefore": before if before != path else None,
            "status": status_from_header(header, before, after),
            "hunks": [],
        }
        header = []

    def finalise():
        """A record with no hunk (binary, mode only, pure rename, submodule) was never
        opened: without this, the next "diff --git" overwrites its header and the file
        vanishes from the review."""
        if courant is None and header:
            ouvrir()
        clore()

    lignes = text.split("\n")
    if lignes and lignes[-1] == "":
        lignes.pop()

    for line in lignes:
        if line.startswith("\\") and hunk is not None and hunk["lines"]:
            hunk["lines"][-1]["sansNewline"] = True
            continue

        if reste_avant > 0 or reste_apres > 0:
            tete = line[:1]
            if tete == "+":
                hunk["lines"].append({"t": "add", "before": None,
                                       "after": hunk["_apres"], "txt": line[1:]})
                hunk["_apres"] += 1
                reste_apres -= 1
                continue
            if tete == "-":
                hunk["lines"].append({"t": "del", "before": hunk["_avant"],
                                       "after": None, "txt": line[1:]})
                hunk["_avant"] += 1
                reste_avant -= 1
                continue
            hunk["lines"].append({"t": "ctx", "before": hunk["_avant"],
                                   "after": hunk["_apres"], "txt": line[1:]})
            hunk["_avant"] += 1
            hunk["_apres"] += 1
            reste_avant -= 1
            reste_apres -= 1
            continue

        if line.startswith("diff --git "):
            finalise()
            header = [line]
            continue

        m = HUNK_HEADER.match(line)
        if m:
            if courant is None:
                if not header:
                    continue
                ouvrir()
            if hunk is not None and hunk["lines"]:
                courant["hunks"].append(hunk)
            debut_avant, n_avant = int(m.group(1)), int(m.group(2) or 1)
            debut_apres, n_apres = int(m.group(3)), int(m.group(4) or 1)
            hunk = {
                "header": line.rstrip("\n"),
                "section": m.group(5).strip(),
                "lines": [],
                "_avant": debut_avant,
                "_apres": debut_apres,
            }
            reste_avant, reste_apres = n_avant, n_apres
            continue

        if courant is None:
            header.append(line)
        else:
            finalise()
            header = []

    finalise()

    for f in files:
        for h in f["hunks"]:
            h.pop("_avant", None)
            h.pop("_apres", None)
    return files


def fingerprint(path):
    try:
        return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()[:13]
    except OSError:
        return None


def count_changes(file):
    additions = sum(1 for h in file["hunks"] for l in h["lines"] if l["t"] == "add")
    deletions = sum(1 for h in file["hunks"] for l in h["lines"] if l["t"] == "del")
    return additions, deletions


def build_model(repo, base=None):
    files = parse_diff(collect_diff(repo, base))
    fichiers, imbriques = untracked(repo)
    files += [nested_repo_record(p) for p in imbriques]
    non_suivis = set(fichiers) | set(imbriques)
    total_a = total_s = 0
    for i, f in enumerate(files):
        additions, deletions = count_changes(f)
        total_a += additions
        total_s += deletions
        f["index"] = i
        f["additions"] = additions
        f["deletions"] = deletions
        f["tracked"] = f["path"] not in non_suivis
        f["generated"] = bool(GENERATED.search(f["path"]))
        f["fingerprint"] = (None if f["status"] == "suppression"
                          else fingerprint(repo / f["path"]))
        # A rename whose contents changed too carries hunks: the note would hide them, under
        # a header still announcing their +/-.
        f["note"] = f.get("note") or (None if f["hunks"] else STATUS_NOTES.get(f["status"]))
    return {
        "version": MODEL_VERSION,
        "generated": datetime.now().astimezone().isoformat(timespec="seconds"),
        "repo": str(repo),
        "project": repo.name,
        "base": base or "HEAD",
        "head": short_head(repo),
        "totals": {
            "files": len(files),
            "additions": total_a,
            "deletions": total_s,
            "lines": sum(len(h["lines"]) for f in files for h in f["hunks"]),
        },
        "files": files,
    }


STATUS_NOTES = {
    "binaire": "binary file, contents not shown",
    "mode": "permission change only",
    "sousmodule": "submodule, contents not shown",
    "renommage": "rename detected (only visible with --base)",
}


def file_lines(path):
    try:
        content = path.read_bytes().decode("utf-8", errors="replace")
    except OSError:
        return None
    if not content:
        return 0
    return content.count("\n") + (0 if content.endswith("\n") else 1)


def numstat_path(raw):
    """--numstat folds a rename into a single field: src/{Old.php => New.php}."""
    m = re.match(r"^(.*)\{(.*) => (.*)\}(.*)$", raw)
    if m:
        return (m.group(1) + m.group(3) + m.group(4)).replace("//", "/")
    if " => " in raw:
        return raw.split(" => ", 1)[1]
    return raw


def numstat_totals(repo, base):
    """The reference --check compares against: the sources of collect_diff, summed per path.

    Summed, because with --base a file committed in the branch and modified again in the working
    tree holds TWO records in the model - one per source. A path git reports as binary ("-") is
    marked unmeasurable rather than counted.
    """
    totaux = {}
    sources = ([f"{base}...HEAD"] if base else []) + (["HEAD"] if has_head(repo) else [])
    for source in sources:
        for line in git(repo, "diff", "--numstat", source).splitlines():
            bouts = line.split("\t", 2)
            if len(bouts) != 3:
                continue
            path = numstat_path(unquote(bouts[2]))
            a, s = totaux.get(path, (0, 0))
            if a is None or bouts[0] == "-" or bouts[1] == "-":
                totaux[path] = (None, None)
            else:
                totaux[path] = (a + int(bouts[0]), s + int(bouts[1]))
    return totaux


def verify(repo, model, base=None):
    """Parser integrity: the parsed +/- against git itself.

    Reference for tracked files: numstat_totals, on the same sources as collect_diff - given the
    base, which the model only keeps as a display value ("HEAD" when there is none). Reference for
    untracked ones: the file's line count, since `--numstat` does not know about them.
    """
    divergences = []
    attendu = numstat_totals(repo, base)

    vus, parses = set(), {}
    for f in model["files"]:
        vus.add(f["path"])
        if f["status"] in ("binaire", "mode", "sousmodule"):
            continue
        if f["tracked"]:
            a, s = parses.get(f["path"], (0, 0))
            parses[f["path"]] = (a + f["additions"], s + f["deletions"])
        else:
            n = file_lines(repo / f["path"])
            if n is not None and n != f["additions"]:
                divergences.append(
                    f"{f['path']}: parsed +{f['additions']}, file has {n} line(s)"
                )

    for path, (a, s) in sorted(parses.items()):
        ref = attendu.get(path)
        if ref is None:
            divergences.append(f"{path}: parsed but missing from --numstat")
        elif ref[0] is not None and (a, s) != ref:
            divergences.append(
                f"{path}: parsed +{a}/-{s}, git says +{ref[0]}/-{ref[1]}"
            )

    for path in attendu:
        if path not in vus:
            divergences.append(f"{path}: in --numstat but missing from the model")
    fichiers, imbriques = untracked(repo)
    for path in fichiers + imbriques:
        if path not in vus:
            divergences.append(f"{path}: untracked but missing from the model")

    absolus = [f["path"] for f in model["files"] if f["path"].startswith("/")]
    divergences += [f"{c}: absolute path in the model" for c in absolus]
    return divergences


ASSETS = Path(__file__).resolve().parent.parent / "assets"

def auto_dark_block(css):
    """The borrowed CSS drives the theme through data-theme only: the "auto" mode is derived from it.

    The dark tokens are re-emitted under prefers-color-scheme, guarded by the absence of the
    attribute and not by :not([data-theme='light']): 'dimmed' has the same specificity and sits
    earlier in the stylesheet, so a value-based guard would override a theme chosen by hand.
    """
    debut = css.find(":root[data-theme='dark'] {")
    if debut < 0:
        return ""
    fin = css.find("}", debut)
    body = css[css.find("{", debut) + 1:fin]
    return ("@media (prefers-color-scheme: dark){:root:not([data-theme]){"
            + body + "}}")


def read_asset(nom):
    try:
        return (ASSETS / nom).read_text(encoding="utf-8")
    except OSError:
        return ""


JS = r"""
(function () {
  var D = window.LOCALPR;
  var CLE = 'localpr:' + D.repo + ':' + D.base;
  var CLE_VUS = 'localpr:viewed:' + D.repo + ':' + D.base;
  var CLE_PREFS = 'localpr:prefs';
  var server = false, state = null, seq = 0;
  var SEVS = { fix: 'blocking', followUp: 'nitpick', workflowNote: 'question' };
  var LIBS = { fix: 'Must fix', followUp: 'Follow-up', workflowNote: 'Workflow note' };

  function maintenant() { return new Date().toISOString() }
  /* The server stamps its local offset, the page stamps UTC: compared as strings, a comment
     saved at 20:30Z looks older than a 22:18+02:00 state and the stale one wins. */
  function instant(s) { var t = Date.parse(s || ''); return isNaN(t) ? 0 : t }
  function esc(s) { var d = document.createElement('div'); d.textContent = s == null ? '' : String(s); return d.innerHTML }
  function $(id) { return document.getElementById(id) }
  function lireJson(cle, defaut) {
    try { return JSON.parse(localStorage.getItem(cle) || 'null') || defaut } catch (e) { return defaut }
  }
  function ecrireJson(cle, valeur) {
    try { localStorage.setItem(cle, JSON.stringify(valeur)) } catch (e) {}
  }

  var prefs = window.LOCALPR_PREFS || {};
  if (!prefs.view) prefs.view = 'unified';
  if (!prefs.theme) prefs.theme = 'auto';
  if (!prefs.tab) prefs.tab = 4;
  if (!prefs.width) prefs.width = 300;
  var vus = lireJson(CLE_VUS, {});

  var minuteurPrefs = null;
  function sauvePrefs() {
    prefs.at = maintenant();
    ecrireJson(CLE_PREFS, prefs);
    if (!server) return;
    clearTimeout(minuteurPrefs);
    minuteurPrefs = setTimeout(function () {
      // A pref lost on the way is not worth the fallback banner: only comments are.
      post('/prefs', prefs).catch(function () {});
    }, 500);
  }

  function poseTheme(t) {
    prefs.theme = t;
    if (t === 'auto') document.documentElement.removeAttribute('data-theme');
    else document.documentElement.setAttribute('data-theme', t);
    $('opt-theme').value = t;
    sauvePrefs();
  }

  function poseWrap(on) {
    prefs.wrap = !!on;
    document.body.classList.toggle('soft-wrap', prefs.wrap);
    $('opt-wrap').checked = prefs.wrap;
    sauvePrefs();
  }

  function poseTab(n) {
    prefs.tab = +n || 4;
    document.body.style.setProperty('--tab', prefs.tab);
    $('opt-tab').value = String(prefs.tab);
    sauvePrefs();
  }

  function poseLargeur(px) {
    prefs.width = Math.max(180, Math.min(560, px));
    $('sidebar').style.width = prefs.width + 'px';
    sauvePrefs();
  }

  function poseSidebar(replie) {
    prefs.collapsed = !!replie;
    var s = $('sidebar');
    s.classList.toggle('collapsed', prefs.collapsed);
    s.style.width = prefs.collapsed ? '' : prefs.width + 'px';
    sauvePrefs();
  }

  var onglet = 'files';
  function poseOnglet(nom) {
    onglet = nom;
    var conv = nom === 'conversation';
    $('pane-conversation').hidden = !conv;
    $('pane-files').hidden = conv;
    $('tab-conversation').setAttribute('aria-selected', String(conv));
    $('tab-files').setAttribute('aria-selected', String(!conv));
    document.body.classList.toggle('on-conversation', conv);
  }

  var gabarit = document.createElement('template');

  function coloriser(section) {
    if (section.dataset.colorise || !section.dataset.language || !window.Render) return;
    section.dataset.colorise = '1';
    var hl = Render.highlighterFor(section.dataset.language);
    section.querySelectorAll('tr.commentable .line-code').forEach(function (td) {
      var n = td.lastChild;
      if (!n || n.nodeType !== 3) return;
      gabarit.innerHTML = hl(n.textContent);
      n.replaceWith(gabarit.content);
    });
  }

  function texteDe(td) {
    var out = '';
    for (var n = td.firstChild; n; n = n.nextSibling) {
      if (n.nodeType === 1 && n.classList.contains('marker')) continue;
      out += n.textContent;
    }
    return out;
  }

  /* The model is read once from the rendered unified table, colorised: the split view is
     rebuilt from it, and a comment anchor stays computable whichever view is showing. */
  function modele(section) {
    if (section._rows) return section._rows;
    coloriser(section);
    var rows = [], hunk = null;
    section.querySelectorAll('.diff-table > tbody > tr').forEach(function (tr) {
      if (tr.classList.contains('hunk')) {
        hunk = tr.firstElementChild.dataset.hunk;
        rows.push({ t: 'hunk', html: tr.firstElementChild.innerHTML, brut: hunk });
        return;
      }
      if (tr.classList.contains('nonl-row')) { rows.push({ t: 'nonl' }); return }
      if (!tr.classList.contains('commentable')) return;
      var tds = tr.children, code = tds[2];
      rows.push({
        t: tr.classList.contains('add') ? 'add' : tr.classList.contains('del') ? 'del' : 'ctx',
        before: tds[0].dataset.line || tds[0].textContent.trim(),
        after: tds[1].dataset.line || '',
        html: code.innerHTML,
        text: texteDe(code),
        hunk: hunk
      });
    });
    section._rows = rows;
    return rows;
  }

  function celluleNum(cls, side, line) {
    var attrs = line ? ' data-side="' + side + '" data-line="' + line + '"' : '';
    return '<td class="line-num ' + cls + '"' + attrs + '>' + (line || '') + '</td>';
  }

  function celluleCode(cls, side, line, html) {
    var attrs = line ? ' data-side="' + side + '" data-line="' + line + '"' : '';
    return '<td class="line-code ' + cls + '"' + attrs + '>' + html + '</td>';
  }

  var VIDE = '<td class="line-num pad"></td><td class="line-code pad"></td>';

  function construireSplit(rows) {
    var out = [], dels = [], adds = [];

    function vider() {
      var n = Math.max(dels.length, adds.length);
      for (var i = 0; i < n; i++) {
        var d = dels[i], a = adds[i];
        out.push('<tr class="commentable">' +
          (d ? celluleNum('del', 'old', d.before) + celluleCode('del', 'old', d.before, d.html) : VIDE) +
          (a ? celluleNum('add', 'new', a.after) + celluleCode('add', 'new', a.after, a.html) : VIDE) +
          '</tr>');
      }
      dels = []; adds = [];
    }

    rows.forEach(function (r) {
      if (r.t === 'del') { dels.push(r); return }
      if (r.t === 'add') { adds.push(r); return }
      vider();
      if (r.t === 'hunk') {
        out.push('<tr class="hunk"><td colspan="4" data-hunk="' + esc(r.brut) + '">' + r.html +
          '</td></tr>');
        return;
      }
      if (r.t === 'nonl') {
        out.push('<tr class="nonl-row"><td colspan="4" class="nonl">no newline at end of file</td></tr>');
        return;
      }
      out.push('<tr class="ctx commentable">' +
        '<td class="line-num ctx">' + (r.before || '') + '</td>' +
        '<td class="line-code ctx">' + r.html + '</td>' +
        celluleNum('ctx', 'new', r.after) + celluleCode('ctx', 'new', r.after, r.html) +
        '</tr>');
    });
    vider();
    return out.join('');
  }

  function appliquerVue(section) {
    var table = section.querySelector('.diff-table');
    if (!table) return false;
    var estSplit = table.classList.contains('split');
    if ((prefs.view === 'split') === estSplit) return false;
    section.querySelectorAll('.thread-row,.form-row-inline').forEach(function (n) { n.remove() });
    if (prefs.view === 'split') {
      section._unified = table;
      var t = document.createElement('table');
      t.className = 'diff-table split';
      t.innerHTML = '<colgroup><col style="width:48px"><col><col style="width:48px"><col>' +
        '</colgroup><tbody>' + construireSplit(modele(section)) + '</tbody>';
      table.replaceWith(t);
    } else if (section._unified) {
      table.replaceWith(section._unified);
    }
    return true;
  }

  /* Colorising and the split rebuild are paid per file, on the DOM of a page that already holds
     every line of the review: done for all of them at load, the browser spends its time there
     instead of scrolling. Only what comes near the viewport is prepared. */
  var sections = [], visibles = new Set(), prepares = new Set();

  function preparer(section) {
    if (section.classList.contains('collapsed')) { prepares.delete(section); return }
    prepares.add(section);
    coloriser(section);
    if (appliquerVue(section) && state) rendreFilsDe(section);
  }

  function poseVue(vue) {
    prefs.view = vue;
    $('view-split').setAttribute('aria-pressed', String(vue === 'split'));
    $('view-unified').setAttribute('aria-pressed', String(vue === 'unified'));
    prepares.forEach(function (s) { coloriser(s); appliquerVue(s) });
    sauvePrefs();
    rendreFils();
  }

  function comptes() {
    var par = {};
    state.comments.forEach(function (c) {
      var k = String(c.fichier_index);
      par[k] = (par[k] || 0) + 1;
    });
    return par;
  }

  function majCompteurs() {
    var par = comptes();
    document.querySelectorAll('.tree-file').forEach(function (b) {
      var n = par[b.dataset.f] || 0, c = b.querySelector('.tree-badge');
      if (n && !c) {
        c = document.createElement('span');
        c.className = 'tree-badge';
        c.innerHTML = '<svg class="octicon" width="12" height="12" viewBox="0 0 16 16">' +
          '<path d="M2 3.25C2 2.56 2.56 2 3.25 2h9.5c.69 0 1.25.56 1.25 1.25v6.5c0 .69-.56' +
          ' 1.25-1.25 1.25H8l-3.5 3v-3H3.25C2.56 11 2 10.44 2 9.75Z" fill="none"' +
          ' stroke="currentColor" stroke-width="1.4"/></svg><b></b>';
        b.querySelector('.tree-stat').before(c);
      }
      if (c) { c.querySelector('b').textContent = n || ''; c.hidden = !n }
    });
    document.querySelectorAll('.file-diff').forEach(function (s) {
      var n = par[s.dataset.f] || 0, c = s.querySelector('.thread-compteur');
      if (c) { c.textContent = n; c.hidden = !n }
    });
    rendreTracker();
  }

  function etatDe(c) {
    var rep = D.replies[c.id];
    if (!rep) return 'open';
    return rep.verdict === 'fixed' ? 'done' : 'replied';
  }

  function rendreTracker() {
    var liste = $('tracker-list');
    $('tracker').hidden = state.comments.length === 0;
    var compte = { open: 0, replied: 0, done: 0 };
    liste.innerHTML = state.comments.map(function (c) {
      var e = etatDe(c);
      compte[e]++;
      var ou = c.file ? c.file + (c.line ? ':' + c.line : '') : 'global';
      return '<button class="tracker-item ' + e + '" data-goto="' + esc(c.id) + '">' +
        '<span class="tracker-line"><span class="tracker-dot ' + e + '"></span>' +
        '<span class="tracker-file">' + esc(ou) + '</span></span>' +
        '<span class="tracker-body">' + esc(c.body) + '</span></button>';
    }).join('');
    $('ct-open').textContent = compte.open + ' open';
    $('ct-replied').textContent = compte.replied + ' replied';
    $('ct-done').textContent = compte.done + ' done';
    $('conversation-empty').hidden = state.comments.length > 0;
    $('tab-count').hidden = state.comments.length === 0;
    $('tab-count').textContent = compte.open + ' open';
  }

  function majProgres() {
    var total = D.files.length, n = 0;
    D.files.forEach(function (f) { if (vus[f.path]) n++ });
    $('progress-text').textContent = n + '/' + total + ' viewed';
    $('progress-fill').style.width = total ? Math.round(100 * n / total) + '%' : '0';
  }

  function appliquerVu(section, on) {
    section.classList.toggle('viewed', on);
    section.classList.toggle('collapsed', on);
    var boite = section.querySelector('[data-viewed]');
    if (boite) boite.checked = on;
    var ligne = ligneArbre(section);
    if (ligne) ligne.classList.toggle('viewed', on);
    if (on) prepares.delete(section); else preparer(section);
  }

  function poseVu(section, on) {
    var path = section.dataset.path;
    if (on) vus[path] = 1; else delete vus[path];
    ecrireJson(CLE_VUS, vus);
    appliquerVu(section, on);
    majProgres();
  }

  function corpsFil(c, readonly) {
    var rep = D.replies[c.id], sev = c.origin ? 'suggestion' : (SEVS[c.type] || 'nitpick');
    var h = '<div class="thread" id="thread-' + esc(c.id) + '"><div class="thread-comment">';
    h += '<div class="comment-head"><span class="who">' + (c.origin ? esc(c.origin.tool) : 'you') + '</span>';
    h += '<span class="sev-tag" data-sev="' + sev + '">' + esc(LIBS[c.type] || c.type) + '</span>';
    if (c.origin && c.origin.severity) h += '<span class="badge-outline">' + esc(c.origin.severity) + '</span>';
    if (c.origin && c.origin.state === 'dropped') h += '<span class="badge-outline">dropped below threshold</span>';
    if (c.side === 'old') h += '<span class="badge-outline">deleted line</span>';
    if (c.scope === 'file') h += '<span class="badge-outline">whole file</span>';
    if (c.scope === 'global') h += '<span class="badge-outline">global scope</span>';
    h += '<span style="margin-left:auto" class="mono">' + esc(c.id) + '</span></div>';
    h += '<div class="comment-body md">' + Render.markdown(c.body) + '</div>';
    if (rep) {
      var ko = rep.verdict && rep.verdict !== 'fixed';
      h += '<div class="response' + (ko ? ' ko' : '') + '" style="margin:0 12px 12px"><b>' +
        esc(rep.verdict) + '</b> — ' + esc(rep.response) + '</div>';
    }
    h += '<div class="thread-actions">';
    h += readonly ? '<button data-rep="' + esc(c.id) + '">take up this finding</button>'
                  : '<button data-sup="' + esc(c.id) + '">delete</button>';
    h += '</div></div></div>';
    return h;
  }

  function cible(section, c) {
    if (c.scope !== 'line' || c.line == null) return null;
    var td = section.querySelector('.line-code[data-side="' + (c.side === 'old' ? 'old' : 'new') +
      '"][data-line="' + c.line + '"]');
    return td ? td.parentNode : null;
  }

  function colonnes(section) {
    var t = section.querySelector('.diff-table');
    return t && t.classList.contains('split') ? 4 : 3;
  }

  function lectureSeule(c) {
    return !!c.origin && String(c.id).charAt(0) === 'F';
  }

  function affiches() {
    var repris = {};
    state.comments.forEach(function (c) { if (c.reprisDe) repris[c.reprisDe] = 1 });
    return state.comments.concat(D.findings.filter(function (f) { return !repris[f.id] }));
  }

  function insererFil(section, c) {
    var ligne = '<tr class="thread-row"><td colspan="' + colonnes(section) + '">' +
      corpsFil(c, lectureSeule(c)) + '</td></tr>';
    var tr = cible(section, c);
    if (tr) {
      tr.insertAdjacentHTML('afterend', ligne);
      tr.classList.add('has-thread');
      return true;
    }
    var tb = section.querySelector('.diff-table tbody');
    if (!tb) return false;
    tb.insertAdjacentHTML('afterbegin', ligne);
    return true;
  }

  /* A section rebuilt into the other view loses the rows its threads sat in: with the rebuild
     deferred to the scroll, re-rendering the whole review there would cost every file. */
  function rendreFilsDe(section) {
    section.querySelectorAll('.thread-row,.form-row-inline').forEach(function (n) { n.remove() });
    section.querySelectorAll('tr.has-thread').forEach(function (n) { n.classList.remove('has-thread') });
    var idx = section.dataset.f;
    affiches().forEach(function (c) {
      if (String(c.fichier_index) === idx) insererFil(section, c);
    });
  }

  function sectionDe(c) {
    return c.fichier_index === null || c.fichier_index === undefined ? null
      : document.querySelector('.file-diff[data-f="' + c.fichier_index + '"]');
  }

  function rendreFils() {
    var liste = affiches();
    liste.forEach(function (c) {
      var section = sectionDe(c);
      if (section && section.classList.contains('collapsed')) {
        section.classList.remove('collapsed');
        preparer(section);
      }
    });
    document.querySelectorAll('.thread-row,.form-row-inline').forEach(function (n) { n.remove() });
    document.querySelectorAll('tr.has-thread').forEach(function (n) { n.classList.remove('has-thread') });
    $('globaux').innerHTML = '';
    var globaux = [];

    liste.forEach(function (c) {
      var section = sectionDe(c);
      if (!section || !insererFil(section, c)) globaux.push(corpsFil(c, lectureSeule(c)));
    });
    $('globaux').innerHTML = globaux.join('');
    majCompteurs();
  }

  var formulaire = null;

  function closeForm() {
    if (formulaire) { formulaire.remove(); formulaire = null }
  }

  function champs(initial) {
    seq++;
    var opts = ['fix', 'followUp', 'workflowNote'].map(function (t, i) {
      var id = 'sev' + seq + '-' + i;
      return '<input type="radio" name="sev' + seq + '" id="' + id + '" value="' + t + '"' +
        (i === 0 ? ' checked' : '') + '><label for="' + id + '" data-sev="' + SEVS[t] + '">' +
        LIBS[t] + '</label>';
    }).join('');
    return '<div class="thread"><div class="thread-form">' +
      '<textarea class="input" placeholder="what is wrong, and what to do about it"></textarea>' +
      '<div class="form-row"><span class="severity">' + opts + '</span>' +
      '<span class="spacer"></span><span class="diffstat-text" style="font-size:12px">' +
      esc(initial) + '</span>' +
      '<button class="btn btn-sm" data-cancel="1">cancel</button>' +
      '<button class="btn btn-sm btn-primary" data-ok="1">Submit</button></div></div></div>';
  }

  function openForm(target, inTable, createWith, initial) {
    closeForm();
    cacherAjout();
    var cols = inTable ? colonnes(target.closest('.file-diff')) : 0;
    var html = inTable
      ? '<tr class="form-row-inline"><td colspan="' + cols + '">' + champs(initial) + '</td></tr>'
      : '<div class="form-row-inline">' + champs(initial) + '</div>';
    target.insertAdjacentHTML('afterend', html);
    formulaire = target.nextElementSibling;
    var zone = formulaire.querySelector('textarea');
    zone.focus();
    formulaire.querySelector('[data-cancel]').addEventListener('click', closeForm);
    formulaire.querySelector('[data-ok]').addEventListener('click', function () {
      var text = zone.value.trim();
      if (!text) { zone.focus(); return }
      var type = formulaire.querySelector('input[type=radio]:checked').value;
      closeForm();
      createWith(type, text);
    });
    zone.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') closeForm();
      if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) formulaire.querySelector('[data-ok]').click();
    });
  }

  function fenetre(section, side, line) {
    var rows = modele(section).filter(function (r) { return r.t !== 'hunk' && r.t !== 'nonl' });
    var i = -1, cle = String(line);
    for (var k = 0; k < rows.length; k++) {
      var r = rows[k];
      var ok = side === 'old' ? (r.t !== 'add' && r.before === cle) : (r.t !== 'del' && r.after === cle);
      if (ok) { i = k; break }
    }
    if (i < 0) return { anchor: null, offset: null, hunk: null };
    var lines = [], offset = 0;
    for (var j = Math.max(0, i - 2); j <= Math.min(rows.length - 1, i + 2); j++) {
      if (j === i) offset = lines.length;
      lines.push(rows[j].text);
    }
    return { anchor: lines.join('\n'), offset: offset, hunk: rows[i].hunk };
  }

  function ajouter(c) {
    state.n = (state.n || 0) + 1;
    c.id = 'C' + state.n;
    c.state = 'open';
    c.deposeA = maintenant();
    state.comments.push(c);
    enregistrer();
    rendreFils();
  }

  function creerLigne(section, side, line, type, body) {
    var idx = +section.dataset.f, f = D.files[idx], a = fenetre(section, side, line);
    ajouter({
      scope: 'line', type: type, side: side,
      file: f.path, fichier_index: idx,
      line: +line, lineEnd: null,
      hunk: a.hunk,
      anchor: a.anchor, anchorOffset: a.offset, fingerprint: f.fingerprint,
      body: body, origin: null
    });
  }

  function ecrireLocal() { ecrireJson(CLE, state) }

  function post(route, body) {
    return fetch(route, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Localpr-Token': D.token || '' },
      body: JSON.stringify(body || {})
    }).then(function (r) { if (!r.ok) throw new Error(r.status); return r });
  }

  function setStatus(text, sev) {
    var e = $('state-enreg');
    e.innerHTML = text ? '<span class="sev-tag" data-sev="' + sev + '">' + esc(text) + '</span>' : '';
  }

  function switchToFallback(raison) {
    server = false;
    $('fallback').hidden = false;
    setStatus(raison, 'nitpick');
    majJson();
  }

  function majJson() {
    var z = $('json');
    if (z) z.value = JSON.stringify(state, null, 2);
  }

  function enregistrer() {
    state.updated = maintenant();
    ecrireLocal();
    majCompteurs();
    majJson();
    if (!server) return;
    post('/comments', state).catch(function () {
      switchToFallback('server unreachable - comments kept locally');
    });
  }

  var lignesArbre = null, fichierActif = null;

  function ligneArbre(section) {
    if (!lignesArbre) {
      lignesArbre = {};
      document.querySelectorAll('.tree-file').forEach(function (b) { lignesArbre[b.dataset.f] = b });
    }
    return lignesArbre[section.dataset.f] || null;
  }

  /* Scrolling calls this on every tick: sweeping the sidebar and scrolling it into view when
     nothing moved forces a layout of a document that holds the whole review. */
  function activerFichier(section, defiler) {
    if (section !== fichierActif) {
      var ancienne = fichierActif && ligneArbre(fichierActif);
      if (ancienne) ancienne.classList.remove('active');
      fichierActif = section;
      var ligne = ligneArbre(section);
      if (ligne) {
        ligne.classList.add('active');
        ligne.scrollIntoView({ block: 'nearest', behavior: 'instant' });
      }
    }
    if (defiler) section.scrollIntoView({ block: 'start', behavior: 'instant' });
  }

  function filtrer(q) {
    var terme = q.trim().toLowerCase();
    var visibles = 0;
    document.querySelectorAll('.tree-file').forEach(function (b) {
      var ok = !terme || b.dataset.path.toLowerCase().indexOf(terme) >= 0;
      b.hidden = !ok;
      if (ok) visibles++;
    });
    var groupes = Array.prototype.slice.call(document.querySelectorAll('.tree-group')).reverse();
    groupes.forEach(function (g) {
      var vide = !g.querySelector('.tree-file:not([hidden]), .tree-group:not([hidden])');
      g.hidden = vide;
      var tete = document.querySelector('.tree-dir[data-group="' + g.id + '"]');
      if (tete) {
        tete.hidden = vide;
        if (terme) tete.classList.remove('closed');
        else tete.classList.toggle('closed', !!tete.dataset.closed);
        if (!terme && tete.dataset.closed) g.hidden = true;
      }
    });
    $('tree-empty').hidden = visibles > 0;
  }

  /* Parked in the page, moved by transform: inserting it into a cell dirties the layout of a
     table holding thousands of rows, and the browser replays it at every hover. */
  var boutonAjout = document.createElement('button');
  boutonAjout.type = 'button';
  boutonAjout.className = 'add-comment-btn off';
  boutonAjout.textContent = '+';
  boutonAjout.title = 'Add a comment on this line';
  boutonAjout.celluleAncree = null;
  document.body.appendChild(boutonAjout);

  /* Hiding it by `hidden` would drop its box out of the layout tree, and putting it back costs a
     prepaint walk of the whole review at every hover: it only ever moves, by transform. */
  function cacherAjout() {
    if (!boutonAjout.celluleAncree) return;
    boutonAjout.classList.add('off');
    boutonAjout.celluleAncree = null;
  }

  function replacerAjout() {
    var cellule = boutonAjout.celluleAncree;
    if (!cellule) return;
    var r = cellule.getBoundingClientRect(), zone = $('main').getBoundingClientRect();
    if (r.bottom <= zone.top || r.top >= zone.bottom) { cacherAjout(); return }
    boutonAjout.style.transform = 'translate(' + (r.left + 2) + 'px,' + (r.top + 1) + 'px)';
  }

  function ancrerAjout(cellule) {
    if (boutonAjout.celluleAncree === cellule) return;
    boutonAjout.celluleAncree = cellule;
    replacerAjout();
    boutonAjout.classList.remove('off');
  }

  document.addEventListener('mouseover', function (e) {
    var cellule = e.target.closest('.line-num[data-line]');
    if (!cellule) {
      var code = e.target.closest('.line-code[data-line]');
      if (code) {
        cellule = code.parentNode.querySelector('.line-num[data-side="' + code.dataset.side +
                                                '"][data-line="' + code.dataset.line + '"]');
      }
    }
    if (cellule) ancrerAjout(cellule); else if (e.target !== boutonAjout) cacherAjout();
  });

  var replacementPrevu = false;
  $('main').addEventListener('scroll', function () {
    if (!boutonAjout.celluleAncree || replacementPrevu) return;
    replacementPrevu = true;
    requestAnimationFrame(function () { replacementPrevu = false; replacerAjout() });
  }, { passive: true });

  document.addEventListener('click', function (e) {
    var menu = $('settings-menu');
    if (!e.target.closest('.menu-wrap') && !menu.hidden) {
      menu.hidden = true;
      $('settings').setAttribute('aria-expanded', 'false');
    }

    var chev = e.target.closest('.chevron');
    if (chev) {
      var s = chev.closest('.file-diff');
      s.classList.toggle('collapsed');
      preparer(s);
      return;
    }

    var dir = e.target.closest('.tree-dir');
    if (dir) {
      var ferme = !dir.classList.contains('closed');
      dir.classList.toggle('closed', ferme);
      if (ferme) dir.dataset.closed = '1'; else delete dir.dataset.closed;
      $(dir.dataset.group).hidden = ferme;
      return;
    }

    var tf = e.target.closest('.tree-file');
    if (tf) {
      var target = $(tf.dataset.target);
      if (target) {
        target.classList.remove('collapsed');
        preparer(target);
        activerFichier(target, true);
      }
      return;
    }

    var vers = e.target.closest('[data-goto]');
    if (vers) {
      poseOnglet('files');
      var fil = $('thread-' + vers.dataset.goto);
      if (fil) {
        var sec = fil.closest('.file-diff');
        if (sec) { sec.classList.remove('collapsed'); preparer(sec) }
        fil.scrollIntoView({ block: 'center' });
      }
      return;
    }

    var sup = e.target.closest('[data-sup]');
    if (sup) {
      state.comments = state.comments.filter(function (c) { return c.id !== sup.dataset.sup });
      enregistrer(); rendreFils(); return;
    }

    var rep = e.target.closest('[data-rep]');
    if (rep) {
      var trouve = D.findings.filter(function (x) { return x.id === rep.dataset.rep })[0];
      if (trouve) {
        var c = JSON.parse(JSON.stringify(trouve));
        c.reprisDe = trouve.id;
        c.type = 'fix';
        ajouter(c);
      }
      return;
    }

    var cf = e.target.closest('[data-comment-file]');
    if (cf) {
      var sec = cf.closest('.file-diff');
      sec.classList.remove('collapsed');
      preparer(sec);
      openForm(sec.querySelector('.file-diff-head'), false, function (t, body) {
        var idx = +sec.dataset.f;
        ajouter({ scope: 'file', type: t, side: 'new', file: D.files[idx].path,
                  fichier_index: idx, line: null, lineEnd: null, anchor: null, anchorOffset: null,
                  fingerprint: D.files[idx].fingerprint, body: body, origin: null });
      }, 'comment on ' + sec.dataset.path);
      return;
    }

    if (e.target.closest('.thread') || e.target.closest('.form-row-inline')) return;

    if (e.target === boutonAjout) {
      var cellule = boutonAjout.celluleAncree;
      if (!cellule) return;
      var section = cellule.closest('.file-diff');
      var side = cellule.dataset.side, line = cellule.dataset.line;
      openForm(cellule.parentNode, true, function (t, body) {
        creerLigne(section, side, line, t, body);
      }, side === 'old' ? 'deleted line ' + line + ' - not re-anchorable, the hunk travels with it'
                        : 'line ' + line + ' of the new file');
    }
  });

  document.addEventListener('change', function (e) {
    var vu = e.target.closest('[data-viewed]');
    if (vu) poseVu(vu.closest('.file-diff'), vu.checked);
  });

  $('settings').addEventListener('click', function () {
    var menu = $('settings-menu');
    menu.hidden = !menu.hidden;
    $('settings').setAttribute('aria-expanded', String(!menu.hidden));
  });
  $('opt-wrap').addEventListener('change', function (e) { poseWrap(e.target.checked) });
  $('opt-tab').addEventListener('change', function (e) { poseTab(e.target.value) });
  $('opt-theme').addEventListener('change', function (e) { poseTheme(e.target.value) });
  $('opt-expand').addEventListener('click', function () {
    sections.forEach(function (s) { s.classList.remove('collapsed') });
    visibles.forEach(preparer);
  });
  $('opt-collapse').addEventListener('click', function () {
    sections.forEach(function (s) { s.classList.add('collapsed'); prepares.delete(s) });
  });
  $('view-split').addEventListener('click', function () { poseVue('split') });
  $('view-unified').addEventListener('click', function () { poseVue('unified') });
  $('sidebar-toggle').addEventListener('click', function () { poseSidebar(!prefs.collapsed) });
  $('tab-conversation').addEventListener('click', function () { poseOnglet('conversation') });
  $('tab-files').addEventListener('click', function () { poseOnglet('files') });
  $('filter').addEventListener('input', function (e) { filtrer(e.target.value) });

  (function resize() {
    var handle = $('resizer'), actif = false;
    handle.addEventListener('mousedown', function (e) {
      actif = true;
      handle.classList.add('dragging');
      document.body.style.userSelect = 'none';
      e.preventDefault();
    });
    document.addEventListener('mousemove', function (e) {
      if (!actif) return;
      poseLargeur(e.clientX - $('sidebar').getBoundingClientRect().left);
    });
    document.addEventListener('mouseup', function () {
      actif = false;
      handle.classList.remove('dragging');
      document.body.style.userSelect = '';
    });
  })();

  document.addEventListener('keydown', function (e) {
    var champ = /^(INPUT|TEXTAREA|SELECT)$/.test((e.target.tagName || '').toUpperCase());
    if (e.key === 'Escape') {
      closeForm();
      $('settings-menu').hidden = true;
      if (champ && e.target.id === 'filter') { e.target.value = ''; filtrer(''); e.target.blur() }
      return;
    }
    if (champ || e.metaKey || e.ctrlKey || e.altKey || onglet !== 'files') return;
    if (e.key === '/') { e.preventDefault(); $('filter').focus(); return }
    if (e.key === 'j' || e.key === 'k') {
      var courant = fichierActif ? fichierActif._i : 0;
      var suivant = sections[Math.max(0, Math.min(sections.length - 1, courant + (e.key === 'j' ? 1 : -1)))];
      if (suivant) { preparer(suivant); activerFichier(suivant, true) }
    }
  });

  $('global').addEventListener('click', function () {
    openForm($('globaux'), false, function (t, body) {
      ajouter({ scope: 'global', type: t, side: null, file: null, fichier_index: null,
                line: null, lineEnd: null, anchor: null, anchorOffset: null, fingerprint: null,
                body: body, origin: null });
    }, 'global-scope comment, handled separately');
  });

  $('copyjson').addEventListener('click', function () {
    var z = $('json'), b = $('copyjson');
    z.hidden = false;
    z.select();
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(z.value).then(function () { b.textContent = 'copied' },
        function () { b.textContent = 'press Ctrl+C to copy' });
    } else b.textContent = 'press Ctrl+C to copy';
  });

  var bt = $('terminer');
  bt.addEventListener('click', function () {
    if (!server) { switchToFallback('no server: copy the JSON and paste it into the chat'); return }
    post('/done', state).then(function () {
      setStatus('review sent — ' + state.comments.length + ' comment(s)', 'praise');
      bt.disabled = true;
    }).catch(function () { switchToFallback('server unreachable - comments kept locally') });
  });

  var br = $('regen');
  br.addEventListener('click', function () {
    if (!server) { switchToFallback('server unreachable - cannot regenerate'); return }
    br.disabled = true;
    post('/regenerate', {}).then(function () { location.reload() })
      .catch(function () { br.disabled = false; switchToFallback('server unreachable - comments kept locally') });
  });

  state = D.comments && D.comments.comments
    ? D.comments : { version: 1, updated: null, n: 0, comments: [] };
  var local = lireJson(CLE, null);
  if (local && local.comments && instant(local.updated) > instant(state.updated)) state = local;

  poseTheme(prefs.theme);
  poseWrap(prefs.wrap);
  poseTab(prefs.tab);
  poseSidebar(prefs.collapsed);
  poseOnglet('files');
  $('view-split').setAttribute('aria-pressed', String(prefs.view === 'split'));
  $('view-unified').setAttribute('aria-pressed', String(prefs.view === 'unified'));

  sections = Array.prototype.slice.call(document.querySelectorAll('.file-diff'));
  sections.forEach(function (s, i) {
    s._i = i;
    if (vus[s.dataset.path]) appliquerVu(s, true);
  });
  majProgres();
  rendreFils();
  majJson();

  (function suivreDefilement() {
    if (!sections.length) return;
    if (!window.IntersectionObserver) { sections.forEach(preparer); return }

    var proches = new IntersectionObserver(function (entries) {
      entries.forEach(function (x) {
        if (!x.isIntersecting) { visibles.delete(x.target); return }
        visibles.add(x.target);
        preparer(x.target);
      });
    }, { root: $('main'), rootMargin: '1500px 0px' });
    sections.forEach(function (s) { proches.observe(s) });

    var vues = new Set();
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (x) {
        if (x.isIntersecting) vues.add(x.target); else vues.delete(x.target);
      });
      var haut = null;
      vues.forEach(function (s) { if (!haut || s._i < haut._i) haut = s });
      if (haut) activerFichier(haut, false);
    }, { root: $('main'), rootMargin: '0px 0px -70% 0px' });
    sections.forEach(function (s) { io.observe(s) });
  })();

  function heartbeat() {
    if (!server) return;
    post('/ping', {}).catch(function () {
      switchToFallback('server stopped - comments kept locally');
    });
  }

  post('/ping', {}).then(function () {
    server = true;
    setInterval(heartbeat, 30000);
    setStatus('saved on the server', 'praise');
    $('fallback').hidden = true;
    if (state.updated && instant(state.updated) > instant(D.comments && D.comments.updated)) enregistrer();
  }).catch(function () {
    switchToFallback(D.token ? 'server unreachable - comments kept locally'
                             : 'page opened without a server - comments kept locally');
  });

  if (location.hash) {
    var c = document.querySelector(location.hash.replace(/[^#\w:.-]/g, ''));
    if (c) {
      var s = c.closest('.file-diff');
      if (s) { s.classList.remove('collapsed'); preparer(s) }
      c.classList.add('has-thread');
      c.scrollIntoView({ block: 'center' });
    }
  }
})();
"""


def esc(x):
    return html.escape(str(x if x is not None else ""))


LANGUAGES = {
    ".php": "php", ".phtml": "php",
    ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript", ".jsx": "javascript",
    ".ts": "javascript", ".tsx": "javascript",
    ".py": "python", ".pyi": "python",
    ".go": "go", ".rs": "rust",
    ".java": "java", ".kt": "java", ".kts": "java", ".groovy": "java",
    ".c": "c", ".h": "c", ".cpp": "c", ".hpp": "c", ".cc": "c", ".cs": "c",
    ".sql": "sql",
    ".sh": "shell", ".bash": "shell", ".zsh": "shell", ".bats": "shell",
    ".json": "json", ".jsonc": "json",
    ".yaml": "yaml", ".yml": "yaml", ".neon": "yaml", ".dist": "yaml", ".lock": "yaml",
    ".ini": "ini", ".toml": "ini", ".cfg": "ini", ".env": "ini", ".conf": "ini",
    ".css": "css", ".scss": "css", ".sass": "css", ".less": "css",
    ".html": "markup", ".htm": "markup", ".xml": "markup", ".svg": "markup",
    ".twig": "markup", ".vue": "markup", ".xsd": "markup",
    ".md": "markdown", ".markdown": "markdown",
}
FILENAME_LANGUAGES = {
    "Dockerfile": "shell", "Makefile": "shell", "Jenkinsfile": "java",
    ".gitignore": "ini", ".env": "ini", ".editorconfig": "ini",
}


def octicon(body, size=16, cls=""):
    return (f'<svg class="octicon{" " + cls if cls else ""}" width="{size}" height="{size}"'
            f' viewBox="0 0 16 16" aria-hidden="true">{body}</svg>')


CHEVRON = octicon('<path d="M12.78 6.22a.75.75 0 0 1 0 1.06l-4.25 4.25a.75.75 0 0 1-1.06 0L3.22'
                  ' 7.28a.75.75 0 0 1 1.06-1.06L8 9.94l3.72-3.72a.75.75 0 0 1 1.06 0Z"/>')
CHEVRON_MINI = octicon('<path d="M12.78 6.22a.75.75 0 0 1 0 1.06l-4.25 4.25a.75.75 0 0 1-1.06'
                       ' 0L3.22 7.28a.75.75 0 0 1 1.06-1.06L8 9.94l3.72-3.72a.75.75 0 0 1 1.06'
                       ' 0Z"/>', 12, "chevron-mini")
ICON_FOLDER = octicon('<path d="M1.75 3.5h3.4l1.3 1.6h7.8c.41 0 .75.34.75.75v6.65c0 .41-.34.75-.75'
                      '.75H1.75a.75.75 0 0 1-.75-.75V4.25c0-.41.34-.75.75-.75Z" fill="none"'
                      ' stroke="currentColor" stroke-width="1.3"/>')
ICON_SEARCH = octicon('<path d="M11.5 7a4.5 4.5 0 1 1-9 0 4.5 4.5 0 0 1 9 0Zm-1.3 3.9L14 14.5"'
                      ' fill="none" stroke="currentColor" stroke-width="1.5"'
                      ' stroke-linecap="round"/>')
ICON_BRANCH = octicon('<path d="M4 3.5v9M4 3.5a1.5 1.5 0 1 0 0-.01ZM4 12.5a1.5 1.5 0 1 0 0-.01Z'
                      'M12 5.5a1.5 1.5 0 1 0 0-.01ZM12 7v.5A2.5 2.5 0 0 1 9.5 10H6.5"'
                      ' fill="none" stroke="currentColor" stroke-width="1.5"'
                      ' stroke-linecap="round"/>')
ICON_COMMENT = octicon('<path d="M2 3.25C2 2.56 2.56 2 3.25 2h9.5c.69 0 1.25.56 1.25 1.25v6.5c0'
                       ' .69-.56 1.25-1.25 1.25H8l-3.5 3v-3H3.25C2.56 11 2 10.44 2 9.75Z"'
                       ' fill="none" stroke="currentColor" stroke-width="1.3"/>', 14)
ICON_GEAR = octicon('<path d="M8 10.5a2.5 2.5 0 1 0 0-5 2.5 2.5 0 0 0 0 5Z" fill="none"'
                    ' stroke="currentColor" stroke-width="1.3"/><path d="M8 1.5 9 3.3l2-.4.6 2'
                    ' 1.8 1-1 1.8 1 1.8-1.8 1-.6 2-2-.4-1 1.8-1-1.8-2 .4-.6-2-1.8-1 1-1.8-1-1.8'
                    ' 1.8-1 .6-2 2 .4Z" fill="none" stroke="currentColor" stroke-width="1.1"'
                    ' stroke-linejoin="round"/>')
ICON_PANEL = octicon('<path d="M2.25 2.5h11.5c.41 0 .75.34.75.75v9.5c0 .41-.34.75-.75.75H2.25a.75'
                     '.75 0 0 1-.75-.75v-9.5c0-.41.34-.75.75-.75ZM6 2.5v11" fill="none"'
                     ' stroke="currentColor" stroke-width="1.3"/>')

BOX = ('<rect x="2.3" y="2.3" width="11.4" height="11.4" rx="2.6" fill="none"'
       ' stroke="currentColor" stroke-width="1.3"/>')
STROKE = 'fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"'
STATUS_ICON = {
    "added": octicon(BOX + f'<path d="M8 5.3v5.4M5.3 8h5.4" {STROKE}/>'),
    "suppression": octicon(BOX + f'<path d="M5.3 8h5.4" {STROKE}/>'),
    "modif": octicon(BOX + '<circle cx="8" cy="8" r="1.9" fill="currentColor"/>'),
    "renommage": octicon(BOX + f'<path d="M5.2 8h5.2M8.4 6l2 2-2 2" {STROKE}/>'),
    "binaire": octicon(BOX + f'<path d="M5.6 6.2h4.8M5.6 9.8h4.8" {STROKE}/>'),
    "mode": octicon(BOX + f'<path d="M6.2 8h3.6" {STROKE}/>'),
    "sousmodule": octicon(BOX + '<circle cx="8" cy="8" r="1.9" fill="none"'
                                ' stroke="currentColor" stroke-width="1.3"/>'),
}
STATUS_LABEL = {
    "added": "added", "suppression": "deleted", "renommage": "renamed",
    "binaire": "binary", "mode": "mode change", "sousmodule": "submodule",
}


def status_icon(status):
    return STATUS_ICON.get(status, STATUS_ICON["modif"])


def language_of(path):
    name = path.rsplit("/", 1)[-1]
    if name in FILENAME_LANGUAGES:
        return FILENAME_LANGUAGES[name]
    for ext, language in LANGUAGES.items():
        if name.endswith(ext):
            return language
    return ""


def fmt_num(n):
    return f"{int(n or 0):,}".replace(",", " ")


def diffstat_bar(additions, deletions):
    total = additions + deletions
    if not total:
        return ""
    verts = max(1, round(5 * additions / total)) if additions else 0
    rouges = max(1, round(5 * deletions / total)) if deletions else 0
    while verts + rouges > 5:
        if rouges >= verts:
            rouges -= 1
        else:
            verts -= 1
    cases = ['<i class="on-add"></i>'] * verts + ['<i class="on-del"></i>'] * rouges
    cases += ['<i class="off"></i>'] * (5 - len(cases))
    return f'<span class="diffstat-bar">{"".join(cases)}</span>'


def hunk_header_row(hunk):
    bouts = hunk["header"].split("@@")
    plage = f'@@{bouts[1]}@@' if len(bouts) > 2 else hunk["header"]
    section = (f'<span class="section">{esc(hunk["section"])}</span>'
               if hunk.get("section") else "")
    return (f'<tr class="hunk"><td colspan="3" data-hunk="{esc(hunk["header"])}">'
            f'<span class="range">{esc(plage)}</span>{section}</td></tr>')


def render_lines(f, hunk):
    """Cells, not rows, carry the side and the line number: the split view is rebuilt from
    them on the client, where a single row holds one line of each side."""
    out = [hunk_header_row(hunk)]
    for l in hunk["lines"]:
        kind = {"add": "add", "del": "del"}.get(l["t"], "ctx")
        marqueur = {"add": "+", "del": "-"}.get(l["t"], "")
        side = "old" if l["t"] == "del" else "new"
        line = l["before"] if side == "old" else l["after"]
        anchor = f' id="f{f["index"]}-L{l["after"]}"' if l["t"] != "del" and l["after"] else ""
        gauche = (f'<td class="line-num {kind}" data-side="old" data-line="{l["before"]}">'
                  f'{l["before"]}</td>' if l["t"] == "del"
                  else f'<td class="line-num {kind}">{l["before"] or ""}</td>')
        droite = (f'<td class="line-num {kind}" data-side="new" data-line="{l["after"]}">'
                  f'{l["after"]}</td>' if l["after"]
                  else f'<td class="line-num {kind}"></td>')
        out.append(
            f'<tr class="{kind} commentable"{anchor}>{gauche}{droite}'
            f'<td class="line-code {kind}" data-side="{side}" data-line="{line or ""}">'
            f'<span class="marker">{marqueur}</span>{esc(l["txt"])}</td></tr>'
        )
        if l.get("sansNewline"):
            out.append('<tr class="nonl-row"><td colspan="3" class="nonl">'
                       'no newline at end of file</td></tr>')
    return "".join(out)


def render_file(f, restant):
    """Expanded by default: a review exists to be read, and find-in-page returns nothing
    inside a collapsed panel. Only files past the cap and obviously generated files
    start collapsed."""
    total = sum(len(h["lines"]) for h in f["hunks"])
    replie = total > FILE_CAP or f["generated"] or restant <= 0
    body, shown, coupe = [], 0, False
    for h in f["hunks"]:
        if shown >= FILE_CAP or restant - shown <= 0:
            coupe = True
            break
        body.append(render_lines(f, h))
        shown += len(h["lines"])

    if f["note"]:
        inner = f'<div class="empty">{esc(f["note"])}</div>'
    elif not f["hunks"]:
        inner = '<div class="empty">no textual content in this diff</div>'
    else:
        inner = f'<table class="diff-table"><tbody>{"".join(body)}</tbody></table>'
        if coupe:
            inner += (f'<div class="truncated">… {fmt_num(total - shown)} more line(s), '
                      f'not shown (cap of {FILE_CAP} per file)</div>')

    # Reserved for a body not laid out yet: a stylesheet guess makes the scrollbar jump.
    hauteur = inner.count("<tr") * LINE_HEIGHT + (34 if coupe else 0) or 60

    large = max([len(str(l["after"] or l["before"] or "")) for h in f["hunks"]
                 for l in h["lines"]] or [2])

    dossier, _, nom = f["path"].rpartition("/")
    chemin = (f'<span class="dir">{esc(dossier)}/</span>' if dossier else "") + \
             f'<span class="base">{esc(nom)}</span>'

    tags = []
    if f["status"] != "modif":
        tags.append(f'<span class="badge-outline">{esc(STATUS_LABEL[f["status"]])}</span>')
    if f.get("pathBefore"):
        tags.append(f'<span class="badge-outline" title="{esc(f["pathBefore"])}">'
                    f'from {esc(f["pathBefore"].rsplit("/", 1)[-1])}</span>')
    if f["generated"]:
        tags.append('<span class="badge-outline">generated</span>')

    return (
        f'<section class="file-diff{" collapsed" if replie else ""}" id="f{f["index"]}"'
        f' data-f="{f["index"]}" data-path="{esc(f["path"])}"'
        f' data-language="{language_of(f["path"])}"'
        f' style="--num-w:{large}ch;--body-h:{hauteur}px">'
        f'<div class="file-diff-head">'
        f'<button class="chevron" aria-label="collapse or expand">{CHEVRON}</button>'
        f'<span class="st-{f["status"]}" title="{esc(STATUS_LABEL.get(f["status"], "modified"))}">'
        f'{status_icon(f["status"])}</span>'
        f'<span class="file-path">{chemin}</span>'
        + "".join(tags)
        + f'<span class="diffstat-text"><span class="add">+{f["additions"]}</span> '
        f'<span class="del">−{f["deletions"]}</span></span>'
        + diffstat_bar(f["additions"], f["deletions"])
        + '<span class="counter thread-compteur" hidden></span>'
        f'<div class="file-actions">'
        f'<button class="icon-btn" data-comment-file="1" title="comment on this file"'
        f' aria-label="comment on this file">{ICON_COMMENT}</button>'
        f'<label class="viewed-toggle"><input type="checkbox" data-viewed="1">Viewed</label>'
        f'</div></div>'
        f'<div class="file-diff-body">{inner}</div></section>'
    ), shown


def tree_nodes(files):
    racine = {"dirs": {}, "files": []}
    for f in files:
        bouts = f["path"].split("/")
        node = racine
        for part in bouts[:-1]:
            node = node["dirs"].setdefault(part, {"dirs": {}, "files": []})
        node["files"].append((bouts[-1], f))
    return racine


def render_tree_level(node, depth, compteur):
    """Single-child directories are folded into one row (src/Domain/Commission), the way a
    file browser does: PHP namespaces would otherwise cost one indentation level each."""
    out = []
    for nom in sorted(node["dirs"]):
        enfant, chemin = node["dirs"][nom], nom
        while not enfant["files"] and len(enfant["dirs"]) == 1:
            suivant = next(iter(enfant["dirs"]))
            chemin += "/" + suivant
            enfant = enfant["dirs"][suivant]
        compteur[0] += 1
        groupe = f"g{compteur[0]}"
        out.append(
            f'<button class="tree-row tree-dir" data-group="{groupe}"'
            f' style="padding-left:{8 + depth * 13}px" title="{esc(chemin)}">'
            f'{CHEVRON_MINI}{ICON_FOLDER}<span class="tree-name">{esc(chemin)}</span></button>'
            f'<div class="tree-group" id="{groupe}">'
            + render_tree_level(enfant, depth + 1, compteur)
            + "</div>"
        )
    for nom, f in sorted(node["files"], key=lambda x: x[0].lower()):
        out.append(
            f'<button class="tree-row tree-file" data-target="f{f["index"]}"'
            f' data-f="{f["index"]}" data-path="{esc(f["path"])}" title="{esc(f["path"])}"'
            f' style="padding-left:{8 + depth * 13 + 4}px">'
            f'<span class="st-{f["status"]}">{status_icon(f["status"])}</span>'
            f'<span class="tree-name">{esc(nom)}</span>'
            f'<span class="tree-stat"><span class="add">+{f["additions"]}</span> '
            f'<span class="del">−{f["deletions"]}</span></span></button>'
        )
    return "".join(out)


def render_tree(model):
    return (f'<div class="tree" id="tree">'
            + render_tree_level(tree_nodes(model["files"]), 0, [0])
            + '<div class="tree-empty" id="tree-empty" hidden>no file matches</div></div>')


def render_toolbar(model):
    t = model["totals"]
    base = esc(model["base"]) + (f' · {esc(model["head"])}' if model["head"] else "")
    return (
        '<header class="toolbar">'
        '<span class="toolbar-brand">localpr</span>'
        f'<span class="toolbar-project">{esc(model["project"])}</span>'
        f'<span class="chip">{ICON_BRANCH}{base}</span>'
        f'<span class="toolbar-stat">{fmt_num(t["files"])} file(s) changed '
        f'<span class="add">+{fmt_num(t["additions"])}</span> '
        f'<span class="del">−{fmt_num(t["deletions"])}</span></span>'
        '<span class="state-chip" id="state-enreg"></span>'
        '<span class="spacer"></span>'
        '<span class="progress" id="progress" title="files marked as viewed">'
        '<span id="progress-text">0/0</span>'
        '<span class="progress-track"><span class="progress-fill" id="progress-fill"></span>'
        '</span></span>'
        '<span class="seg" role="group" aria-label="diff layout">'
        '<button id="view-unified" aria-pressed="true">Unified</button>'
        '<button id="view-split" aria-pressed="false">Split</button></span>'
        '<span class="menu-wrap">'
        f'<button class="icon-btn" id="settings" aria-expanded="false" title="settings">'
        f'{ICON_GEAR}</button>'
        '<div class="menu" id="settings-menu" hidden>'
        '<label class="menu-item"><input type="checkbox" id="opt-wrap">Soft wrap</label>'
        '<label class="menu-item">Tab size<select id="opt-tab">'
        '<option>2</option><option selected>4</option><option>8</option></select></label>'
        '<label class="menu-item">Theme<select id="opt-theme">'
        '<option value="auto">auto</option><option value="light">light</option>'
        '<option value="dark">dark</option>'
        '<option value="dimmed">dark dimmed</option></select></label>'
        '<div class="menu-sep"></div>'
        '<div class="menu-item" id="opt-expand">Expand all files</div>'
        '<div class="menu-item" id="opt-collapse">Collapse all files</div>'
        '</div></span>'
        '<button class="btn btn-sm" id="global" title="comment on the review as a whole">'
        '+ Global</button>'
        '<button class="btn btn-sm" id="regen" title="re-collect the diff">↻</button>'
        '<button class="btn btn-sm btn-primary" id="terminer">Finish review</button>'
        '</header>'
    )


def render_tabs(model):
    return (
        '<nav class="tabs" role="tablist">'
        '<button class="tab" id="tab-conversation" role="tab" aria-selected="false"'
        ' aria-controls="pane-conversation">Conversation'
        '<span class="tab-count" id="tab-count" hidden></span></button>'
        '<button class="tab" id="tab-files" role="tab" aria-selected="true"'
        ' aria-controls="pane-files">Files changed'
        f'<span class="tab-count">{fmt_num(model["totals"]["files"])}</span></button>'
        '</nav>'
    )


def render(model, comments, findings, replies, token):
    body, restant = [], GLOBAL_CAP
    for f in model["files"]:
        html_f, shown = render_file(f, restant)
        restant -= shown
        body.append(html_f)

    donnees = {
        "repo": model["repo"], "base": model["base"], "token": token or "",
        "comments": comments, "findings": findings, "replies": replies,
        "files": [{"path": f["path"], "fingerprint": f["fingerprint"]}
                  for f in model["files"]],
    }

    css = read_asset("primer-like.css")
    style = css + auto_dark_block(css) + read_asset("review.css")
    prefs = read_prefs()

    sidebar = (
        '<aside class="sidebar" id="sidebar">'
        '<div class="sidebar-top">'
        f'<button class="icon-btn" id="sidebar-toggle" title="collapse the file list"'
        f' aria-label="collapse the file list">{ICON_PANEL}</button>'
        f'<span class="sidebar-search">{ICON_SEARCH}'
        '<input id="filter" type="search" placeholder="Filter files…"'
        ' aria-label="filter files"><kbd>/</kbd></span>'
        '</div>'
        + render_tree(model)
        + '</aside>'
    )

    conversation = (
        '<section class="pane-conversation" id="pane-conversation" role="tabpanel"'
        ' aria-labelledby="tab-conversation" hidden>'
        '<div class="conversation">'
        '<p class="conversation-empty" id="conversation-empty">No comment yet — comment on a '
        'line from the <b>Files changed</b> tab.</p>'
        '<div class="tracker" id="tracker" hidden>'
        '<div class="tracker-head">Comments<span class="tracker-counts">'
        '<span class="ct-open" id="ct-open">0 open</span>'
        '<span class="ct-replied" id="ct-replied">0 replied</span>'
        '<span class="ct-done" id="ct-done">0 done</span></span></div>'
        '<div id="tracker-list"></div></div>'
        '</div></section>'
    )

    pied = (
        '<footer class="page-foot">'
        'Click a line — or the <b>+</b> in the gutter — to comment on it · Ctrl+Enter to submit · '
        '<b>/</b> to filter, <b>j</b>/<b>k</b> to move from file to file<br>'
        'No auto-refresh: the diff is frozen at generation time, ↻ re-collects it.<br>'
        f'<span class="mono">{esc(model["repo"])}</span></footer>'
    )

    notice = (
        '<div id="fallback" class="notice" hidden><span><b>Local mode.</b> Comments are only '
        'stored in this browser. Copy the JSON and paste it into the chat to have them '
        'handled.</span><button class="btn btn-sm" id="copyjson">Copy JSON</button>'
        '<textarea class="input" id="json" readonly hidden></textarea></div>'
    )

    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<title>{esc(model["project"])} - localpr review</title>'
        f"<style>{style}</style></head><body>"
        # Before the first paint and before window.LOCALPR, or the theme flashes. The newest of
        # the two stores wins: a page opened without a server can only write localStorage.
        + '<script>window.LOCALPR_PREFS='
        + (json.dumps(prefs, ensure_ascii=False).replace("</", "<\\/") if prefs
           else "null") + ';try{'
        'var l=JSON.parse(localStorage.getItem("localpr:prefs")||"null"),f=window.LOCALPR_PREFS;'
        'var p=(!f||(l&&(l.at||"")>(f.at||"")))?l:f;window.LOCALPR_PREFS=p||null;'
        'if(p&&p.theme&&p.theme!=="auto")document.documentElement.setAttribute("data-theme",p.theme);'
        'if(p&&p.wrap)document.body.classList.add("soft-wrap")}catch(e){}</script>'
        + render_toolbar(model)
        + render_tabs(model)
        + conversation
        + '<div class="app-body" id="pane-files" role="tabpanel"'
          ' aria-labelledby="tab-files">'
        + sidebar
        + '<div class="resizer" id="resizer"></div>'
        + '<main class="main" id="main"><div class="main-content">'
        + notice
        + '<div id="globaux"></div>'
        + "".join(body)
        + pied
        + "</div></main></div>"
        + "<script>window.LOCALPR="
        + json.dumps(donnees, ensure_ascii=False).replace("</", "<\\/")
        + ";</script>"
        + f"<script>{read_asset('render.js')}</script>"
        + f"<script>{JS}</script>"
        + "</body></html>"
    )


def typed(value, type_attendu):
    """A findings file is external input, and taking a finding over copies it into a comment.

    A field of the wrong type would only be refused later by sanitize_state - and from then on the
    whole review would stop saving.
    """
    if isinstance(value, bool) or not isinstance(value, type_attendu):
        return None
    return value


def load_findings(path):
    """Findings from another reviewer, normalised into read-only comments."""
    data = read_json(Path(path)) if path else None
    if not data:
        return []
    source = data.get("source") or "findings"
    out = []
    for i, f in enumerate(data.get("findings") or []):
        fichier, ligne = typed(f.get("file"), str), typed(f.get("line"), int)
        texte = [typed(f.get(k), str) for k in ("finding", "recommendation")]
        out.append({
            "id": f"F{i + 1}",
            "scope": "line" if ligne else ("file" if fichier else "global"),
            "type": "fix",
            "side": "new",
            "file": fichier,
            "fichier_index": None,
            "line": ligne,
            "lineEnd": None,
            "anchor": typed(f.get("extrait"), str),
            "anchorOffset": 0,
            "fingerprint": None,
            "body": " — ".join(x for x in texte if x) or "(finding with no text)",
            "origin": {"tool": str(source), "axis": typed(f.get("axis"), str),
                        "severity": typed(f.get("severity"), str),
                        "state": typed(f.get("state"), str)},
            "state": "open",
        })
    return out


def index_findings(findings, model):
    index = {f["path"]: f["index"] for f in model["files"]}
    for f in findings:
        if f["file"] in index:
            f["fichier_index"] = index[f["file"]]
    return findings


def load_replies(dossier):
    out = {}
    if not dossier.is_dir():
        return out
    for file in sorted(dossier.glob("*.json")):
        data = read_json(file)
        for entree in (data if isinstance(data, list) else [data] if data else []):
            cid = (entree or {}).get("comment") or (entree or {}).get("id")
            if cid:
                out[cid] = entree
    return out


PREFS_FILE = (Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
              / "localpr" / "prefs.json")
THEMES = {"auto", "light", "dark", "dimmed"}
VIEWS = {"unified", "split"}
TABS = {2, 4, 8}
STAMP = re.compile(r"^[\d.:+TZ-]{1,40}$")


def sanitize_prefs(raw):
    """The same trust boundary as sanitize_state, and the same trap: a key missing here is
    silently dropped on save."""
    if not isinstance(raw, dict):
        raise ValueError("object expected")
    propres = {}
    if raw.get("theme") in THEMES:
        propres["theme"] = raw["theme"]
    if raw.get("view") in VIEWS:
        propres["view"] = raw["view"]
    if raw.get("tab") in TABS:
        propres["tab"] = int(raw["tab"])
    largeur = raw.get("width")
    if isinstance(largeur, (int, float)) and not isinstance(largeur, bool):
        propres["width"] = max(180, min(560, int(largeur)))
    for cle in ("wrap", "collapsed"):
        if isinstance(raw.get(cle), bool):
            propres[cle] = raw[cle]
    if isinstance(raw.get("at"), str) and STAMP.match(raw["at"]):
        propres["at"] = raw["at"]
    return propres


def read_prefs():
    """Deliberately outside --out: a theme is set once for the machine, and localStorage cannot
    hold it - the served page changes origin with every ephemeral port."""
    try:
        return sanitize_prefs(read_json(PREFS_FILE) or {})
    except ValueError:
        return {}


def write_prefs(prefs):
    try:
        PREFS_FILE.parent.mkdir(parents=True, exist_ok=True)
        write_atomic(PREFS_FILE, json.dumps(prefs, indent=2) + "\n")
        return True
    except OSError:
        return False


COMMENT_KEYS = {
    "id", "scope", "type", "side", "file", "fichier_index", "line", "lineEnd",
    "hunk", "anchor", "anchorOffset", "fingerprint", "body", "origin", "state", "deposeA",
    "reprisDe",
}
COMMENT_ID = re.compile(r"^[CF]\d{1,6}$")
FIELD_TYPES = {
    "file": str, "state": str, "deposeA": str, "reprisDe": str,
    "anchor": str, "hunk": str, "fingerprint": str, "origin": dict,
    "fichier_index": int, "line": int, "lineEnd": int, "anchorOffset": int,
}
SCOPES = {"line", "range", "file", "global"}
TYPES = {"fix", "followUp", "workflowNote"}
MAX_BODY = 8000
MAX_COMMENTS = 500


def sanitize_state(raw):
    """A request body is never trustworthy, not even locally.

    Key whitelist, constrained types, bounded lengths: what comes out of here is injected back
    into the page, and the capped comment count keeps a client-side loop from filling the disk.
    """
    if not isinstance(raw, dict):
        raise ValueError("object expected")
    entrees = raw.get("comments")
    if not isinstance(entrees, list):
        raise ValueError("comments: list expected")
    if len(entrees) > MAX_COMMENTS:
        raise ValueError(f"more than {MAX_COMMENTS} comments")
    propres = []
    for e in entrees:
        if not isinstance(e, dict):
            raise ValueError("comment: object expected")
        cid = str(e.get("id") or "")
        if not COMMENT_ID.match(cid):
            raise ValueError(f"invalid id: {cid[:20]}")
        if e.get("scope") not in SCOPES:
            raise ValueError(f"invalid scope on {cid}")
        if e.get("type") not in TYPES:
            raise ValueError(f"invalid type on {cid}")
        body = e.get("body")
        if not isinstance(body, str) or not body.strip():
            raise ValueError(f"empty body on {cid}")
        if len(body) > MAX_BODY:
            raise ValueError(f"body too long on {cid}")
        for cle, type_attendu in FIELD_TYPES.items():
            valeur = e.get(cle)
            if valeur is None:
                continue
            if isinstance(valeur, bool) or not isinstance(valeur, type_attendu):
                raise ValueError(f"invalid {cle} on {cid}")
        # write_todo indexes the window with it: out of range, /done died server-side and
        # answered the page nothing.
        anchor, offset = e.get("anchor"), e.get("anchorOffset")
        if isinstance(anchor, str) and offset is not None and \
                not 0 <= offset < len(anchor.split("\n")):
            raise ValueError(f"anchorOffset out of range on {cid}")
        propres.append({k: v for k, v in e.items() if k in COMMENT_KEYS})
    return {
        "version": MODEL_VERSION,
        "updated": datetime.now().astimezone().isoformat(timespec="seconds"),
        "n": max([int(c["id"][1:]) for c in propres if c["id"].startswith("C")] or [0]),
        "comments": propres,
    }


class Review:
    """The served HTML lives in memory: a single assignment replaces it.

    Serving a file re-read on every request would expose the page truncated mid-rewrite.
    """

    def __init__(self, repo, out, base, findings):
        self.repo = repo
        self.out = out
        self.base = base
        self.findings = findings
        self.token = secrets.token_urlsafe(24)
        self.port = 0
        self.blob = b""
        self.model = None
        self.last_seen = time.time()

    def url(self):
        return f"http://127.0.0.1:{self.port}/review.html?t={self.token}"

    def hotes(self):
        return {f"127.0.0.1:{self.port}", f"localhost:{self.port}"}

    def origines(self):
        return {f"http://{h}" for h in self.hotes()}

    def rendre(self):
        """The diff stays frozen; the comments do not. Served without this after a save, the page
        hands a refresh back the state the server started with, and the review only survives in
        localStorage."""
        findings = index_findings(load_findings(self.findings), self.model)
        page = render(self.model, read_json(self.out / "comments.json"), findings,
                      load_replies(self.out / "replies"), self.token)
        self.blob = page.encode("utf-8")
        write_atomic(self.out / "review.html",
                        page.replace(f'"token": "{self.token}"', '"token": ""'))

    def regenerer(self):
        self.model = build_model(self.repo, self.base)
        write_atomic(self.out / "diff.json",
                        json.dumps(self.model, ensure_ascii=False, indent=2))
        self.rendre()
        return self.model


PROTOCOL = """
## How to handle these comments

**The three types are not handled the same way.**

| type | what to do with it |
|---|---|
| `fix` | applied to the code |
| `followUp` | **nothing is written**: only reported back at hand-off |
| `workflowNote` | reported back as a lesson about the way of working, never to the code |

**Finding the line again.** The anchor is not a line number but a *window*: the commented line
plus/minus 2 lines, with `anchorOffset` giving the index of the target line inside that window. An
isolated line (`    }`, `    return $this;`, a blank line) occurs dozens of times in a file:
searching for it alone yields a silent false positive.

1. `fingerprint` identical to the file's `sha256` today -> **line numbers are reliable**, go
   straight to `line`. This is the common case.
2. Different fingerprint -> the file moved since the review (php-cs-fixer runs **in write mode**
   under `make quality`, not `--dry-run`: it rewrites lines without moving them). Search for the
   window, stopping at the first rung that yields exactly one candidate:
   **exact -> `rstrip()` -> `strip()` -> normalised inner whitespace**.
3. `side: "old"` -> the comment targets a **deleted** line: it no longer exists in the working
   tree, there is nothing to re-anchor. The `hunk` field carries the context.
4. No candidate -> verdict `anchor-lost`. Do not guess.

**A comment's blast radius is not its file.** Deleting a comment, a rename, a move stay local and
can be handled in parallel. A change to a **business rule** has an unknown radius: it breaks tests
elsewhere. Observed case - removing one piece of information from a completeness calculation broke
a test in another file, which built its "incomplete" case precisely on that information. Work
confined to a single file would have reported "fixed" and left the suite red. Those comments are
handled with the right to follow their tests.

**Reply to every comment**, one file per comment, in `replies/<id>.json`:

```json
{ "comment": "C3", "verdict": "fixed",
  "response": "what was done, or why not - never an intention",
  "filesChanged": ["src/..."] }
```

`verdict`: `fixed` | `refused` | `out-of-scope` | `anchor-lost`. **The right to refuse is
explicit**: a wrong comment must come back as such, with its reason. Applying a mistaken remark out
of obedience is a failure. The page shows the reply under its thread on the next render.

**To finish**: replay the project's own check (`grep -E '^[a-z-]+:' Makefile`, typically
`make quality` then the tests) and never claim green without the command's output. Then regenerate
the page so the replies show up:
`python3 <path to localpr.py> <root> --out <this directory>`.

**Nothing is ever committed**: the working tree is modified, `git status` / `git diff` is there to
be read, and the developer commits.
"""


def write_todo(review, state):
    """The instructions travel with the data.

    Knowledge filed away in a skill only loads if someone invokes it; placed here, it arrives with
    the comments, at the moment they are handled.
    """
    entrees = state.get("comments") or []
    lines = [f"# {len(entrees)} comment(s) to handle - {state.get('updated', '')}", "",
              f"Repository: `{review.repo}` · base `{review.base or 'HEAD'}`", "",
              "Raw data: `comments.json` · frozen diff: `diff.json`", ""]

    par_fichier = {}
    for c in entrees:
        par_fichier.setdefault(c.get("file") or "(global scope)", []).append(c)

    for file in sorted(par_fichier):
        lines.append(f"## {file}")
        for c in par_fichier[file]:
            place = []
            if c.get("line"):
                place.append(f"line {c['line']}")
            if c.get("side"):
                place.append(f"{c['side']} side")
            if c.get("scope") not in (None, "line"):
                place.append(f"{c['scope']} scope")
            if c.get("fingerprint"):
                place.append(c["fingerprint"])
            lines.append(f"- **{c.get('id')}** [{c.get('type')}] — {', '.join(place)}")
            lines.append(f"  > {(c.get('body') or '').strip()}")
            if c.get("origin"):
                lines.append(f"  (taken from a {c['origin'].get('tool')} finding, "
                              f"severity {c['origin'].get('severity')})")
            if c.get("anchor"):
                extrait = (c["anchor"].split("\n")[c.get("anchorOffset") or 0]
                           if c.get("anchor") else "")
                lines.append(f"  anchor: `{extrait.strip()}`")
        lines.append("")

    lines.append(PROTOCOL.strip())
    write_atomic(review.out / "TODO.md", "\n".join(lines) + "\n")


def make_handler(review, stop):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "localpr"

        def log_message(self, *args):
            pass

        def repondre(self, code, body=b"", type_mime="application/json; charset=utf-8"):
            self.send_response(code)
            self.send_header("Content-Type", type_mime)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            if body:
                self.wfile.write(body)

        def refuser(self, code, raison):
            self.repondre(code, json.dumps({"error": raison}).encode("utf-8"))

        def last_seen(self):
            review.last_seen = time.time()

        def host_ok(self):
            """Host checked on top of Origin: this is the defence against DNS rebinding,
            which an Origin check alone does not cover."""
            if self.headers.get("Host", "") not in review.hotes():
                return False
            origin = self.headers.get("Origin")
            return not origin or origin in review.origines()

        def token_ok(self, fourni):
            return secrets.compare_digest(fourni or "", review.token)

        def do_OPTIONS(self):
            """No CORS header: the preflight triggered by X-Localpr-Token makes any request
            from another page fail before it reaches the handler."""
            self.refuser(403, "origin refused")

        def do_GET(self):
            self.last_seen()
            path, _, requete = self.path.partition("?")
            if path not in ("/", "/review.html"):
                return self.refuser(404, "unknown route")
            if not self.host_ok():
                return self.refuser(403, "host refused")
            token = parse_qs(requete).get("t", [""])[0]
            if not self.token_ok(token):
                return self.refuser(403, "missing or invalid token")
            self.repondre(200, review.blob, "text/html; charset=utf-8")

        def do_POST(self):
            self.last_seen()
            if not self.host_ok():
                return self.refuser(403, "host refused")
            if not self.token_ok(self.headers.get("X-Localpr-Token")):
                return self.refuser(403, "missing or invalid token")
            try:
                taille = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return self.refuser(400, "invalid Content-Length")
            if taille > MAX_PAYLOAD:
                return self.refuser(413, f"body larger than {MAX_PAYLOAD} bytes")
            raw = self.rfile.read(min(taille, MAX_PAYLOAD)) if taille else b"{}"

            if self.path == "/ping":
                return self.repondre(200, b'{"ok":true}')

            if self.path == "/prefs":
                try:
                    prefs = sanitize_prefs(json.loads(raw.decode("utf-8", errors="replace")))
                except (ValueError, UnicodeError) as e:
                    return self.refuser(400, f"prefs refused: {e}")
                # Merged, not replaced: a payload carrying one pref must not erase the others.
                if not write_prefs({**read_prefs(), **prefs}):
                    return self.refuser(500, f"{PREFS_FILE} not written")
                return self.repondre(200, b'{"ok":true}')

            if self.path == "/regenerate":
                t = review.regenerer()["totals"]
                return self.repondre(200, json.dumps({"ok": True, "totals": t}).encode("utf-8"))

            if self.path in ("/comments", "/done"):
                try:
                    state = sanitize_state(json.loads(raw.decode("utf-8", errors="replace")))
                except (ValueError, UnicodeError) as e:
                    return self.refuser(400, f"state refused: {e}")
                write_atomic(review.out / "comments.json",
                                json.dumps(state, ensure_ascii=False, indent=2))
                review.rendre()
                if self.path == "/done":
                    write_todo(review, state)
                    (review.out / "done").write_text(
                        f"{len(state['comments'])} comment(s) - {state['updated']}\n"
                        f"to handle: {review.out / 'TODO.md'}\n",
                        encoding="utf-8")
                    threading.Thread(target=stop, daemon=True).start()
                return self.repondre(200, json.dumps(
                    {"ok": True, "n": len(state["comments"])}).encode("utf-8"))

            return self.refuser(404, "unknown route")

    return Handler


SILENCE_MAX = 300
WATCH_STEP = 20


def watch_presence(review, stop):
    """Automatic shutdown once nobody is watching.

    Without this, a tab closed without clicking Finish review leaves a server listening until
    --max-minutes. The page sends a heartbeat; past SILENCE_MAX with no request at all, there is no
    reader left and the server shuts down.
    """
    while True:
        time.sleep(WATCH_STEP)
        if time.time() - review.last_seen > SILENCE_MAX:
            stop()
            return


def serve(review, max_minutes):
    instance = []

    def stop():
        if instance:
            instance[0].shutdown()

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(review, stop))
    httpd.daemon_threads = True
    instance.append(httpd)
    review.port = httpd.server_address[1]
    review.regenerer()

    write_atomic(review.out / "server.json", json.dumps({
        "url": review.url(), "port": review.port, "token": review.token, "pid": os.getpid(),
    }, ensure_ascii=False, indent=2))

    t = review.model["totals"]
    print(f"{t['files']} file(s), +{t['additions']}/-{t['deletions']}, "
          f"{t['lines']} line(s) of diff")
    print(review.url())
    print(f"out : {review.out}", flush=True)

    timers = []
    if max_minutes:
        limit = threading.Timer(max_minutes * 60, stop)
        limit.daemon = True
        limit.start()
        timers.append(limit)

    watcher = threading.Thread(target=watch_presence, args=(review, stop), daemon=True)
    watcher.start()

    def sur_signal(_sig, _frame):
        threading.Thread(target=stop, daemon=True).start()

    signal.signal(signal.SIGTERM, sur_signal)
    signal.signal(signal.SIGINT, sur_signal)

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    for m in timers:
        m.cancel()
    httpd.server_close()
    (review.out / "server.json").unlink(missing_ok=True)
    fin = review.out / "done"
    print(fin.read_text(encoding="utf-8").strip() if fin.exists()
          else "server stopped without Finish review")


def answers(url, token):
    """A live pid proves nothing: the number of a server killed abruptly goes to the next
    process to start, and --stop-all would SIGTERM a stranger. Only the token identifies it."""
    if not isinstance(url, str) or not isinstance(token, str):
        return False
    req = Request(url.split("/review.html", 1)[0] + "/ping", data=b"{}", method="POST",
                  headers={"X-Localpr-Token": token, "Content-Type": "application/json"})
    try:
        # http_proxy in the environment would route 127.0.0.1 through the proxy.
        with build_opener(ProxyHandler({})).open(req, timeout=2) as r:
            return r.status == 200
    except (OSError, ValueError):
        return False


def live_servers():
    """Servers declared under ~/.claude/reviews, with the real state of their process.

    `server.json` is deleted on clean shutdown: one that survives with no live process is the
    record of a server killed abruptly, not of a running one.
    """
    root = Path.home() / ".claude" / "reviews"
    found = []
    for file in sorted(root.glob("*/*/server.json")) if root.is_dir() else []:
        data = read_json(file) or {}
        pid = data.get("pid")
        alive = False
        if isinstance(pid, int):
            try:
                os.kill(pid, 0)
                alive = True
            except (OSError, ProcessLookupError):
                alive = False
        found.append({"dossier": file.parent, "pid": pid, "url": data.get("url"),
                        "alive": alive and answers(data.get("url"), data.get("token")),
                        "pid_alive": alive})
    return found


def output_dir(repo, out):
    if out:
        return Path(out).expanduser().resolve()
    horodatage = time.strftime("%Y-%m-%d-%H%M")
    return Path.home() / ".claude" / "reviews" / repo.name / horodatage


def write_atomic(path, text):
    """Replace-on-write: a page served or re-read mid-write would arrive truncated."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def main():
    p = argparse.ArgumentParser(description="Pull-request-style review of a local diff.")
    p.add_argument("repo", nargs="?", default=".", help="root of the repository to review")
    p.add_argument("--out", help="output directory (default ~/.claude/reviews/<project>/<timestamp>)")
    p.add_argument("--base", help="compare against a ref instead of the working tree alone")
    p.add_argument("--findings", help="findings JSON to display as a second reviewer")
    p.add_argument("--dump-json", action="store_true", help="print the data model and exit")
    p.add_argument("--check", action="store_true", help="check the parser against git")
    p.add_argument("--list", action="store_true", dest="lister",
                   help="list review servers still alive")
    p.add_argument("--stop-all", action="store_true", dest="stop_all",
                   help="stop every review server still alive")
    p.add_argument("--serve", action="store_true",
                   help="serve the page on 127.0.0.1 and collect comments")
    p.add_argument("--max-minutes", type=int, default=60,
                   help="safety net: stop the server past this (0 = no limit)")
    a = p.parse_args()

    if a.lister or a.stop_all:
        found = live_servers()
        if not found:
            print("no review server declared")
            return 0
        for t in found:
            state = ("alive" if t["alive"]
                     else "pid alive but not answering as localpr (stale record, not stopped)"
                     if t["pid_alive"] else "process gone (stale record)")
            print(f"{state} · pid {t['pid']} · {t['dossier']}")
            if t["url"]:
                print(f"  {t['url']}")
            if a.stop_all and t["alive"]:
                try:
                    os.kill(t["pid"], 15)
                    print("  stopped")
                except OSError as e:
                    print(f"  could not stop: {e}")
            if a.stop_all and not t["alive"]:
                (t["dossier"] / "server.json").unlink(missing_ok=True)
                print("  stale record removed")
        return 0

    try:
        repo = repo_root(Path(a.repo).expanduser().resolve())
    except GitUnavailable as e:
        print(f"localpr : {e}", file=sys.stderr)
        return 2

    if a.serve:
        out = output_dir(repo, a.out)
        (out / "replies").mkdir(parents=True, exist_ok=True)
        if a.findings:
            write_atomic(out / "findings.json",
                            Path(a.findings).read_text(encoding="utf-8"))
        serve(Review(repo, out, a.base, a.findings or (out / "findings.json")),
               a.max_minutes)
        return 0

    model = build_model(repo, a.base)

    if a.check:
        divergences = verify(repo, model, a.base)
        t = model["totals"]
        print(f"{t['files']} file(s), +{t['additions']}/-{t['deletions']}, "
              f"{t['lines']} line(s) of diff")
        for d in divergences:
            print(f"  ✗ {d}")
        print("integrity: OK" if not divergences else f"integrity: {len(divergences)} mismatch(es)")
        return 0 if not divergences else 1

    if a.dump_json:
        print(json.dumps(model, ensure_ascii=False, indent=2))
        return 0

    out = output_dir(repo, a.out)
    (out / "replies").mkdir(parents=True, exist_ok=True)
    write_atomic(out / "diff.json", json.dumps(model, ensure_ascii=False, indent=2))

    if a.findings:
        write_atomic(out / "findings.json",
                        Path(a.findings).read_text(encoding="utf-8"))
    findings = index_findings(load_findings(a.findings or (out / "findings.json")), model)
    comments = read_json(out / "comments.json")
    replies = load_replies(out / "replies")

    page = out / "review.html"
    write_atomic(page, render(model, comments, findings, replies, None))
    t = model["totals"]
    print(f"{t['files']} file(s), +{t['additions']}/-{t['deletions']}")
    print(f"file://{page}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
