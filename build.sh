#!/bin/bash
# build.sh - Build the Chronicle Export Firefox extension.

set -e

echo "🔍 Linting extension..."
web-ext lint --source-dir extension

echo ""
echo "📦 Building extension..."
web-ext build --source-dir extension --artifacts-dir artifacts --overwrite-dest

echo ""
echo "✅ Done. Artifact in ./artifacts/"
