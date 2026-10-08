#!/usr/bin/env bash
# Build chatbridge_<version>_all.deb without root or debhelper:  packaging/deb/build-deb.sh [output-dir]
# Reproducible: file times come from the last commit (SOURCE_DATE_EPOCH), ownership is forced to root.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
OUT="${1:-$HERE/dist}"
VERSION="$(python3 -c "import re,sys; print(re.search(r'__version__ = \"([^\"]+)\"', open('$HERE/chatbridge/__init__.py').read()).group(1))")"
EPOCH="${SOURCE_DATE_EPOCH:-$(git -C "$HERE" log -1 --format=%ct 2>/dev/null || date +%s)}"
PKG="chatbridge"
ROOT="$(mktemp -d)"
STAGE="$ROOT/${PKG}_${VERSION}_all"
trap 'rm -rf "$ROOT"' EXIT

install -d -m 755 "$STAGE/DEBIAN" "$STAGE/usr/bin" "$STAGE/usr/lib/python3/dist-packages" \
  "$STAGE/usr/share/applications" "$STAGE/usr/share/icons/hicolor/scalable/apps" "$STAGE/usr/share/metainfo" \
  "$STAGE/usr/lib/systemd/user" "$STAGE/usr/share/man/man1" "$STAGE/usr/share/doc/$PKG"

# Python package (no bytecode: it is compiled for the target interpreter in postinst)
cp -r "$HERE/chatbridge" "$STAGE/usr/lib/python3/dist-packages/chatbridge"
find "$STAGE/usr/lib/python3/dist-packages" -name '__pycache__' -prune -exec rm -rf {} +

# Command wrappers (system python, as Debian policy expects)
for spec in "chatbridge:chatbridge.cli" "chatbridge-gui:chatbridge.gui.app"; do
  name="${spec%%:*}"; module="${spec##*:}"
  printf '#!/usr/bin/python3\nfrom %s import main_entry\n\nmain_entry()\n' "$module" > "$STAGE/usr/bin/$name"
  chmod 755 "$STAGE/usr/bin/$name"
done

# Desktop integration
sed "s|@LAUNCHER@|/usr/bin/chatbridge-gui|" "$HERE/packaging/io.github.pwnapplehat.ChatBridge.desktop.in" \
  > "$STAGE/usr/share/applications/io.github.pwnapplehat.ChatBridge.desktop"
install -m 644 "$HERE/packaging/io.github.pwnapplehat.ChatBridge.svg" "$STAGE/usr/share/icons/hicolor/scalable/apps/"
install -m 644 "$HERE/packaging/io.github.pwnapplehat.ChatBridge.metainfo.xml" "$STAGE/usr/share/metainfo/"
sed "s|@CLI@|/usr/bin/chatbridge|" "$HERE/packaging/chatbridge-sync.service.in" > "$STAGE/usr/lib/systemd/user/chatbridge-sync.service"

# Man pages, documentation
for page in chatbridge chatbridge-gui; do gzip -9n -c "$HERE/packaging/deb/$page.1" > "$STAGE/usr/share/man/man1/$page.1.gz"; done
gzip -9n -c "$HERE/README.md" > "$STAGE/usr/share/doc/$PKG/README.md.gz"
cp -r "$HERE/docs" "$STAGE/usr/share/doc/$PKG/docs"
cat > "$STAGE/usr/share/doc/$PKG/copyright" <<C
Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/
Upstream-Name: ChatBridge
Source: https://github.com/pwnapplehat/chatbridge

Files: *
Copyright: 2026 ChatBridge contributors
License: Expat
 Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated
 documentation files (the "Software"), to deal in the Software without restriction, including without limitation the
 rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software, and to permit
 persons to whom the Software is furnished to do so, subject to the following conditions:
 .
 The above copyright notice and this permission notice shall be included in all copies or substantial portions of the
 Software.
 .
 THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE
 WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR
 COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR
 OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
C
{
  printf '%s (%s) unstable; urgency=medium\n\n' "$PKG" "$VERSION"
  sed -n '/^## /,$p' "$HERE/CHANGELOG.md" | sed '1d;s/^- /  * /;/^$/d'
  printf '\n -- iOS_hAT <ioshat7797@gmail.com>  %s\n' "$(date -u -R -d "@$EPOCH")"
} | gzip -9n > "$STAGE/usr/share/doc/$PKG/changelog.Debian.gz"

# Control files
INSTALLED_KB="$(du -sk --exclude=DEBIAN "$STAGE" | cut -f1)"
cat > "$STAGE/DEBIAN/control" <<C
Package: $PKG
Version: $VERSION
Section: utils
Priority: optional
Architecture: all
Depends: python3 (>= 3.11), python3-gi, gir1.2-gtk-4.0, gir1.2-adw-1 (>= 1.5)
Installed-Size: $INSTALLED_KB
Maintainer: iOS_hAT <ioshat7797@gmail.com>
Homepage: https://github.com/pwnapplehat/chatbridge
Description: two-way chat history sync between Cursor and Claude
 ChatBridge keeps conversations in step between Cursor and Claude. Continue a task
 in either tool and carry it over to the other, with every message, the model's
 reasoning and all tool calls with their outputs.
 .
 Syncing is append-only and reversible: nothing is rewritten or deleted, every
 write into Cursor is journaled and can be undone, and Cursor is never written
 while it is running. Includes a GTK4/libadwaita application, a command line
 tool and an optional auto-sync systemd user service.
C
cat > "$STAGE/DEBIAN/postinst" <<'C'
#!/bin/sh
set -e
if [ "$1" = configure ]; then
    command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database -q /usr/share/applications || true
    command -v gtk-update-icon-cache >/dev/null 2>&1 && gtk-update-icon-cache -q -t -f /usr/share/icons/hicolor || true
    command -v py3compile >/dev/null 2>&1 && py3compile -p chatbridge || true
fi
exit 0
C
cat > "$STAGE/DEBIAN/prerm" <<'C'
#!/bin/sh
set -e
if [ "$1" = remove ] || [ "$1" = upgrade ]; then
    command -v py3clean >/dev/null 2>&1 && py3clean -p chatbridge || true
fi
exit 0
C
cat > "$STAGE/DEBIAN/postrm" <<'C'
#!/bin/sh
set -e
if [ "$1" = remove ] || [ "$1" = purge ]; then
    command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database -q /usr/share/applications || true
    command -v gtk-update-icon-cache >/dev/null 2>&1 && gtk-update-icon-cache -q -t -f /usr/share/icons/hicolor || true
fi
exit 0
C
chmod 755 "$STAGE/DEBIAN/postinst" "$STAGE/DEBIAN/prerm" "$STAGE/DEBIAN/postrm"

# Normalise modes and times, then checksums and build
find "$STAGE" -type d -exec chmod 755 {} +
find "$STAGE" -type f ! -path "$STAGE/DEBIAN/*" ! -path "$STAGE/usr/bin/*" -exec chmod 644 {} +
(cd "$STAGE" && find usr -type f -print0 | sort -z | xargs -0 md5sum > DEBIAN/md5sums)
find "$STAGE" -exec touch -h -d "@$EPOCH" {} +
mkdir -p "$OUT"
dpkg-deb --root-owner-group -Zxz --build "$STAGE" "$OUT/${PKG}_${VERSION}_all.deb" >/dev/null
echo "$OUT/${PKG}_${VERSION}_all.deb"
