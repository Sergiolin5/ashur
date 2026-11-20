#!/bin/sh
# Rebuilds core/ui/vendor/grafo.min.js (sigma 3.0.3 + graphology 0.26.0, MIT) and vendor/grafo-worker.js (ForceAtlas2,
# graphology-layout-forceatlas2 0.10.1, MIT) for the vault graph of «Cerebro» (0.75.0). Run from the repo root with Node.
set -e
tmp=$(mktemp -d)
cp deploy/grafo-entry.js "$tmp/entry.js"
cp deploy/grafo-worker-entry.js "$tmp/worker.js"
cd "$tmp"
npm init -y >/dev/null
npm install --silent sigma@3.0.3 graphology@0.26.0 graphology-layout-forceatlas2@0.10.1 esbuild@0.25
npx esbuild entry.js --bundle --minify --format=esm --legal-comments=inline --outfile=grafo.min.js
npx esbuild worker.js --bundle --minify --format=iife --legal-comments=inline --outfile=grafo-worker.js
cd - >/dev/null
cp "$tmp/grafo.min.js" "$tmp/grafo-worker.js" core/ui/vendor/
echo "core/ui/vendor/grafo.min.js: $(wc -c < core/ui/vendor/grafo.min.js) bytes; grafo-worker.js: $(wc -c < core/ui/vendor/grafo-worker.js) bytes"
