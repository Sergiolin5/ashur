#!/bin/sh
# Rebuilds core/ui/vendor/three.min.js (three.js, MIT) with only what the «Cerebro» tab uses.
# Run from the repo root on a machine with Node. 0.65.0 shipped three 0.187.0.
set -e
tmp=$(mktemp -d)
cp deploy/three-entry.js "$tmp/entry.js"
cd "$tmp"
npm init -y >/dev/null
npm install --silent three@0.187.0 esbuild@0.25
npx esbuild entry.js --bundle --minify --format=esm --legal-comments=inline --outfile=three.min.js
cd - >/dev/null
cp "$tmp/three.min.js" core/ui/vendor/three.min.js
echo "core/ui/vendor/three.min.js: $(wc -c < core/ui/vendor/three.min.js) bytes"
