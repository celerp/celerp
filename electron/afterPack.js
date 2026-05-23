/**
 * afterPack hook — removes the wrong-arch Python dir from each arch temp build
 * before @electron/universal merges them.
 *
 * electron-builder copies ALL extraResources (both python-arm64 and python-x64)
 * into every arch's temp .app. The universal merger then sees different .so counts
 * and fails. This hook deletes the non-matching dir so each temp .app only carries
 * its own arch's Python.
 *
 * arch enum (electron-builder): ia32=0, x64=1, armv7l=2, arm64=3, universal=4
 */
const path = require("path");
const fs = require("fs");

exports.default = async function afterPack(context) {
  const { appOutDir, arch } = context;

  // Only applies to macOS per-arch temp builds (not universal=4, not Linux/Win)
  if (context.packager.platform.name !== "mac") return;
  if (arch === 4) return; // universal pass — nothing to do here

  const resourcesPath = path.join(
    appOutDir,
    "Celerp.app",
    "Contents",
    "Resources"
  );

  const toRemove =
    arch === 3 // arm64 build: remove x64 Python
      ? path.join(resourcesPath, "python-x64")
      : path.join(resourcesPath, "python-arm64"); // x64 build: remove arm64 Python

  if (fs.existsSync(toRemove)) {
    fs.rmSync(toRemove, { recursive: true, force: true });
    console.log(
      `afterPack: removed ${path.basename(toRemove)} from arch=${arch} build`
    );
  }
};
