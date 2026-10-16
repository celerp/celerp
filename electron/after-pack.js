/**
 * afterPack hook.
 *
 * 1. All platforms: prune the duplicate `licenses/` folder that rides along
 *    inside each bundled pg-tools tree (pg-arm64/pg-x64/pg-linux). Those trees
 *    are copied wholesale via extraResources, so their per-tree license copy is
 *    redundant — the authoritative attribution lives in resources/licenses/.
 *
 * 2. Universal mac builds: no-op for the python-arm64 / python-x64 trees — both
 *    are included in every per-arch temp build so @electron/universal merges
 *    cleanly; app-main.js picks the right one at runtime via process.arch.
 *
 * 3. Windows: prune the StackBuilder GUI DLLs (wxWidgets + libcurl) that the
 *    embedded PostgreSQL package vendors. postgres.exe does not link them
 *    (verified via PE imports), so removing them shrinks the installer and the
 *    license attribution surface (no wxWindows Licence to ship). Done here, on
 *    the real packaged tree, because the pnpm node_modules layout makes a
 *    pre-build `rm` on a fixed path unreliable (windows-x64 is transitive, not a
 *    top-level entry).
 *
 * 4. Windows: copy the Visual C++ runtime (vcruntime140.dll, msvcp140.dll) from
 *    the folder named by CELERP_VC_RUNTIME_DIR into the embedded server's bin
 *    folder and the pg-win tools bin folder. The PostgreSQL binaries link it, and
 *    a PC without the Visual C++ Redistributable has no copy of its own. A
 *    Windows build without it stops here rather than ship a database that cannot
 *    start.
 */

const fs = require("fs");
const path = require("path");

const VC_RUNTIME_DLLS = ["vcruntime140.dll", "msvcp140.dll"];

exports.default = async function afterPack(context) {
  const plat = context.electronPlatformName;

  // Resources dir differs by platform: mac nests it inside the .app bundle.
  let resourcesDir;
  if (plat === "darwin") {
    const appDir = fs.readdirSync(context.appOutDir).find((f) => f.endsWith(".app"));
    resourcesDir = appDir && path.join(context.appOutDir, appDir, "Contents", "Resources");
  } else {
    resourcesDir = path.join(context.appOutDir, "resources");
  }

  // 1. Prune duplicate pg-*/licenses/ (authoritative copy is resources/licenses/).
  if (resourcesDir && fs.existsSync(resourcesDir)) {
    for (const entry of fs.readdirSync(resourcesDir)) {
      if (!/^pg-/.test(entry)) continue;
      const lic = path.join(resourcesDir, entry, "licenses");
      if (fs.existsSync(lic)) {
        fs.rmSync(lic, { recursive: true, force: true });
        console.log(`afterPack: pruned duplicate ${entry}/licenses`);
      }
    }
  }

  if (plat !== "win32") return;

  const binDir = path.join(
    context.appOutDir,
    "resources", "app.asar.unpacked", "node_modules",
    "@embedded-postgres", "windows-x64", "native", "bin"
  );
  if (!fs.existsSync(binDir)) {
    throw new Error(`afterPack: the embedded PostgreSQL bin folder is missing: ${binDir}`);
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

  const runtimeDir = process.env.CELERP_VC_RUNTIME_DIR;
  if (!runtimeDir) {
    throw new Error("afterPack: CELERP_VC_RUNTIME_DIR is not set; it names the folder holding the Visual C++ runtime DLLs");
  }
  for (const dll of VC_RUNTIME_DLLS) {
    const source = path.join(runtimeDir, dll);
    if (!fs.existsSync(source)) throw new Error(`afterPack: ${dll} is not in ${runtimeDir}`);
    for (const dir of [binDir, path.join(resourcesDir, "pg-win", "bin")]) fs.copyFileSync(source, path.join(dir, dll));
  }
  console.log(`afterPack: copied ${VC_RUNTIME_DLLS.join(", ")} next to the PostgreSQL binaries`);
};