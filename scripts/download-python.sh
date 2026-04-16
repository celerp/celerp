#!/usr/bin/env bash
# Download python-build-standalone for Electron bundling.
# Usage: ./scripts/download-python.sh <platform> <arch>
#   platform: windows | linux
#   arch:     x64
#
# Output: electron/python-standalone/<platform>-<arch>/
# The directory is used by electron-builder extraResources.
#
# Python version is pinned via PYTHON_VERSION + PBS_RELEASE below.
# Bump these together when upgrading Python.
set -euo pipefail

PLATFORM="${1:-}"
ARCH="${2:-x64}"
PYTHON_VERSION="3.12.13"
PBS_RELEASE="20260408"   # python-build-standalone release tag

if [[ -z "$PLATFORM" ]]; then
  echo "Usage: $0 <platform> <arch>" >&2
  echo "  platform: win | linux  (matches electron-builder \${os} macro)" >&2
  exit 1
fi

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT_DIR="$REPO_ROOT/electron/python-standalone/${PLATFORM}-${ARCH}"

# Map our platform/arch to python-build-standalone naming
case "${PLATFORM}-${ARCH}" in
  win-x64)
    PBS_TARGET="x86_64-pc-windows-msvc"
    PYTHON_BIN_REL="python/python.exe"
    PIP_BIN_REL="python/Scripts/pip.exe"
    ;;
  linux-x64)
    PBS_TARGET="x86_64-unknown-linux-gnu"
    PYTHON_BIN_REL="python/bin/python3"
    PIP_BIN_REL="python/bin/pip3"
    ;;
  *)
    echo "Unsupported platform-arch: ${PLATFORM}-${ARCH}" >&2
    exit 1
    ;;
esac

FILENAME="cpython-${PYTHON_VERSION}+${PBS_RELEASE}-${PBS_TARGET}-install_only.tar.gz"
URL="https://github.com/astral-sh/python-build-standalone/releases/download/${PBS_RELEASE}/${FILENAME}"

echo "Downloading $FILENAME..."
TMPDIR="$(mktemp -d)"
trap 'rm -rf "$TMPDIR"' EXIT

curl -fL --progress-bar -o "$TMPDIR/$FILENAME" "$URL"

echo "Extracting to $OUT_DIR..."
rm -rf "$OUT_DIR"
mkdir -p "$OUT_DIR"
tar xzf "$TMPDIR/$FILENAME" -C "$OUT_DIR"

PYTHON_BIN="$OUT_DIR/$PYTHON_BIN_REL"

if [[ ! -f "$PYTHON_BIN" ]]; then
  echo "ERROR: Python binary not found at $PYTHON_BIN" >&2
  echo "Contents of $OUT_DIR:" >&2
  ls -la "$OUT_DIR/" >&2
  exit 1
fi

# Pre-install Celerp and all its dependencies into the bundled Python.
# This means zero network calls at first launch — all deps ship in the binary.
echo "Installing Celerp dependencies into bundled Python (this takes a few minutes)..."
"$PYTHON_BIN" -m pip install \
  --quiet \
  --no-warn-script-location \
  --upgrade pip

"$PYTHON_BIN" -m pip install \
  --quiet \
  --no-warn-script-location \
  "$REPO_ROOT[prod]"

echo ""
echo "✓ Bundled Python $PYTHON_VERSION ready at: $OUT_DIR"
echo "  Binary: $PYTHON_BIN"
echo "  Celerp deps installed."
