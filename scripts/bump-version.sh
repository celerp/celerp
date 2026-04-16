#!/usr/bin/env bash
# Bump version across all three version sources atomically.
# Usage: ./scripts/bump-version.sh 1.0.0
set -euo pipefail

VERSION="${1:-}"
if [[ -z "$VERSION" ]]; then
  echo "Usage: $0 <version>  (e.g. $0 1.0.0)" >&2
  exit 1
fi

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

# 1. pyproject.toml
sed -i "s/^version = .*/version = \"$VERSION\"/" "$REPO_ROOT/pyproject.toml"
echo "  ✓ pyproject.toml -> $VERSION"

# 2. celerp/__init__.py
sed -i "s/^__version__ = .*/__version__ = \"$VERSION\"/" "$REPO_ROOT/celerp/__init__.py"
echo "  ✓ celerp/__init__.py -> $VERSION"

# 3. electron/package.json (requires node)
node -e "
const fs = require('fs');
const p = JSON.parse(fs.readFileSync('$REPO_ROOT/electron/package.json', 'utf8'));
p.version = '$VERSION';
fs.writeFileSync('$REPO_ROOT/electron/package.json', JSON.stringify(p, null, 2) + '\n');
"
echo "  ✓ electron/package.json -> $VERSION"

echo ""
echo "Version bumped to $VERSION. Verify with:"
echo "  grep -E '^version|^__version__' pyproject.toml celerp/__init__.py"
echo "  node -e \"console.log(require('./electron/package.json').version)\""
