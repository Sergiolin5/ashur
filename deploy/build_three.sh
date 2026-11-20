#!/bin/sh
# Rebuilds core/ui/vendor/three.min.js (three.js, MIT) with only what «Cerebro» and Casa · Mapa use,
# and writes the Basis transcoder worker (Apache-2.0, shipped inside three) that KTX2Loader needs.
# Run from the repo root on a machine with Node. 0.65.0 shipped three 0.187.0; 0.70.0 added the map's loaders.
set -e
tmp=$(mktemp -d)
cp deploy/three-entry.js "$tmp/entry.js"
cp deploy/ktx2-worker.mjs "$tmp/ktx2-worker.mjs"
cd "$tmp"
npm init -y >/dev/null
npm install --silent three@0.187.0 esbuild@0.25
npx esbuild entry.js --bundle --minify --format=esm --legal-comments=inline --outfile=three.min.js
node ktx2-worker.mjs ktx2-worker.js
cd - >/dev/null
cp "$tmp/three.min.js" core/ui/vendor/three.min.js
mkdir -p core/ui/vendor/basis
# the worker is a real file (not KTX2Loader's blob) so the core can serve it with its own CSP: the Basis transcoder
# needs eval (emscripten embind), and only this worker gets it, never the panel
cp "$tmp/ktx2-worker.js" "$tmp/node_modules/three/examples/jsm/libs/basis/basis_transcoder.wasm" core/ui/vendor/basis/
echo "core/ui/vendor/three.min.js: $(wc -c < core/ui/vendor/three.min.js) bytes"
ls -l core/ui/vendor/basis
