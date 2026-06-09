#!/usr/bin/env bash
#
# build-pg-tools-linux.sh — build relocatable pg_dump / pg_restore from source (Linux).
#
# Runs inside a manylinux_2_28 container (glibc 2.28) so the result is portable
# across modern distros (Ubuntu 20.04+/Debian 10+/RHEL 8+). Mirrors the macOS
# build-pg-tools.sh but uses ELF tooling (patchelf + readelf) instead of Mach-O.
#
# Usage:  build-pg-tools-linux.sh <out_dir>      e.g. electron/pg-tools/linux
# Required env: PG_VERSION, PG_SHA256, OPENSSL_VERSION, OPENSSL_SHA256
#
# Produces: <out_dir>/bin/{pg_dump,pg_restore} + <out_dir>/lib/{libpq,libssl,libcrypto}.so.*
# Self-contained except system glibc + libz (universal). Built --without readline
# (GPL) / nls (LGPL gettext+iconv) / icu / lz4 / zstd → only PostgreSQL + OpenSSL.

set -euo pipefail
trap 'echo "build-pg-tools-linux.sh: FAILED at line $LINENO (exit $?)" >&2' ERR

OUT_DIR="${1:?usage: build-pg-tools-linux.sh <out_dir>}"
: "${PG_VERSION:?PG_VERSION not set}"
: "${PG_SHA256:?PG_SHA256 not set}"
: "${OPENSSL_VERSION:?OPENSSL_VERSION not set}"
: "${OPENSSL_SHA256:?OPENSSL_SHA256 not set}"

# manylinux base lacks flex/bison (PG build) and some perl modules (OpenSSL build).
PKGS="bison flex zlib-devel perl-IPC-Cmd perl-Data-Dumper perl-Pod-Usage perl-Time-Piece perl-FindBin"
if ! command -v bison >/dev/null 2>&1 || ! command -v flex >/dev/null 2>&1; then
  (yum install -y -q $PKGS || dnf install -y -q $PKGS) >/dev/null 2>&1 || true
fi

sha256c() { command -v sha256sum >/dev/null && sha256sum -c - || shasum -a 256 -c -; }
JOBS="$(nproc 2>/dev/null || echo 4)"
WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT
OSSL_PREFIX="$WORK/openssl-install"

echo "==> Building Linux pg tools (PG $PG_VERSION, OpenSSL $OPENSSL_VERSION; $(ldd --version 2>/dev/null | head -1))"

# ── 1. Fetch + verify ────────────────────────────────────────────────────────
cd "$WORK"
# NB: manylinux_2_28's curl (7.61) predates --retry-all-errors (7.71); use only
# flags it supports. --retry-connrefused (7.52) covers the flaky-connect case.
curl -fsSL --retry 10 --retry-connrefused --retry-delay 15 -o openssl.tar.gz \
  "https://github.com/openssl/openssl/releases/download/openssl-${OPENSSL_VERSION}/openssl-${OPENSSL_VERSION}.tar.gz"
echo "${OPENSSL_SHA256}  openssl.tar.gz" | sha256c
curl -fsSL --retry 10 --retry-connrefused --retry-delay 15 -o postgresql.tar.bz2 \
  "https://ftp.postgresql.org/pub/source/v${PG_VERSION}/postgresql-${PG_VERSION}.tar.bz2"
echo "${PG_SHA256}  postgresql.tar.bz2" | sha256c

# ── 2. OpenSSL (shared) ──────────────────────────────────────────────────────
tar xzf openssl.tar.gz
( cd "openssl-${OPENSSL_VERSION}"
  ./Configure linux-x86_64 shared no-tests no-docs \
    --prefix="$OSSL_PREFIX" --openssldir="$OSSL_PREFIX/ssl"
  make -j"$JOBS"
  make install_sw )
OSSL_LIB="$(dirname "$(find "$OSSL_PREFIX" -name 'libssl.so.3' | head -1)")"

# ── 3. libpq + pg_dump/pg_restore (client only; no server, no psql) ──────────
tar xjf postgresql.tar.bz2
cd "postgresql-${PG_VERSION}"
./configure \
  --without-readline --without-nls --without-icu --without-zstd --without-lz4 \
  --with-openssl --with-zlib \
  --with-includes="$OSSL_PREFIX/include" \
  --with-libraries="$OSSL_LIB"
make -C src/backend generated-headers
make -C src/interfaces/libpq -j"$JOBS"
make -C src/bin/pg_dump      -j"$JOBS"

# ── 4. Collect — copy the REAL file behind each SONAME (no symlinks: safer for
#      electron-builder extraResources packaging) ────────────────────────────
rm -rf "$OUT_DIR"; mkdir -p "$OUT_DIR/bin" "$OUT_DIR/lib"
cp src/bin/pg_dump/pg_dump src/bin/pg_dump/pg_restore "$OUT_DIR/bin/"
bundle() {  # copy real file behind $1 into lib/ named by its SONAME; echo soname
  local real soname
  real="$(readlink -f "$1")"
  soname="$(patchelf --print-soname "$real" 2>/dev/null || true)"; [ -n "$soname" ] || soname="$(basename "$real")"
  cp "$real" "$OUT_DIR/lib/$soname"
  printf '%s\n' "$soname"
}
LIBPQ="$(bundle src/interfaces/libpq/libpq.so.5)"
LIBSSL="$(bundle "$OSSL_LIB/libssl.so.3")"
LIBCRYPTO="$(bundle "$OSSL_LIB/libcrypto.so.3")"

# ── 4b. License texts (PostgreSQL + OpenSSL; --without-nls drops LGPL) ────────
LIC="$OUT_DIR/licenses"; mkdir -p "$LIC/postgresql" "$LIC/openssl"
cp "$WORK/postgresql-${PG_VERSION}/COPYRIGHT"     "$LIC/postgresql/COPYRIGHT"
cp "$WORK/openssl-${OPENSSL_VERSION}/LICENSE.txt" "$LIC/openssl/LICENSE.txt"

# ── 5. Relocate: RPATH=$ORIGIN-relative (strip build-machine paths) ───────────
chmod -R u+w "$OUT_DIR/bin" "$OUT_DIR/lib"
patchelf --set-rpath '$ORIGIN/../lib' "$OUT_DIR/bin/pg_dump" "$OUT_DIR/bin/pg_restore"
patchelf --set-rpath '$ORIGIN'        "$OUT_DIR/lib/$LIBPQ" "$OUT_DIR/lib/$LIBSSL" "$OUT_DIR/lib/$LIBCRYPTO"

# ── 6. Audit: only bundled + universal system libs; RPATH must be $ORIGIN-based ─
ALLOWED='^(libpq\.so|libssl\.so|libcrypto\.so|libc\.so|libm\.so|libdl\.so|libpthread\.so|librt\.so|libz\.so|ld-linux)'
for f in "$OUT_DIR"/bin/pg_dump "$OUT_DIR"/bin/pg_restore "$OUT_DIR"/lib/*; do
  bad="$(readelf -d "$f" | awk '/NEEDED/{gsub(/[][]/,"",$NF);print $NF}' | grep -vE "$ALLOWED" || true)"
  if [ -n "$bad" ]; then echo "AUDIT FAIL: $f needs non-bundled/non-system libs:" >&2; echo "$bad" >&2; exit 1; fi
  rp="$(readelf -d "$f" | grep -E 'R(UN)?PATH' | grep -v '\$ORIGIN' || true)"
  if [ -n "$rp" ]; then echo "AUDIT FAIL: $f has a non-\$ORIGIN RPATH:" >&2; echo "$rp" >&2; exit 1; fi
done

# ── 7. Smoke test ────────────────────────────────────────────────────────────
"$OUT_DIR/bin/pg_dump" --version
"$OUT_DIR/bin/pg_restore" --version
echo "==> OK: linux tree at $OUT_DIR"