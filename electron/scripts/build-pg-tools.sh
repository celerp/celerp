#!/usr/bin/env bash
#
# build-pg-tools.sh — build relocatable pg_dump / pg_restore from source (macOS).
#
# Builds one architecture's tree. The CI macOS job calls it twice (arm64, then
# x86_64) on a single arm64 runner; the x86_64 pass cross-compiles with
# `-arch x86_64` and relies on Rosetta 2 to run configure's compile-and-run
# probes.
#
# Usage:  build-pg-tools.sh <arch> <out_dir>
#   arch     : arm64 | x86_64
#   out_dir  : destination tree, e.g. electron/pg-tools/mac-arm64
#
# Required env (pinned in .github/workflows/build.yml):
#   PG_VERSION, PG_SHA256, OPENSSL_VERSION, OPENSSL_SHA256
#
# Produces:  <out_dir>/bin/{pg_dump,pg_restore}  +  <out_dir>/lib/libpq.5.dylib
# Fails loudly if any binary still references a non-system, non-bundled path.

set -euo pipefail

ARCH="${1:?usage: build-pg-tools.sh <arch> <out_dir>}"
OUT_DIR="${2:?usage: build-pg-tools.sh <arch> <out_dir>}"

: "${PG_VERSION:?PG_VERSION not set}"
: "${PG_SHA256:?PG_SHA256 not set}"
: "${OPENSSL_VERSION:?OPENSSL_VERSION not set}"
: "${OPENSSL_SHA256:?OPENSSL_SHA256 not set}"

case "$ARCH" in
  arm64)  OSSL_TARGET="darwin64-arm64-cc";  ARCH_FLAG="-arch arm64"  ;;
  x86_64) OSSL_TARGET="darwin64-x86_64-cc"; ARCH_FLAG="-arch x86_64" ;;
  *) echo "ERROR: unknown arch '$ARCH' (expected arm64 or x86_64)" >&2; exit 1 ;;
esac

# Building the x86_64 slice on an arm64 host needs Rosetta 2 so configure's
# run-probes (and the smoke test below) can execute the Intel test binaries.
if [ "$ARCH" = "x86_64" ] && [ "$(uname -m)" = "arm64" ]; then
  if ! arch -x86_64 /usr/bin/true >/dev/null 2>&1; then
    echo "ERROR: Rosetta 2 is required to build x86_64 on an arm64 host." >&2
    echo "       Install with: softwareupdate --install-rosetta --agree-to-license" >&2
    exit 1
  fi
fi

JOBS="$(sysctl -n hw.ncpu 2>/dev/null || echo 4)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
OSSL_PREFIX="$WORK/openssl-install"

echo "==> Building pg tools for $ARCH (PG $PG_VERSION, OpenSSL $OPENSSL_VERSION)"

# ── 1. Fetch + verify source tarballs ────────────────────────────────────────
cd "$WORK"
curl -fsSL -o openssl.tar.gz \
  "https://github.com/openssl/openssl/releases/download/openssl-${OPENSSL_VERSION}/openssl-${OPENSSL_VERSION}.tar.gz"
echo "${OPENSSL_SHA256}  openssl.tar.gz" | shasum -a 256 -c -

curl -fsSL -o postgresql.tar.bz2 \
  "https://ftp.postgresql.org/pub/source/v${PG_VERSION}/postgresql-${PG_VERSION}.tar.bz2"
echo "${PG_SHA256}  postgresql.tar.bz2" | shasum -a 256 -c -

# ── 2. Build OpenSSL static (no shared dylibs to ship) ───────────────────────
tar xzf openssl.tar.gz
( cd "openssl-${OPENSSL_VERSION}"
  ./Configure "$OSSL_TARGET" no-shared no-tests no-docs \
    --prefix="$OSSL_PREFIX" --openssldir="$OSSL_PREFIX/ssl"
  make -j"$JOBS"
  make install_sw )

# ── 3. Build libpq + pg_dump/pg_restore (client only; no server, no psql) ─────
tar xjf postgresql.tar.bz2
cd "postgresql-${PG_VERSION}"
./configure \
  CC="cc ${ARCH_FLAG}" \
  --without-readline \
  --without-nls \
  --without-icu \
  --without-zstd \
  --without-lz4 \
  --with-openssl \
  --with-zlib \
  --with-includes="$OSSL_PREFIX/include" \
  --with-libraries="$OSSL_PREFIX/lib"

# generated-headers makes the catalog/*_d.h etc. that pg_dump needs without
# compiling the backend; the two submake targets then build everything else.
make -C src/backend generated-headers
make -C src/interfaces/libpq -j"$JOBS"
make -C src/bin/pg_dump      -j"$JOBS"

# ── 4. Collect into the output tree ──────────────────────────────────────────
rm -rf "$OUT_DIR"
mkdir -p "$OUT_DIR/bin" "$OUT_DIR/lib"
cp src/bin/pg_dump/pg_dump    "$OUT_DIR/bin/"
cp src/bin/pg_dump/pg_restore "$OUT_DIR/bin/"
LIBPQ_SRC="$(find src/interfaces/libpq -maxdepth 1 -name 'libpq.*.dylib' | head -1)"
[ -n "$LIBPQ_SRC" ] || { echo "ERROR: libpq dylib not found after build" >&2; exit 1; }
LIBPQ="$(basename "$LIBPQ_SRC")"
cp "$LIBPQ_SRC" "$OUT_DIR/lib/"

# ── 4b. Capture exact license texts from the built source (for licenses/) ────
LIC="$OUT_DIR/licenses"
mkdir -p "$LIC/postgresql" "$LIC/openssl"
cp "$WORK/postgresql-${PG_VERSION}/COPYRIGHT"     "$LIC/postgresql/COPYRIGHT"
cp "$WORK/openssl-${OPENSSL_VERSION}/LICENSE.txt" "$LIC/openssl/LICENSE.txt"
[ -f "$WORK/openssl-${OPENSSL_VERSION}/NOTICE.txt" ] \
  && cp "$WORK/openssl-${OPENSSL_VERSION}/NOTICE.txt" "$LIC/openssl/NOTICE.txt" || true

# ── 5. Make relocatable: @rpath instead of build-machine paths ───────────────
install_name_tool -id "@rpath/${LIBPQ}" "$OUT_DIR/lib/${LIBPQ}"
for b in pg_dump pg_restore; do
  bin="$OUT_DIR/bin/$b"
  old="$(otool -L "$bin" | awk '/libpq\./{print $1; exit}')"
  [ -n "$old" ] && install_name_tool -change "$old" "@rpath/${LIBPQ}" "$bin"
  install_name_tool -add_rpath "@loader_path/../lib" "$bin"
done

# ── 6. Audit: every dependency must be a system lib or something we ship ──────
audit() {
  local f="$1" bad
  bad="$(otool -L "$f" | tail -n +2 | awk '{print $1}' \
    | grep -vE '^/usr/lib/|^/System/Library/|^@rpath/|^@loader_path/' || true)"
  if [ -n "$bad" ]; then
    echo "AUDIT FAIL: $f references non-system / non-bundled libraries:" >&2
    echo "$bad" >&2
    exit 1
  fi
}
for f in "$OUT_DIR/bin/pg_dump" "$OUT_DIR/bin/pg_restore" "$OUT_DIR/lib/${LIBPQ}"; do
  audit "$f"
  if ! lipo -archs "$f" | tr ' ' '\n' | grep -qx "$ARCH"; then
    echo "ARCH FAIL: $f is not $ARCH (got: $(lipo -archs "$f"))" >&2
    exit 1
  fi
done

# ── 7. Smoke test (x86_64 binary runs under Rosetta on the arm64 runner) ─────
"$OUT_DIR/bin/pg_dump" --version
"$OUT_DIR/bin/pg_restore" --version

echo "==> OK: $ARCH tree at $OUT_DIR"