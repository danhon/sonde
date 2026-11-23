#!/bin/sh
# Compile sonde/web/static/app.css with Tailwind's standalone CLI (sonde#6).
#
#   scripts/tailwind.sh            write sonde/web/static/app.css
#   scripts/tailwind.sh --check    fail if the committed app.css is stale
#
# The CLI is one binary, no Node. It is pinned by version and SHA-256 and
# cached outside the repo; a download that doesn't match is deleted, not run.
set -eu

VERSION=3.4.17
case "$(uname -s)-$(uname -m)" in
  Darwin-arm64)  ASSET=tailwindcss-macos-arm64 SHA=a1d0c7985759accca0bf12e51ac1dcbf0f6cf2fffb62e6e0f62d091c477a10a3 ;;
  Darwin-x86_64) ASSET=tailwindcss-macos-x64   SHA=6cbdad74be776c087ffa5e9a057512c54898f9fe8828d3362212dfe32fc933a3 ;;
  Linux-x86_64)  ASSET=tailwindcss-linux-x64   SHA=7d24f7fa191d2193b78cd5f5a42a6093e14409521908529f42d80b11fde1f1d4 ;;
  Linux-aarch64) ASSET=tailwindcss-linux-arm64 SHA=69b1378b8133192d7d2feb12a116fa12d035594f58db3eff215879e4ad8cf39b ;;
  *) echo "tailwind.sh: no pinned CLI for $(uname -s)-$(uname -m)" >&2; exit 1 ;;
esac

cd "$(dirname "$0")/.."
CACHE="${XDG_CACHE_HOME:-$HOME/.cache}/sonde-tailwind/$VERSION"
BIN="$CACHE/$ASSET"

sha256() { if command -v sha256sum >/dev/null; then sha256sum "$1"; else shasum -a 256 "$1"; fi | cut -d' ' -f1; }

if [ ! -x "$BIN" ] || [ "$(sha256 "$BIN")" != "$SHA" ]; then
  mkdir -p "$CACHE"
  curl -fsSL -o "$BIN.part" \
    "https://github.com/tailwindlabs/tailwindcss/releases/download/v$VERSION/$ASSET"
  if [ "$(sha256 "$BIN.part")" != "$SHA" ]; then
    rm -f "$BIN.part"
    echo "tailwind.sh: $ASSET does not match its pinned SHA-256; not running it" >&2
    exit 1
  fi
  chmod +x "$BIN.part" && mv "$BIN.part" "$BIN"
fi

OUT=sonde/web/static/app.css
build() { "$BIN" -c tailwind.config.js -i sonde/web/tailwind.css -o "$1" --minify; }

if [ "${1:-}" = "--check" ]; then
  tmp=$(mktemp)
  trap 'rm -f "$tmp"' EXIT
  build "$tmp"
  if ! cmp -s "$tmp" "$OUT"; then
    echo "tailwind.sh: $OUT is stale; run make css and commit it" >&2
    exit 1
  fi
  echo "tailwind.sh: $OUT is current"
else
  build "$OUT"
  echo "tailwind.sh: wrote $OUT ($(wc -c < "$OUT" | tr -d ' ') bytes)"
fi
