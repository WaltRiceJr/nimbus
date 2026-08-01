#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Cut a release: bump the version everywhere it lives, commit, and tag.
#
#   packaging/release.sh 1.1.0          # bump, commit, tag v1.1.0
#   packaging/release.sh 1.1.0 --push   # ...and push, triggering the
#                                       # release workflow
#
# The version lives in three files, and this script is the only thing that
# should ever touch it:
#
#   nimbus/__init__.py                  __version__ (app.py and pyproject
#                                       both read it from here)
#   packaging/nimbus-weather.spec       Version: plus a %changelog entry
#   data/org.nimbus.Weather.metainfo.xml  a <release/> entry
set -euo pipefail

die() { echo "error: $*" >&2; exit 1; }

cd "$(dirname "${BASH_SOURCE[0]}")/.."

VERSION="${1:-}"
PUSH="${2:-}"
[ -n "$VERSION" ] || die "usage: packaging/release.sh X.Y.Z [--push]"
[[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "version must be X.Y.Z, got '$VERSION'"
[ -z "$PUSH" ] || [ "$PUSH" = "--push" ] || die "unknown argument '$PUSH'"

git diff --quiet && git diff --cached --quiet || die "working tree is not clean"
git rev-parse -q --verify "refs/tags/v$VERSION" >/dev/null && die "tag v$VERSION already exists"

# -B: a bytecode cache written here would outlive the sed edit below, whose
# same-length substitution can leave mtime and size looking unchanged.
OLD="$(python3 -B -c 'import nimbus; print(nimbus.__version__)')"
[ "$VERSION" != "$OLD" ] || die "already at $OLD"

NAME="$(git config user.name)"
[ -n "$NAME" ] || die "git config user.name is empty"
TODAY="$(date +%Y-%m-%d)"
RPMDATE="$(LC_ALL=C date '+%a %b %d %Y')"

echo "Releasing $OLD -> $VERSION"

# -- bump ---------------------------------------------------------------------

sed -i "s/^__version__ = \".*\"/__version__ = \"$VERSION\"/" nimbus/__init__.py

sed -i "s/^Version:\([[:space:]]*\).*/Version:\1$VERSION/" packaging/nimbus-weather.spec
sed -i "/^%changelog$/a * $RPMDATE $NAME - $VERSION-1\n- Release $VERSION\n" \
    packaging/nimbus-weather.spec

# A fresh entry above the newest release element, matching its indentation.
sed -i "0,/<release /s//<release version=\"$VERSION\" date=\"$TODAY\"\/>\n    <release /" \
    data/org.nimbus.Weather.metainfo.xml

# -- verify -------------------------------------------------------------------

grep -q "^__version__ = \"$VERSION\"$" nimbus/__init__.py \
    || die "nimbus/__init__.py did not take the new version"
grep -q "^Version:[[:space:]]*$VERSION$" packaging/nimbus-weather.spec \
    || die "spec did not take the new version"
grep -q "<release version=\"$VERSION\" date=\"$TODAY\"/>" data/org.nimbus.Weather.metainfo.xml \
    || die "metainfo did not take the new version"

command -v rpmspec >/dev/null && rpmspec -P packaging/nimbus-weather.spec >/dev/null
command -v appstreamcli >/dev/null && \
    appstreamcli validate --no-net data/org.nimbus.Weather.metainfo.xml >/dev/null

# -- commit and tag -----------------------------------------------------------

git add nimbus/__init__.py packaging/nimbus-weather.spec data/org.nimbus.Weather.metainfo.xml
git commit -m "Release $VERSION"
git tag -a "v$VERSION" -m "NimbUS $VERSION"

if [ "$PUSH" = "--push" ]; then
    git push origin HEAD "v$VERSION"
    echo "Pushed. The release workflow is building the packages:"
    echo "  https://github.com/WaltRiceJr/nimbus/actions"
else
    echo "Committed and tagged v$VERSION. To trigger the release workflow:"
    echo "  git push origin HEAD v$VERSION"
fi
