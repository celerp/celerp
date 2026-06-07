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
# Print the failing line on any error (set -e deaths are otherwise silent).
trap 'echo "build-pg-tools.sh: FAILED at line $LINENO (exit $?)" >&2' ERR

# First match of a glob (or empty). Avoids `find ... | head -1`, which under
# `pipefail` can SIGPIPE the producer and silently abort the script.
first_glob() { local m=("$@"); [ -e "${m[0]}" ] && printf '%s\n' "${m[0]}" || true; }

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
curl -fsSL --retry 5 --retry-all-errors --retry-delay 3 -o openssl.tar.gz \
  "https://github.com/openssl/openssl/releases/download/openssl-${OPENSSL_VERSION}/openssl-${OPENSSL_VERSION}.tar.gz"
echo "${OPENSSL_SHA256}  openssl.tar.gz" | shasum -a 256 -c -

curl -fsSL --retry 5 --retry-all-errors --retry-delay 3 -o postgresql.tar.bz2 \
  "https://ftp.postgresql.org/pub/source/v${PG_VERSION}/postgresql-${PG_VERSION}.tar.bz2"
echo "${PG_SHA256}  postgresql.tar.bz2" | shasum -a 256 -c -

# ── 2. Build OpenSSL (shared) ────────────────────────────────────────────────
# Dynamic, NOT static: a static libcrypto pulls _atexit/_pthread_exit into
# libpq.5.dylib, which trips PostgreSQL's libpq-refs-stamp check ("libpq must not
# call exit"). Dynamic linking leaves those as undefined refs (allowed); we ship
# libssl/libcrypto next to libpq and rewrite their install names (§5).
tar xzf openssl.tar.gz
( cd "openssl-${OPENSSL_VERSION}"
  ./Configure "$OSSL_TARGET" shared no-tests no-docs \
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
LIBPQ_SRC="$(first_glob src/interfaces/libpq/libpq.*.dylib)"
[ -n "$LIBPQ_SRC" ] || { echo "ERROR: libpq dylib not found after build" >&2; exit 1; }
LIBPQ="$(basename "$LIBPQ_SRC")"
cp "$LIBPQ_SRC" "$OUT_DIR/lib/"
# Bundle the OpenSSL dylibs libpq dynamically links against (§2).
LIBSSL_SRC="$(first_glob "$OSSL_PREFIX"/lib/libssl.*.dylib)"
LIBCRYPTO_SRC="$(first_glob "$OSSL_PREFIX"/lib/libcrypto.*.dylib)"
[ -n "$LIBSSL_SRC" ] && [ -n "$LIBCRYPTO_SRC" ] || { echo "ERROR: OpenSSL dylibs not found in $OSSL_PREFIX/lib" >&2; exit 1; }
LIBSSL="$(basename "$LIBSSL_SRC")"
LIBCRYPTO="$(basename "$LIBCRYPTO_SRC")"
cp "$LIBSSL_SRC" "$LIBCRYPTO_SRC" "$OUT_DIR/lib/"

# ── 4b. Capture exact license texts from the built source (for licenses/) ────
LIC="$OUT_DIR/licenses"
mkdir -p "$LIC/postgresql" "$LIC/openssl"
cp "$WORK/postgresql-${PG_VERSION}/COPYRIGHT"     "$LIC/postgresql/COPYRIGHT"
cp "$WORK/openssl-${OPENSSL_VERSION}/LICENSE.txt" "$LIC/openssl/LICENSE.txt"
[ -f "$WORK/openssl-${OPENSSL_VERSION}/NOTICE.txt" ] \
  && cp "$WORK/openssl-${OPENSSL_VERSION}/NOTICE.txt" "$LIC/openssl/NOTICE.txt" || true

# ── 5. Make relocatable: @rpath instead of build-machine paths ───────────────
# Set each dylib's own install name and repoint every inter-dependency
# (libpq->ssl/crypto, ssl->crypto, bins->libpq) at @rpath. @loader_path rpaths
# let each file find the others in the bundled lib/ dir on any machine.
repoint() {                     # repoint $1's dep matching regex $2 -> @rpath/$3
  local f="$1" old
  # awk must NOT early-exit: that SIGPIPEs otool and (under pipefail) aborts us.
  old="$(otool -L "$f" | awk -v n="$2" '$1 ~ n && !seen {print $1; seen=1}')"
  # No match is legitimate (e.g. dead_strip_dylibs drops OpenSSL from the bins
  # since only libpq uses it). Must return 0 then — but a real install_name_tool
  # failure still aborts via set -e.
  if [ -n "$old" ]; then
    install_name_tool -change "$old" "@rpath/$3" "$f"
  fi
  return 0
}
install_name_tool -id "@rpath/${LIBCRYPTO}" "$OUT_DIR/lib/${LIBCRYPTO}"
install_name_tool -id "@rpath/${LIBSSL}"    "$OUT_DIR/lib/${LIBSSL}"
repoint "$OUT_DIR/lib/${LIBSSL}" 'libcrypto\.' "$LIBCRYPTO"
install_name_tool -add_rpath "@loader_path" "$OUT_DIR/lib/${LIBSSL}"
install_name_tool -id "@rpath/${LIBPQ}" "$OUT_DIR/lib/${LIBPQ}"
repoint "$OUT_DIR/lib/${LIBPQ}" 'libssl\.'    "$LIBSSL"
repoint "$OUT_DIR/lib/${LIBPQ}" 'libcrypto\.' "$LIBCRYPTO"
install_name_tool -add_rpath "@loader_path" "$OUT_DIR/lib/${LIBPQ}"
for b in pg_dump pg_restore; do
  bin="$OUT_DIR/bin/$b"
  repoint "$bin" 'libpq\.'     "$LIBPQ"
  repoint "$bin" 'libssl\.'    "$LIBSSL"
  repoint "$bin" 'libcrypto\.' "$LIBCRYPTO"
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
for f in "$OUT_DIR/bin/pg_dump" "$OUT_DIR/bin/pg_restore" \
         "$OUT_DIR/lib/${LIBPQ}" "$OUT_DIR/lib/${LIBSSL}" "$OUT_DIR/lib/${LIBCRYPTO}"; do
  audit "$f"
  # No pipe to grep -q here: that would SIGPIPE lipo and (with pipefail) misfire.
  archs="$(lipo -archs "$f")"
  case " $archs " in
    *" $ARCH "*) : ;;
    *) echo "ARCH FAIL: $f is not $ARCH (got: $archs)" >&2; exit 1 ;;
  esac
done

# ── 6b. Re-sign (ad-hoc) ─────────────────────────────────────────────────────
# install_name_tool invalidated the linker-applied signatures, and arm64 macOS
# refuses to run Mach-O with a broken/absent signature (the smoke test below
# would be "Killed: 9"). electron-builder re-signs with the real Developer ID at
# packaging time; this ad-hoc pass just makes them runnable here. Sign dylibs
# before the binaries that load them.
for f in "$OUT_DIR/lib/${LIBCRYPTO}" "$OUT_DIR/lib/${LIBSSL}" "$OUT_DIR/lib/${LIBPQ}" \
         "$OUT_DIR/bin/pg_dump" "$OUT_DIR/bin/pg_restore"; do
  codesign --remove-signature "$f" >/dev/null 2>&1 || true
  codesign -s - -f "$f"
done

# ── 7. Smoke test (x86_64 binary runs under Rosetta on the arm64 runner) ─────
"$OUT_DIR/bin/pg_dump" --version
"$OUT_DIR/bin/pg_restore" --version

echo "==> OK: $ARCH tree at $OUT_DIR"