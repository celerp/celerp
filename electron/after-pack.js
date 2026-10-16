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
 * 3. Windows: remove every entry in win-prune.json: Python build and GUI
 *    files, second copies of the app in site-packages, package test trees,
 *    PostgreSQL import libraries and translations, the StackBuilder GUI DLLs
 *    (postgres.exe does not link them, verified via PE imports) and a duplicate
 *    licence file. Fewer files make the installer faster to extract and copy.
 *    Each entry is removed only after checking it belongs to its named owner;
 *    a missing or foreign entry stops the build. Done here, on the real
 *    packaged tree, because the pnpm node_modules layout makes a pre-build
 *    `rm` on a fixed path unreliable (windows-x64 is transitive, not a
 *    top-level entry).
 *
 * 4. Windows: copy the Visual C++ runtime (vcruntime140.dll, vcruntime140_1.dll, msvcp140.dll) from
 *    the folder named by CELERP_VC_RUNTIME_DIR into the embedded server's bin
 *    folder and the pg-win tools bin folder. The PostgreSQL binaries link it, and
 *    a PC without the Visual C++ Redistributable has no copy of its own. A
 *    Windows build without it stops here rather than ship a database that cannot
 *    start.
 */

const crypto = require("crypto");
const fs = require("fs");
const path = require("path");
const PRUNE = require("./win-prune.json");

const VC_RUNTIME_DLLS = ["vcruntime140.dll", "vcruntime140_1.dll", "msvcp140.dll"];

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

  pruneWindows(context.appOutDir);
  const binDir = path.join(context.appOutDir, PRUNE.postgres, "bin");

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

const inside = (p, root) => p === root || p.startsWith(root + "/");

function filesUnder(full) {
  if (!fs.statSync(full).isDirectory()) return [full];
  return fs.readdirSync(full).flatMap((name) => filesUnder(path.join(full, name)));
}

function sha256(file) {
  return crypto.createHash("sha256").update(fs.readFileSync(file)).digest("hex");
}

// Every file a site-packages distribution installed, as listed in its RECORD.
function recordOf(appOutDir, owner) {
  const site = path.join(appOutDir, PRUNE.sitePackages);
  const norm = (n) => n.toLowerCase().replace(/[-_.]+/g, "_");
  const dist = fs.readdirSync(site).find((d) => d.endsWith(".dist-info") && norm(d.split("-")[0]) === norm(owner));
  if (!dist) throw new Error(`afterPack: no ${owner} distribution in site-packages`);
  const lines = fs.readFileSync(path.join(site, dist, "RECORD"), "utf8").split(/\r?\n/);
  return new Set(lines.filter(Boolean).map((l) => path.join(site, l.split(",")[0])));
}

// Throws unless every file under `full` belongs to the entry's owner.
function checkOwner(appOutDir, entry, full) {
  const rel = entry.path;
  if (entry.owner === "python") {
    if (!inside(rel, PRUNE.python) || inside(rel, PRUNE.sitePackages)) throw new Error(`afterPack: ${rel} is not part of the Python runtime`);
  } else if (entry.owner === "postgres") {
    if (!inside(rel, PRUNE.postgres)) throw new Error(`afterPack: ${rel} is not part of the PostgreSQL server`);
  } else if (entry.owner === "duplicate") {
    const kept = path.join(appOutDir, entry.sameAs);
    if (!fs.existsSync(kept) || sha256(kept) !== sha256(full)) throw new Error(`afterPack: ${rel} is not identical to ${entry.sameAs}`);
  } else {
    if (!inside(rel, PRUNE.sitePackages)) throw new Error(`afterPack: ${rel} is not in site-packages`);
    const record = recordOf(appOutDir, entry.owner);
    const foreign = filesUnder(full).filter((f) => !record.has(f));
    if (foreign.length) throw new Error(`afterPack: ${foreign[0]} under ${rel} is not installed by ${entry.owner}`);
  }
}

function pruneWindows(appOutDir) {
  let files = 0;
  let bytes = 0;
  for (const entry of PRUNE.entries) {
    const full = path.join(appOutDir, entry.path);
    if (!fs.existsSync(full)) throw new Error(`afterPack: ${entry.path} is listed in win-prune.json but not in the package`);
    checkOwner(appOutDir, entry, full);
    const targets = entry.match
      ? fs.readdirSync(full).filter((f) => new RegExp(entry.match, "i").test(f)).map((f) => path.join(full, f))
      : [full];
    if (!targets.length) throw new Error(`afterPack: nothing in ${entry.path} matches ${entry.match}`);
    for (const t of targets) {
      for (const f of filesUnder(t)) {
        files++;
        bytes += fs.statSync(f).size;
      }
      fs.rmSync(t, { recursive: true });
    }
  }
  console.log(`afterPack: removed ${files} files (${bytes} bytes) listed in win-prune.json`);
}
