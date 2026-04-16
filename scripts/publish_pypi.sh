#!/usr/bin/env bash
# PyPI submission script for celerp
# Usage: ./scripts/publish_pypi.sh [testpypi|pypi]
#
# Prerequisites:
#   pip install twine build
#   Set PYPI_TOKEN or TESTPYPI_TOKEN in environment (or ~/.pypirc)
#
# Steps:
#   1. Run from repo root: cd /path/to/celerp/core
#   2. Bump version in pyproject.toml if needed
#   3. ./scripts/publish_pypi.sh testpypi   (test first)
#   4. ./scripts/publish_pypi.sh pypi       (publish for real)

set -euo pipefail

DEST="${1:-testpypi}"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

cd "$REPO_ROOT"

echo "=== Building wheel ==="
python -m build --wheel --no-isolation

echo "=== Checking distribution ==="
twine check dist/*.whl

if [[ "$DEST" == "testpypi" ]]; then
    echo "=== Uploading to TestPyPI ==="
    twine upload --repository testpypi dist/*.whl
    echo ""
    echo "Install with: pip install --index-url https://test.pypi.org/simple/ celerp"
elif [[ "$DEST" == "pypi" ]]; then
    echo "=== Uploading to PyPI ==="
    twine upload dist/*.whl
    echo ""
    echo "Install with: pip install celerp"
else
    echo "Usage: $0 [testpypi|pypi]"
    exit 1
fi

echo "Done."
