// Copyright (c) 2026 Noah Severs
// SPDX-License-Identifier: BUSL-1.1
//
// Celerp Electron main process.
// Responsibilities:
//   1. Start bundled Postgres (embedded-postgres)
//   2. Run alembic upgrade head
//   3. Start FastAPI (celerp) on a dynamic port
//   4. Start FastHTML UI on a dynamic port
//   5. Open a BrowserWindow pointed at the UI
//   6. Watch for restart sentinel (written by /system/restart) and respawn servers
//   7. Shut everything down cleanly on quit

"use strict";

const { app, BrowserWindow, shell, dialog, ipcMain, Menu, powerSaveBlocker } = require("electron");
const { autoUpdater } = require("electron-updater");
const path = require("path");
const fs = require("fs");
const childProcess = require("child_process");
const { execFileSync } = childProcess;
const net = require("net");
let EmbeddedPostgres; // loaded via dynamic import() - embedded-postgres is ESM-only

// ── Asar path fix for embedded-postgres ─────────────────────────────────────
// embedded-postgres resolves binary paths via __dirname inside app.asar, then
// calls child_process.spawn() on those paths. This fails with ENOTDIR because
// the OS sees app.asar as a file, not a directory.
// Fix: rewrite .asar/ → .asar.unpacked/ before the spawn hits the OS.

function rewriteAsarPath(p) {
  if (typeof p === "string" && p.includes("app.asar") && !p.includes("app.asar.unpacked")) {
    return p.replace(/app\.asar([/\\])/g, "app.asar.unpacked$1");
  }
  return p;
}

// Patch child_process.spawn globally — embedded-postgres imports spawn directly
// from 'child_process', so a local wrapper would not affect it.
const _spawn = childProcess.spawn.bind(childProcess);
childProcess.spawn = function spawn(cmd, args, opts) {
  return _spawn(rewriteAsarPath(cmd), args, opts);
};
const spawn = childProcess.spawn;

// Suppress fs.promises.chmod for embedded-postgres binary paths.
// embedded-postgres calls chmod to ensure its binaries are executable, but:
//   1. The afterPack hook already set +x before signing, so the bits are correct.
//   2. The path it passes is the virtual app.asar path (not app.asar.unpacked),
//      which the OS cannot chmod — it would throw ENOTDIR.
//   3. On a signed/notarized build the OS would throw EPERM anyway.
// Making chmod a no-op is safe: the binaries already have the right permissions.
const _fsPromises = fs.promises;
const _chmod = _fsPromises.chmod.bind(_fsPromises);
_fsPromises.chmod = async function chmod(p, mode) {
  if (typeof p === "string" && p.includes("@embedded-postgres")) return;
  return _chmod(p, mode);
};

// Patch async-exit-hook before embedded-postgres loads so gracefulShutdown(done)
// always receives a callable done. In some Electron exit paths async-exit-hook
// calls hooks without the done callback, causing "TypeError: done is not a function".
{
  const hookPath = require.resolve("async-exit-hook");
  const originalAdd = require(hookPath);
  const patchedAdd = function patchedAdd(hook) {
    return originalAdd(hook.length > 0
      ? function(done) { return hook(typeof done === "function" ? done : () => {}); }
      : hook);
  };
  Object.assign(patchedAdd, originalAdd);
  require.cache[hookPath].exports = patchedAdd;
}

// ── Constants ────────────────────────────────────────────────────────────────

const IS_DEV = !app.isPackaged;

// DEV_MODE: skip bundled Postgres + Python management entirely.
// Set CELERP_DEV_MODE=1 in your shell before running `pnpm start`.
// Expects an already-running FastAPI on DEV_API_PORT (default 8000)
// and FastHTML UI on DEV_UI_PORT (default 8001).
const DEV_MODE = IS_DEV && process.env.CELERP_DEV_MODE === "1";
const DEV_API_PORT = parseInt(process.env.DEV_API_PORT || "8000", 10);
const DEV_UI_PORT = parseInt(process.env.DEV_UI_PORT || "8001", 10);

const APP_DIR = IS_DEV
  ? path.resolve(__dirname, "..")
  : path.join(process.resourcesPath, "app");

const DATA_DIR = path.join(app.getPath("userData"), "celerp-data");
const PG_DATA_DIR = path.join(DATA_DIR, "postgres");
const LOG_DIR = path.join(DATA_DIR, "logs");
const MODULE_DIR = path.join(DATA_DIR, "modules");
const CONFIG_PATH = path.join(DATA_DIR, "celerp-config.json");
const PYTHON_CONFIG_PATH = path.join(DATA_DIR, "config.toml");

// Default modules shipped with the binary (in app resources/default_modules/).
// Copied to MODULE_DIR on first boot if not already present.
const DEFAULT_MODULES_SRC = IS_DEV
  ? path.resolve(__dirname, "../default_modules")
  : path.join(process.resourcesPath, "app", "default_modules");

// ── Globals ──────────────────────────────────────────────────────────────────

let mainWindow = null;
let isQuitting = false; // set true in before-quit so the close handler doesn't intercept Cmd+Q

// C4: hold a power-save assertion (prevent idle system sleep) ONLY while the relay is actually
// serving, so an idle Mac can't drop the connection. The relay state is owned by the Python
// gateway client, which prints `CELERP_RELAY_STATE=<status>` on every transition; we read that
// off the API process stdout below. `prevent-app-suspension` keeps the SYSTEM awake (the display
// may still sleep) and is auto-released when the app exits. It does NOT override lid-close,
// explicit sleep, or low battery - C3's auto-reconnect remains the safety net for those.
let relaySleepBlockerId = null;
function applyRelaySleepState(active) {
  if (active && relaySleepBlockerId === null) {
    relaySleepBlockerId = powerSaveBlocker.start("prevent-app-suspension");
  } else if (!active && relaySleepBlockerId !== null) {
    powerSaveBlocker.stop(relaySleepBlockerId);
    relaySleepBlockerId = null;
  }
}
function consumeRelayStateFromLog(text) {
  // A chunk may carry several transitions; act on the last one only.
  const matches = [...text.matchAll(/CELERP_RELAY_STATE=(\w+)/g)];
  if (matches.length) applyRelaySleepState(matches[matches.length - 1][1] === "active");
}
let pgInstance = null;
let apiProcess = null;
let uiProcess = null;
let apiPort = null;
let uiPort = null;

const { watchForRestart } = require("./restart");
const { migrateArgs } = require("./migrate_cmd");

// ── Utilities ────────────────────────────────────────────────────────────────

/** Find a free TCP port. */
function getFreePort() {
  return new Promise((resolve, reject) => {
    const srv = net.createServer();
    srv.listen(0, "127.0.0.1", () => {
      const { port } = srv.address();
      srv.close(() => resolve(port));
    });
    srv.on("error", reject);
  });
}

/** Poll until a TCP port accepts connections (max `attempts` × `intervalMs`). */
function waitForPort(port, attempts = 60, intervalMs = 500) {
  return new Promise((resolve, reject) => {
    let tries = 0;
    const check = () => {
      const sock = net.createConnection({ port, host: "127.0.0.1" });
      sock.once("connect", () => { sock.destroy(); resolve(); });
      sock.once("error", () => {
        sock.destroy();
        if (++tries >= attempts) reject(new Error(`Port ${port} never opened`));
        else setTimeout(check, intervalMs);
      });
    };
    check();
  });
}

/** Return the directory containing bundled pg_dump / pg_restore binaries.
 *  Dev and non-macOS return "" → the Python side falls back to system PATH.
 *  Packaged macOS ALWAYS returns the bundle path, even if the binary is absent:
 *  a packaged build that can't find its own bundled tools must fail loudly
 *  (Python raises a clear error), not silently dump with a system pg_dump of
 *  unknown version. */
function pgBinDir() {
  if (IS_DEV) return "";                                // dev: fall back to PATH
  if (process.platform === "darwin") {
    return path.join(process.resourcesPath, `pg-${process.arch}`, "bin");
  }
  if (process.platform === "win32") {
    return path.join(process.resourcesPath, "pg-win", "bin");
  }
  if (process.platform === "linux") {
    return path.join(process.resourcesPath, "pg-linux", "bin");
  }
  return "";
}

/** Resolve the Python binary — packaged apps bundle a standalone Python. */
function pythonBin() {
  if (IS_DEV) {
    return path.join(APP_DIR, ".venv", "bin", "python3");
  }
  // Packaged: standalone Python bundled via python-build-standalone.
  // Universal Mac: each arch has its own Python dir (python-arm64 / python-x64)
  // to avoid mach-o merge conflicts. Pick based on process.arch at runtime.
  // Windows layout: resources/python-x64/python/python.exe
  // Linux layout:   resources/python-x64/python/bin/python3
  const archDir = `python-${process.arch}`;
  const base = path.join(process.resourcesPath, archDir, "python");
  return process.platform === "win32"
    ? path.join(base, "python.exe")
    : path.join(base, "bin", "python3");
}

// ── Startup sequence ─────────────────────────────────────────────────────────

async function startPostgres(dbPort) {
  fs.mkdirSync(PG_DATA_DIR, { recursive: true });
  fs.mkdirSync(LOG_DIR, { recursive: true });

  if (!EmbeddedPostgres) {
    EmbeddedPostgres = (await import("embedded-postgres")).default;
  }

  pgInstance = new EmbeddedPostgres({
    databaseDir: PG_DATA_DIR,
    user: "celerp",
    password: "celerp",
    port: dbPort,
    persistent: true,  // data survives across app restarts
  });

  // Only run initdb on first boot — PG_VERSION is written by initdb and
  // signals that the cluster is already initialised. Calling initialise()
  // on an existing data directory causes initdb to exit non-zero and crash.
  const pgVersionFile = path.join(PG_DATA_DIR, "PG_VERSION");
  if (!fs.existsSync(pgVersionFile)) {
    await pgInstance.initialise();
  }
  await pgInstance.start();

  // Create database if it doesn't exist yet
  const client = pgInstance.getPgClient();
  await client.connect();
  try {
    await client.query("CREATE DATABASE celerp;");
  } catch (e) {
    // "already exists" is fine
    if (!e.message.includes("already exists")) throw e;
  } finally {
    await client.end();
  }
}

function runMigrations(dbUrl) {
  const env = {
    ...process.env,
    // Force UTF-8 file I/O — Windows defaults to cp1252, which crashes on the
    // app's UTF-8 data files (locales, config). Harmless on macOS/Linux.
    PYTHONUTF8: "1",
    DATABASE_URL: dbUrl,
    PYTHONPATH: APP_DIR,
    ALEMBIC_VERSION_LOCATIONS: _moduleAlembicLocations(),
  };
  execFileSync(pythonBin(), migrateArgs(dbUrl), {
    cwd: APP_DIR,
    env,
    stdio: "pipe",
  });
}

/** Seed default modules from resources into DATA_DIR/modules/ on first boot. */
function seedDefaultModules() {
  const srcDir = DEFAULT_MODULES_SRC;
  if (!fs.existsSync(srcDir)) return; // Dev mode, modules already on path

  fs.mkdirSync(MODULE_DIR, { recursive: true });

  for (const modName of fs.readdirSync(srcDir)) {
    const src = path.join(srcDir, modName);
    const dst = path.join(MODULE_DIR, modName);
    if (!fs.statSync(src).isDirectory()) continue;
    if (fs.existsSync(dst)) continue; // Already installed — never overwrite user edits
    _copyDirSync(src, dst);
    console.log(`[modules] Seeded default module: ${modName}`);
  }
}

function _copyDirSync(src, dst) {
  fs.mkdirSync(dst, { recursive: true });
  for (const entry of fs.readdirSync(src, { withFileTypes: true })) {
    const s = path.join(src, entry.name);
    const d = path.join(dst, entry.name);
    if (entry.isDirectory()) {
      _copyDirSync(s, d);
    } else {
      fs.copyFileSync(s, d);
    }
  }
}

/** Run pip install for all installed module requirements.txt files. */
function runModuleSetup() {
  const setupScript = path.join(APP_DIR, "scripts", "module_setup.py");
  try {
    execFileSync(pythonBin(), [setupScript, "--data-dir", DATA_DIR], {
      cwd: APP_DIR,
      env: { ...process.env, PYTHONUTF8: "1", PYTHONPATH: APP_DIR },
      stdio: "pipe",
    });
    console.log("[modules] module_setup.py complete");
  } catch (e) {
    // Non-fatal: log and continue. Module will fail to load if deps are missing.
    console.warn("[modules] module_setup.py failed (non-fatal):", e.message);
  }
}

/** Build ALEMBIC_VERSION_LOCATIONS value from installed module migrations. */
function _moduleAlembicLocations() {
  const locations = ["celerp/alembic/versions"]; // core migrations location
  if (!fs.existsSync(MODULE_DIR)) return locations.join(" ");

  for (const modName of fs.readdirSync(MODULE_DIR)) {
    const modPath = path.join(MODULE_DIR, modName);
    if (!fs.statSync(modPath).isDirectory()) continue;
    // Look for migrations/ subdir in any package inside the module
    for (const subdir of fs.readdirSync(modPath)) {
      const migrPath = path.join(modPath, subdir, "migrations");
      if (fs.existsSync(migrPath) && fs.statSync(migrPath).isDirectory()) {
        locations.push(migrPath);
      }
    }
  }
  return locations.join(" ");
}

function startApi(dbUrl, cfg) {
  return new Promise(async (resolve, reject) => {
    // apiPort and uiPort are pre-allocated by the caller before startApi/startUi
    // are invoked, so both values are already set here.
    const env = {
      ...process.env,
      // Scrub Python environment variables that the user's shell may have set
      // (pyenv, conda, Homebrew, virtualenv). If inherited, they redirect the
      // bundled standalone Python to the wrong stdlib and crash uvicorn before
      // it ever binds a port. This is the cause of "Port N never opened" when
      // launching a notarized app from the terminal (Finder launch gets a clean
      // environment and never triggers this).
      PYTHONHOME: undefined,
      PYTHONSTARTUP: undefined,
      VIRTUAL_ENV: undefined,
      CONDA_PREFIX: undefined,
      CONDA_DEFAULT_ENV: undefined,
      PYTHONUTF8: "1",  // UTF-8 file I/O on Windows (cp1252 default crashes on UTF-8 data)
      DATABASE_URL: dbUrl,
      JWT_SECRET: getOrCreateJwtSecret(),
      PYTHONPATH: `${APP_DIR}${path.delimiter}${MODULE_DIR}`,
      MODULE_DIR: MODULE_DIR,
      // Tell the Python module loader that DEFAULT_MODULES_SRC is first-party trusted.
      // Seeded copies in MODULE_DIR inherit trust; without this the loader's BSL AST
      // scan rejects celerp-ai, celerp-connectors, celerp-backup, and celerp-admin.
      CELERP_TRUSTED_MODULE_DIRS: DEFAULT_MODULES_SRC,
      CELERP_DATA_DIR: DATA_DIR,
      CELERP_CONFIG: PYTHON_CONFIG_PATH,
      CELERP_INSTALL_CHANNEL: "electron",
      // C2: make the desktop app version authoritative everywhere. The PC-browser/relay
      // path reads the version from /health (it has no window.celerp), so pass the Electron
      // app version through to the API; /health reports it instead of the Python package
      // version, keeping desktop and relay in agreement.
      CELERP_APP_VERSION: app.getVersion(),
      CELERP_API_PORT: String(apiPort),
      CELERP_UI_PORT: String(uiPort),
      CELERP_PG_BIN_DIR: pgBinDir(),
      ...resolveStorageEnv(cfg),
    };
    apiProcess = spawn(
      pythonBin(),
      ["-m", "uvicorn", "celerp.main:app", "--host", "127.0.0.1", "--port", String(apiPort), "--timeout-graceful-shutdown", "3"],
      { cwd: APP_DIR, env, stdio: "pipe" }
    );
    const apiLog = openProcessLog("api.log");
    const logFile = path.join(LOG_DIR, "api.log");
    let stderr = "";
    apiProcess.stderr.on("data", (d) => { stderr += d.toString(); apiLog?.write(d); });
    apiProcess.stdout.on("data", (d) => {
      const s = d.toString();
      stderr += s; apiLog?.write(d);
      consumeRelayStateFromLog(s);  // C4: hold/release the sleep blocker on relay state changes
    });
    apiProcess.on("error", reject);
    apiProcess.on("exit", (code) => {
      if (code !== 0 && code !== null) reject(new Error(`API process exited (code ${code}). Full log: ${logFile}\n${stderr.slice(-3000)}`));
    });
    waitForPort(apiPort).then(resolve).catch(() =>
      reject(new Error(`API port ${apiPort} never opened. Full log: ${logFile}\n${stderr.slice(-3000)}`))
    );
  });
}

function startUi(dbUrl, cfg) {
  return new Promise(async (resolve, reject) => {
    // uiPort is pre-allocated by the caller; no need to allocate here.
    const env = {
      ...process.env,
      // Scrub Python environment variables that the user's shell may have set.
      // See startApi comment for full explanation.
      PYTHONHOME: undefined,
      PYTHONSTARTUP: undefined,
      VIRTUAL_ENV: undefined,
      CONDA_PREFIX: undefined,
      CONDA_DEFAULT_ENV: undefined,
      API_URL: `http://127.0.0.1:${apiPort}`,
      PYTHONUTF8: "1",  // UTF-8 file I/O on Windows (cp1252 default crashes on UTF-8 data)
      DATABASE_URL: dbUrl,
      JWT_SECRET: getOrCreateJwtSecret(),
      PYTHONPATH: `${APP_DIR}${path.delimiter}${MODULE_DIR}`,
      MODULE_DIR: MODULE_DIR,
      CELERP_TRUSTED_MODULE_DIRS: DEFAULT_MODULES_SRC,
      CELERP_CONFIG: PYTHON_CONFIG_PATH,
      CELERP_UI_PORT: String(uiPort),
      CELERP_API_PORT: String(apiPort),
      CELERP_PG_BIN_DIR: pgBinDir(),
      ...resolveStorageEnv(cfg),
    };
    uiProcess = spawn(
      pythonBin(),
      ["-m", "uvicorn", "ui.app:app", "--host", "127.0.0.1", "--port", String(uiPort), "--timeout-graceful-shutdown", "3"],
      { cwd: APP_DIR, env, stdio: "pipe" }
    );
    const uiLog = openProcessLog("ui.log");
    const logFile = path.join(LOG_DIR, "ui.log");
    let stderr = "";
    uiProcess.stderr.on("data", (d) => { stderr += d.toString(); uiLog?.write(d); });
    uiProcess.stdout.on("data", (d) => { stderr += d.toString(); uiLog?.write(d); });
    uiProcess.on("error", reject);
    uiProcess.on("exit", (code) => {
      if (code !== 0 && code !== null) reject(new Error(`UI process exited (code ${code}). Full log: ${logFile}\n${stderr.slice(-3000)}`));
    });
    waitForPort(uiPort).then(resolve).catch(() =>
      reject(new Error(`UI port ${uiPort} never opened. Full log: ${logFile}\n${stderr.slice(-3000)}`))
    );
  });
}

/** Open a truncating write stream for a child-process log (api.log / ui.log),
 *  so the real Python traceback is always recoverable even if the error dialog
 *  fails to surface it. Returns null if the log can't be opened. */
function openProcessLog(name) {
  try {
    fs.mkdirSync(LOG_DIR, { recursive: true });
    const s = fs.createWriteStream(path.join(LOG_DIR, name), { flags: "w" });
    s.write(`=== ${name} — ${new Date().toISOString()} ===\n`);
    return s;
  } catch (e) {
    console.warn(`Could not open log ${name}: ${e.message}`);
    return null;
  }
}

/** Read or generate a persistent JWT secret stored in userData. */
function getOrCreateJwtSecret() {
  const secretPath = path.join(DATA_DIR, ".jwt_secret");
  fs.mkdirSync(DATA_DIR, { recursive: true });
  if (fs.existsSync(secretPath)) {
    return fs.readFileSync(secretPath, "utf8").trim();
  }
  const secret = require("crypto").randomBytes(32).toString("hex");
  fs.writeFileSync(secretPath, secret, { mode: 0o600 });
  return secret;
}

/** Read persisted config (external DB, storage, feature flags). */
function readConfig() {
  const defaults = {
    db_mode: "local",
    external_db_url: "",
    storage_mode: "local",
    storage_s3_endpoint: "",
    storage_s3_bucket: "",
    storage_s3_access_key: "",
    storage_s3_secret_key: "",
    feature_flags: { external_db: false, external_storage: false, grace_period_ends: null },
  };
  if (!fs.existsSync(CONFIG_PATH)) return defaults;
  try {
    return { ...defaults, ...JSON.parse(fs.readFileSync(CONFIG_PATH, "utf8")) };
  } catch {
    return defaults;
  }
}

/** Persist config changes. */
function writeConfig(patch) {
  fs.mkdirSync(DATA_DIR, { recursive: true });
  const current = readConfig();
  fs.writeFileSync(CONFIG_PATH, JSON.stringify({ ...current, ...patch }, null, 2), { mode: 0o600 });
}

/** Returns true if the SaaS grace period is still active. */
function _isInGrace(flags) {
  return flags.grace_period_ends
    ? new Date(flags.grace_period_ends) > new Date()
    : false;
}

/**
 * Determine active DATABASE_URL based on config + feature flags.
 * Returns { url, useBundledPg } where useBundledPg drives whether
 * embedded Postgres is started.
 */
function resolveDatabaseConfig(dbPort, cfg) {
  const flags = cfg.feature_flags || {};
  const inGrace = _isInGrace(flags);
  const externalAllowed = (flags.external_db || inGrace) && cfg.external_db_url;

  if (externalAllowed && cfg.db_mode === "external") {
    return { url: cfg.external_db_url, useBundledPg: false, gracePeriod: inGrace && !flags.external_db };
  }
  return {
    url: `postgresql+asyncpg://celerp:celerp@localhost:${dbPort}/celerp`,
    useBundledPg: true,
    gracePeriod: false,
  };
}

/** Build storage-related env vars for API and UI processes. */
function resolveStorageEnv(cfg) {
  const flags = cfg.feature_flags || {};
  const storageAllowed = (flags.external_storage || _isInGrace(flags)) && cfg.storage_mode === "s3";

  if (storageAllowed) {
    return {
      STORAGE_BACKEND: "s3",
      STORAGE_S3_ENDPOINT: cfg.storage_s3_endpoint || "",
      STORAGE_S3_BUCKET: cfg.storage_s3_bucket || "",
      STORAGE_S3_ACCESS_KEY: cfg.storage_s3_access_key || "",
      STORAGE_S3_SECRET_KEY: cfg.storage_s3_secret_key || "",
    };
  }
  return { STORAGE_BACKEND: "local" };
}




// ── Auto-updater ─────────────────────────────────────────────────────────────

/**
 * Auto-update via GitHub Releases (electron-updater).
 *
 * Guard: only active in packaged builds. Dev mode skips the updater so
 * a missing GitHub release file doesn't throw noise at the developer.
 *
 * State machine (matches doc section 10):
 *   IDLE -> CHECKING -> DOWNLOADING -> READY -> [admin clicks] -> INSTALLING
 *   Any state -> ERROR on failure (always surfaced to renderer)
 *
 * autoInstallOnAppQuit = false: Squirrel/NSIS never install on normal quit.
 * The ONLY install trigger is an explicit admin action (installUpdate IPC).
 * This eliminates background race conditions entirely.
 *
 * Periodic re-check: every 4 hours while the app is running, in case a new
 * version is released while the user has the app open.
 */
function setupAutoUpdater() {
  if (!app.isPackaged) return;

  autoUpdater.autoDownload = true;
  autoUpdater.autoInstallOnAppQuit = false;
  autoUpdater.allowPrerelease = false;

  function sendLog(msg) {
    if (mainWindow) mainWindow.webContents.send("update-log", String(msg));
  }

  autoUpdater.on("checking-for-update", () => {
    sendLog("Checking for update...");
  });

  autoUpdater.on("update-available", (info) => {
    sendLog("Found v" + info.version + " — downloading...");
    if (mainWindow) mainWindow.webContents.send("update-available", info);
  });

  autoUpdater.on("update-not-available", () => {
    if (mainWindow) mainWindow.webContents.send("update-not-available");
  });

  autoUpdater.on("download-progress", (progress) => {
    // Throttle log output to at most once per second to avoid flooding IPC/DOM.
    // Progress bar updates are sent every tick (just a width change, cheap).
    const now = Date.now();
    if (!autoUpdater._lastProgressLog || now - autoUpdater._lastProgressLog >= 1000) {
      autoUpdater._lastProgressLog = now;
      sendLog(
        "Downloading: " +
          Math.round(progress.percent) +
          "% (" +
          Math.round(progress.bytesPerSecond / 1024) +
          " KB/s)"
      );
    }
    if (mainWindow) mainWindow.webContents.send("download-progress", progress);
  });

  autoUpdater.on("update-downloaded", (info) => {
    sendLog("v" + info.version + " ready — click 'Restart to Install'");
    if (mainWindow) mainWindow.webContents.send("update-downloaded", info);
  });

  autoUpdater.on("error", (err) => {
    // Always surface errors to the renderer — never silently swallow them.
    // Update failures must never interrupt work, but must be visible.
    const msg = err?.message ?? String(err);
    console.error("[updater] error:", msg);
    sendLog("Update error: " + msg);
    if (mainWindow) mainWindow.webContents.send("update-error", { message: msg });
  });

  // Delay initial check until the renderer has loaded and registered its IPC handlers.
  // Firing immediately at app-ready risks emitting update-available/log events before
  // the renderer's ipcRenderer.on(...) calls have run, silently dropping them.
  if (mainWindow) {
    mainWindow.webContents.once("did-finish-load", () => {
      autoUpdater.checkForUpdates().catch(() => {}); // errors handled by the "error" event above
    });
  } else {
    // Fallback: mainWindow not yet created (shouldn't happen in normal flow)
    autoUpdater.checkForUpdates().catch(() => {});
  }

  // Periodic re-check every 4 hours in case a new release ships while app is open.
  setInterval(() => {
    autoUpdater.checkForUpdates().catch(() => {});
  }, 4 * 60 * 60 * 1000);
}

function setLoadingStatus(msg) {
  if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.webContents.executeJavaScript(
      `window.setLoadingStatus && window.setLoadingStatus(${JSON.stringify(msg)})`
    ).catch(() => {});
  }
}

function setupAppMenu() {
  // macOS: build a native menu that includes Uninstall items in the app menu.
  // On other platforms there is no standard app-menu slot; skip.
  if (process.platform !== "darwin") return;

  const template = [
    {
      label: app.name,
      submenu: [
        { role: "about" },
        { type: "separator" },
        { role: "services" },
        { type: "separator" },
        { role: "hide" },
        { role: "hideOthers" },
        { role: "unhide" },
        { type: "separator" },
        {
          label: "Uninstall Celerp…",
          submenu: [
            {
              label: "Quit and Keep Data",
              click: () => ipcMain.emit("_uninstall-keep-data-menu"),
            },
            {
              label: "Quit and Delete All Data…",
              click: () => ipcMain.emit("_uninstall-delete-data-menu"),
            },
          ],
        },
        { type: "separator" },
        { role: "quit" },
      ],
    },
    { role: "editMenu" },
    { role: "viewMenu" },
    { role: "windowMenu" },
  ];

  Menu.setApplicationMenu(Menu.buildFromTemplate(template));

  // Wire menu clicks to the same handlers as the IPC calls
  ipcMain.on("_uninstall-keep-data-menu", () => {
    _doUninstallKeepData();
  });
  ipcMain.on("_uninstall-delete-data-menu", () => {
    _doUninstallDeleteData();
  });
}

// Internal helpers called by both menu and IPC
async function _doUninstallKeepData() {
  const { response } = await dialog.showMessageBox(mainWindow, {
    type: "info",
    buttons: ["Cancel", "Quit Celerp"],
    defaultId: 1,
    cancelId: 0,
    title: "Uninstall Celerp",
    message: "Uninstall Celerp",
    detail:
      "To remove the app, drag Celerp from your Applications folder to the Trash.\n\n" +
      "Your data will be preserved at:\n" +
      `${app.getPath("userData")}\n\n` +
      "Click \"Quit Celerp\" to close the app first, then delete it from Applications.",
  });
  if (response === 1) { isQuitting = true; app.quit(); }
}

async function _doUninstallDeleteData() {
  // Delete only DATA_DIR (celerp-data subdirectory), not the full userData dir.
  // Electron holds open handles on Cache/Code Cache/Network/Persistent State inside
  // userData throughout the process lifetime; deleting userData itself leaves those
  // dirs behind on macOS. DATA_DIR is everything Celerp owns (postgres, modules,
  // config, JWT secret) and is safe to remove while the process is still running.
  const dataPath = DATA_DIR;
  const { response } = await dialog.showMessageBox(mainWindow, {
    type: "warning",
    buttons: ["Cancel", "Delete Data & Quit"],
    defaultId: 0,
    cancelId: 0,
    title: "Uninstall and Delete All Data",
    message: "Delete all Celerp data?",
    detail:
      "This will permanently delete:\n\n" +
      `• ${dataPath}\n\n` +
      "This includes your database, configuration, modules, and all business data. " +
      "This cannot be undone.\n\n" +
      "After quitting, drag Celerp from Applications to Trash to complete removal.",
  });
  if (response === 1) {
    try { fs.rmSync(dataPath, { recursive: true, force: true }); }
    catch (err) { console.error("[uninstall] failed to delete data dir:", err); }
    isQuitting = true;
    app.quit();
  }
}

/**
 * Show an error to the user. Strips verbose Alembic INFO/DEBUG noise so only
 * the actual failure is visible. Uses a scrollable BrowserWindow instead of
 * the native showErrorBox (which clips on macOS with no scroll).
 */
function showError(title, rawMessage) {
  // Never render a blank / "undefined" dialog — fall back to pointing at the logs.
  let raw = rawMessage == null ? "" : String(rawMessage);
  if (raw.trim() === "") {
    raw = `Celerp could not start and no error detail was captured.\n\nCheck the logs in:\n${LOG_DIR}`;
  }
  // Strip alembic INFO/DEBUG lines — keep WARNING/ERROR and anything after them.
  const lines = raw.split("\n");
  const filtered = lines.filter((l) => !/^\s*(INFO|DEBUG)\s+\[/.test(l));
  // If filtering left nothing meaningful, fall back to raw (avoid blank dialog).
  const message = filtered.join("\n").trim() || raw;

  // Short messages: native box is fine (no clipping risk).
  if (message.length < 400 && !message.includes("\n")) {
    dialog.showErrorBox(title, message);
    return;
  }

  // Long messages: scrollable BrowserWindow.
  const errWin = new BrowserWindow({
    width: 720,
    height: 480,
    minWidth: 480,
    minHeight: 280,
    resizable: true,
    title,
    alwaysOnTop: true,
    webPreferences: { nodeIntegration: false, contextIsolation: true },
  });
  const escaped = message
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
  const html = `<!DOCTYPE html><html><head><meta charset="utf-8">
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, sans-serif; background: #1e1e1e; color: #f0f0f0;
         display: flex; flex-direction: column; height: 100vh; }
  h2 { padding: 14px 16px 10px; font-size: 15px; background: #c0392b;
       color: #fff; flex-shrink: 0; }
  pre { flex: 1; overflow: auto; padding: 14px 16px; font-size: 12px;
        font-family: "SF Mono", Menlo, Consolas, monospace; white-space: pre-wrap;
        word-break: break-word; }
  footer { padding: 10px 16px; background: #2a2a2a; flex-shrink: 0; text-align: right; }
  button { padding: 6px 20px; background: #c0392b; color: #fff; border: none;
           border-radius: 4px; font-size: 13px; cursor: pointer; }
  button:hover { background: #a93226; }
</style></head><body>
<h2>${title.replace(/</g, "&lt;")}</h2>
<pre>${escaped}</pre>
<footer><button onclick="window.close()">Close</button></footer>
</body></html>`;
  errWin.loadURL("data:text/html;charset=utf-8," + encodeURIComponent(html));
}

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1280,
    height: 820,
    minWidth: 900,
    minHeight: 600,
    title: "Celerp",
    icon: path.join(__dirname, "assets", "icon.png"),
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
    },
    show: false,
  });

  // Show loading page immediately so there is never a white frame.
  // Main process calls setLoadingStatus() to update the status line
  // while services boot. Once ready, loadURL() switches to the real UI.
  mainWindow.loadFile(path.join(__dirname, "assets", "loading.html"));
  mainWindow.once("ready-to-show", () => mainWindow.show());

  // In Electron (contextIsolation=true), window.confirm() is silently stubbed
  // to false in the renderer. Override it once per page load so ALL confirm()
  // calls — both onclick handlers and htmx hx-confirm attributes — show a real
  // native dialog via the contextBridge IPC.
  mainWindow.webContents.on("did-finish-load", () => {
    mainWindow.webContents.executeJavaScript(`
      (function () {
        if (window.__ceConfirmPatched) return;
        window.__ceConfirmPatched = true;
        window.confirm = function (message) {
          return !!(window.celerp && window.celerp.showConfirm(message));
        };
      })();
    `).catch(() => {}); // ignore if page is being navigated away
  });

  // Open external links in the default browser, not in the app
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    if (!url.startsWith(`http://127.0.0.1:${uiPort}`)) {
      shell.openExternal(url);
      return { action: "deny" };
    }
    return { action: "allow" };
  });

  // On macOS, hide the window instead of destroying it when the user clicks
  // the red X. This matches Telegram's behaviour: app stays alive in the
  // background and re-opens instantly when the user clicks the dock icon.
  // The `before-quit` handler (Cmd+Q) still kills all processes and fully exits.
  mainWindow.on("close", (event) => {
    if (process.platform === "darwin" && !isQuitting) {
      event.preventDefault();
      mainWindow.hide();
    }
  });

  mainWindow.on("closed", () => { mainWindow = null; });
}

// ── IPC handlers ─────────────────────────────────────────────────────────────

// check-for-updates: renderer triggers a manual update check via window.celerp.checkForUpdates()
ipcMain.handle("check-for-updates", () => {
  if (app.isPackaged) autoUpdater.checkForUpdates().catch(() => {}); // errors handled by the "error" event
});

// install-update: renderer triggers quit-and-install via window.celerp.installUpdate()
// ShipIt (Squirrel.Mac) aborts if ANY instance of the app is running when it tries to
// replace the bundle. autoUpdater.quitAndInstall() calls app.quit() internally, but
// Electron's process lingers long enough for ShipIt to see it and cancel. We kill
// subprocesses first, then give them 500ms to exit before handing off to ShipIt.
ipcMain.on("install-update", async () => {
  isQuitting = true;  // prevent the macOS hide-on-close handler from blocking quit
  if (uiProcess) uiProcess.kill();
  if (apiProcess) apiProcess.kill();
  if (pgInstance) {
    try { await pgInstance.stop(); } catch (_) {}
  }
  // Small delay so OS can reap child processes before ShipIt checks
  setTimeout(() => {
    autoUpdater.quitAndInstall(true, true);
  }, 500);
});

// get-version: renderer fetches the current app version
ipcMain.handle("get-version", () => app.getVersion());

// show-confirm: renderer calls window.celerp.showConfirm(message) for hx-confirm
// dialogs. window.confirm() is silently stubbed to false in Electron's renderer,
// so htmx hx-confirm never fires without this native dialog bridge.
ipcMain.on("show-confirm", (event, message) => {
  const result = dialog.showMessageBoxSync(mainWindow, {
    type: "question",
    buttons: ["Cancel", "OK"],
    defaultId: 1,
    cancelId: 0,
    message: String(message),
  });
  event.returnValue = result === 1; // true = OK, false = Cancel
});

// ── Uninstall IPC handlers ───────────────────────────────────────────────────

// uninstall-keep-data: quit without touching user data.
ipcMain.handle("uninstall-keep-data", () => _doUninstallKeepData());

// uninstall-delete-data: delete all user data then quit.
ipcMain.handle("uninstall-delete-data", () => _doUninstallDeleteData());

// ── Single-instance lock ─────────────────────────────────────────────────────

const gotLock = app.requestSingleInstanceLock();
if (!gotLock) {
  app.quit();
} else {
  app.on("second-instance", () => {
    if (mainWindow) {
      if (mainWindow.isMinimized()) mainWindow.restore();
      mainWindow.show();
      mainWindow.focus();
    }
  });
}

// ── App lifecycle ────────────────────────────────────────────────────────────

app.whenReady().then(async () => {
  // ── macOS DMG guard ───────────────────────────────────────────────────────
  // Detect if the user launched Celerp directly from a mounted disk image
  // instead of installing it to /Applications first. The app bundle will be
  // under /Volumes/* which is always read-only; alembic and embedded-postgres
  // will immediately fail with EROFS. Block early with a clear message.
  if (process.platform === "darwin" && app.isPackaged) {
    const appPath = app.getAppPath();
    if (appPath.startsWith("/Volumes/")) {
      dialog.showMessageBoxSync({
        type: "warning",
        title: "Install Celerp First",
        message: "Celerp is running from a disk image and cannot save data.",
        detail:
          "To install Celerp:\n\n" +
          "1. Open Finder and locate the Celerp disk image.\n" +
          "2. Drag Celerp into the Applications folder.\n" +
          "3. Open Celerp from your Applications folder (or Launchpad).\n\n" +
          "You can then eject the disk image.",
        buttons: ["Quit"],
        defaultId: 0,
      });
      app.exit(0);
      return;
    }
  }

  setupAppMenu();
  try {
    if (DEV_MODE) {
      // Skip Postgres/Python management — connect to already-running local services.
      // Usage: CELERP_DEV_MODE=1 pnpm start
      console.log(`[DEV_MODE] Connecting to existing services — API :${DEV_API_PORT}, UI :${DEV_UI_PORT}`);
      apiPort = DEV_API_PORT;
      uiPort = DEV_UI_PORT;
      await waitForPort(apiPort, 5, 500).catch(() => {
        throw new Error(`DEV_MODE: FastAPI not found on port ${apiPort}. Start it first with:\n  uvicorn celerp.main:app --port ${apiPort}`);
      });
      await waitForPort(uiPort, 5, 500).catch(() => {
        throw new Error(`DEV_MODE: UI not found on port ${uiPort}. Start it first with:\n  uvicorn ui.app:app --port ${uiPort}`);
      });
      createWindow();
      mainWindow.loadURL(`http://127.0.0.1:${uiPort}`);
      return;
    }

    const dbPort = await getFreePort();
    const cfg = readConfig();
    const dbConfig = resolveDatabaseConfig(dbPort, cfg);

    // Create the main window immediately so user sees the loading page (no white frame).
    createWindow();

    // Show grace period warning if applicable
    if (dbConfig.gracePeriod) {
      setLoadingStatus("Your Celerp Team subscription has lapsed. External database remains active for up to 15 days. Please renew at celerp.com/subscribe.");
    }

    if (dbConfig.useBundledPg) {
      setLoadingStatus("Starting database…");
      await startPostgres(dbPort);
    }
    setLoadingStatus("Loading modules…");
    seedDefaultModules();
    runModuleSetup();
    setLoadingStatus("Running migrations…");
    runMigrations(dbConfig.url);
    // Pre-allocate both ports so each process env carries both values.
    // GatewayClient runs inside the API process and needs to know the UI port
    // for its reverse-proxy routing; allocating upfront avoids a null race.
    apiPort = await getFreePort();
    uiPort = await getFreePort();
    setLoadingStatus("Starting API server…");
    await startApi(dbConfig.url, cfg);
    setLoadingStatus("Starting UI server…");
    await startUi(dbConfig.url, cfg);

    watchForRestart(dbConfig.url, {
      getApiProcess: () => apiProcess,
      getUiProcess: () => uiProcess,
      setUiProcess: (p) => { uiProcess = p; },
      startApi: async (url) => { apiPort = await getFreePort(); uiPort = await getFreePort(); return startApi(url, readConfig()); },
      startUi: (url) => startUi(url, readConfig()),
      // Sentinel must live next to PYTHON_CONFIG_PATH so Python's config_path().parent
      // resolves to the same directory that Electron watches.
      sentinelPath: path.join(path.dirname(PYTHON_CONFIG_PATH), ".restart_requested"),
      onCrash: (err) => {
        showError("Celerp crashed", err?.message ?? String(err));
        app.quit();
      },
      onRestart: () => {
        // After respawn, API and UI are on new ports. Reload the window to the
        // new uiPort so the browser is no longer pointed at the dead old port.
        if (mainWindow) {
          mainWindow.loadURL(`http://127.0.0.1:${uiPort}`);
        }
      },
    });

    // Switch from loading page to the real UI
    mainWindow.loadURL(`http://127.0.0.1:${uiPort}`);

    if (!IS_DEV) {
      setupAutoUpdater();
    }
  } catch (err) {
    showError("Celerp failed to start", err?.message ?? String(err));
    app.quit();
  }
});

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") app.quit();
});

app.on("activate", () => {
  // Dock icon clicked: show the hidden window (Mac hide-on-close) or create
  // a new one if it was genuinely destroyed (shouldn't normally happen).
  if (mainWindow) mainWindow.show();
  else if (uiPort) createWindow();
});

app.on("before-quit", async () => {
  isQuitting = true;
  applyRelaySleepState(false);  // C4: release the power-save assertion (also auto-released on exit)
  if (uiProcess) uiProcess.kill();
  if (apiProcess) apiProcess.kill();
  if (pgInstance) await pgInstance.stop();
});
