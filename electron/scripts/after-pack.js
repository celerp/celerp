// afterPack hook for electron-builder.
// Fixes execute bits on embedded-postgres binaries in app.asar.unpacked.
//
// WHY: npm tarballs do not guarantee execute bits on binary files. pnpm
// installs @embedded-postgres/darwin-arm64 without +x on the pg binaries.
// electron-builder copies whatever mode it finds into app.asar.unpacked.
// At runtime, embedded-postgres calls fs.stat (which Electron redirects to
// the real unpacked path) and finds the bits missing, then calls fs.chmod
// on the virtual asar path - which fails with ENOTDIR because app.asar is
// a file, not a directory. On a signed/notarized build the chmod would also
// fail with EPERM even if the path were correct.
//
// FIX: set +x on the real files in app.asar.unpacked here, after packing but
// before signing. The bits are then baked into the signature, so at runtime
// embedded-postgres finds them already set and never calls chmod.

"use strict";

const fs = require("fs");
const path = require("path");

exports.default = async function afterPack(context) {
  if (context.electronPlatformName !== "darwin") return;

  const unpackedDir = path.join(
    context.appOutDir,
    `${context.packager.appInfo.productFilename}.app`,
    "Contents",
    "Resources",
    "app.asar.unpacked",
    "node_modules",
    "@embedded-postgres"
  );

  if (!fs.existsSync(unpackedDir)) {
    console.log(`afterPack: ${unpackedDir} not found, skipping chmod`);
    return;
  }

  // Walk all subdirs of @embedded-postgres looking for native/bin/*
  const entries = fs.readdirSync(unpackedDir);
  for (const pkg of entries) {
    const binDir = path.join(unpackedDir, pkg, "native", "bin");
    if (!fs.existsSync(binDir)) continue;
    for (const bin of fs.readdirSync(binDir)) {
      const binPath = path.join(binDir, bin);
      const stat = fs.statSync(binPath);
      if (!stat.isFile()) continue;
      const desired = stat.mode | 0o111; // set execute for owner/group/other
      if (stat.mode !== desired) {
        fs.chmodSync(binPath, desired);
        console.log(`afterPack: chmod +x ${binPath}`);
      }
    }
  }
};
