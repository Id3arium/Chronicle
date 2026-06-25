#!/bin/bash
# build.sh - Build the Chronicle Export Firefox extension.
#
# Uses the project-local web-ext pinned in extension/package.json, so the
# build is reproducible on any machine. No global web-ext needed.

set -e
cd "$(dirname "$0")/extension"

# Bootstrap the pinned toolchain on a fresh clone.
if [ ! -x node_modules/.bin/web-ext ]; then
  echo "📥 Installing build tooling (web-ext)..."
  npm install
fi

echo "🔍 Linting extension..."
npm run --silent lint

echo ""
echo "📦 Building extension..."
npm run --silent build

echo ""
echo "✅ Done. Artifact in ./artifacts/"
