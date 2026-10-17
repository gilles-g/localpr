#!/usr/bin/env sh
# Builds the repository --check is run against: every shape that once broke the parser.
# Usage: fixture.sh <dir>   (creates <dir>/repo, branch "feature" on top of "main")
set -eu
root="$1"
rm -rf "$root" && mkdir -p "$root" && cd "$root"
git init -q -b main repo && cd repo
git config user.email ci@localpr && git config user.name localpr-ci
git config commit.gpgsign false

mkdir -p src/Domain/Commission sub
printf 'a\nb\nc\nd\ne\nf\ng\n' > src/Domain/Commission/Old.php
printf 'x=1\n' > "path with space.ini"
printf 'line1\nline2\n' > keep.txt
printf 'p\n' > plain.txt
# latin-1 bytes: a text=True subprocess would have died here
printf '\351t\351 latin\n' > latin.txt
# a NUL byte makes git call it binary whatever the rest holds
printf 'BIN\000\001\002old\n' > bin.dat
printf '#!/bin/sh\necho hi\n' > script.sh
printf 'm\n' > modeonly.txt
# `<!-- <script>` left open in hidden lines: with only `</` escaped, it swallowed the page
printf '<html>\n<!-- <script>\nold guard, never closed\n<p>1</p>\n<p>2</p>\n<p>3</p>\n<p>4</p>\n<p>5</p>\n<p>6</p>\n</html>\n' > page.html
git add -A && git commit -qm init
# a nested repository committed as a gitlink, bumped later
git -C sub init -q -b main && git -C sub -c user.email=ci@localpr -c user.name=localpr-ci \
    commit -q --allow-empty -m s
git add sub 2>/dev/null && git commit -qm gitlink

git checkout -qb feature
git mv src/Domain/Commission/Old.php src/Domain/Commission/New.php
printf 'a\nB\nc\nd\ne\nf\ng\nh\n' > src/Domain/Commission/New.php
git add -A && git commit -qm "rename+edit"

# working tree, on top of the branch
git mv plain.txt moved.txt                       # pure rename, staged
printf 'x=2\n' > "path with space.ini"
printf 'line1\nline2\nline3' > keep.txt           # no newline at end of file
printf '\351t\351 latin\nplus\n' > latin.txt
printf 'BIN\000\001\002new\n' > bin.dat
chmod +x script.sh && printf '#!/bin/sh\necho hi\necho bye\n' > script.sh   # mode + content
chmod +x modeonly.txt                             # mode alone
printf '<html>\n<!-- <script>\nold guard, never closed\n<p>1</p>\n<p>2</p>\n<p>3</p>\n<p>4</p>\n<p>5</p>\n<p>6 changed</p>\n</html>\n' > page.html
git -C sub -c user.email=ci@localpr -c user.name=localpr-ci commit -q --allow-empty -m s2
printf 'new\nfile\n' > untracked.py
mkdir -p newdir/deep && printf 'k: v\n' > newdir/deep/c.yml
mkdir nested && git -C nested init -q && printf 'z\n' > nested/z.txt
# names git quotes C-style
printf 'q\n' > 'we"ird.txt'
printf 'b\n' > 'back\slash.txt'
printf 't\n' > "$(printf 'tab\there.txt')"
printf 'e\n' > 'é"mixed.txt'
echo "fixture ready: $root/repo"
