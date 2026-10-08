#!/usr/bin/env bash
# Source tarball for any distro:  packaging/build-tarball.sh [output-dir]   (then: tar xf ...; ./install.sh)
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="${1:-$HERE/dist}"
VERSION="$(python3 -c "import re; print(re.search(r'__version__ = \"([^\"]+)\"', open('$HERE/chatbridge/__init__.py').read()).group(1))")"
mkdir -p "$OUT"
git -C "$HERE" archive --format=tar.gz --prefix="chatbridge-$VERSION/" -o "$OUT/chatbridge-$VERSION.tar.gz" HEAD
echo "$OUT/chatbridge-$VERSION.tar.gz"
