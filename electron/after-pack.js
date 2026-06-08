/**
 * afterPack hook.
 *
 * 1. Universal mac builds: no-op for the python-arm64 / python-x64 trees — both
 *    are included in every per-arch temp build so @electron/universal merges
 *    cleanly; main.js picks the right one at runtime via process.arch.
 *
 * 2. Windows: prune the StackBuilder GUI DLLs (wxWidgets + libcurl) that the
 *    embedded PostgreSQL package vendors. postgres.exe does not link them
 *    (verified via PE imports), so removing them shrinks the installer and the
 *    license attribution surface (no wxWindows Licence to ship). Done here, on
 *    the real packaged tree, because the pnpm node_modules layout makes a
 *    pre-build `rm` on a fixed path unreliable (windows-x64 is transitive, not a
 *    top-level entry).
 */

const fs = require("fs");
const path = require("path");

exports.default = async function afterPack(context) {
  if (context.electronPlatformName !== "win32") return;

  const binDir = path.join(
    context.appOutDir,
    "resources", "app.asar.unpacked", "node_modules",
    "@embedded-postgres", "windows-x64", "native", "bin"
  );
  if (!fs.existsSync(binDir)) {
    console.log(`afterPack: ${binDir} not found — nothing to prune`);
    return;
  }

  const prune = (f) => /^wx(base|msw)329u_.*\.dll$/i.test(f) || /^libcurl\.dll$/i.test(f);
  let removed = 0;
  for (const f of fs.readdirSync(binDir)) {
    if (prune(f)) {
      fs.rmSync(path.join(binDir, f));
      removed++;
    }
  }
  console.log(`afterPack: pruned ${removed} StackBuilder DLLs (wxWidgets + libcurl)`);
};