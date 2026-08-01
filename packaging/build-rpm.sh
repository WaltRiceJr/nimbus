#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Build the RPM from the current checkout. Run from the repository root on a
# Fedora-family system (or container) with:
#
#   sudo dnf install rpm-build python3-devel pyproject-rpm-macros \
#                    python3-setuptools python3-pip python3-wheel \
#                    desktop-file-utils
#   packaging/build-rpm.sh
#
# The finished RPM lands in dist/.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

VERSION="$(python3 -c 'import nimbus; print(nimbus.__version__)')"
TOPDIR="$(pwd)/build/rpm"

rm -rf "$TOPDIR"
mkdir -p "$TOPDIR"/{SOURCES,SPECS} dist

git archive --format=tar.gz --prefix="nimbus-$VERSION/" \
    -o "$TOPDIR/SOURCES/nimbus-$VERSION.tar.gz" HEAD
cp packaging/nimbus-weather.spec "$TOPDIR/SPECS/"

rpmbuild --define "_topdir $TOPDIR" -ba "$TOPDIR/SPECS/nimbus-weather.spec"

cp "$TOPDIR"/RPMS/noarch/*.rpm "$TOPDIR"/SRPMS/*.rpm dist/
echo "RPMs in dist/:"
ls -1 dist/*.rpm
