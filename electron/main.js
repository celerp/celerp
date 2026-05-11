// Copyright (c) 2026 Noah Severs
// SPDX-License-Identifier: BSL-1.1
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

const { app, BrowserWindow, shell, dialog, ipcMain } = require("electron");
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
let pgInstance = null;
let apiProcess = null;
let uiProcess = null;
let apiPort = null;
let uiPort = null;

const { watchForRestart } = require("./restart");

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

/** Resolve the Python binary — packaged apps bundle a standalone Python. */
function pythonBin() {
  if (IS_DEV) {
    return path.join(APP_DIR, ".venv", "bin", "python3");
  }
  // Packaged: standalone Python bundled via python-build-standalone.
  // Windows layout: resources/python/python/python.exe
  // Linux layout:   resources/python/python/bin/python3
  const base = path.join(process.resourcesPath, "python", "python");
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
    DATABASE_URL: dbUrl,
    PYTHONPATH: APP_DIR,
    ALEMBIC_VERSION_LOCATIONS: _moduleAlembicLocations(),
  };
  execFileSync(pythonBin(), ["-m", "alembic", "upgrade", "head"], {
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
      env: { ...process.env, PYTHONPATH: APP_DIR },
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
      DATABASE_URL: dbUrl,
      JWT_SECRET: getOrCreateJwtSecret(),
      PYTHONPATH: `${APP_DIR}:${MODULE_DIR}`,
      MODULE_DIR: MODULE_DIR,
      // Tell the Python module loader that DEFAULT_MODULES_SRC is first-party trusted.
      // Seeded copies in MODULE_DIR inherit trust; without this the loader's BSL AST
      // scan rejects celerp-ai, celerp-connectors, celerp-backup, and celerp-admin.
      CELERP_TRUSTED_MODULE_DIRS: DEFAULT_MODULES_SRC,
      CELERP_DATA_DIR: DATA_DIR,
      CELERP_CONFIG: PYTHON_CONFIG_PATH,
      CELERP_INSTALL_CHANNEL: "electron",
      CELERP_API_PORT: String(apiPort),
      CELERP_UI_PORT: String(uiPort),
      ...resolveStorageEnv(cfg),
    };
    apiProcess = spawn(
      pythonBin(),
      ["-m", "uvicorn", "celerp.main:app", "--host", "127.0.0.1", "--port", String(apiPort), "--timeout-graceful-shutdown", "3"],
      { cwd: APP_DIR, env, stdio: "pipe" }
    );
    let stderr = "";
    apiProcess.stderr.on("data", (d) => { stderr += d.toString(); });
    apiProcess.stdout.on("data", (d) => { stderr += d.toString(); });
    apiProcess.on("error", reject);
    apiProcess.on("exit", (code) => {
      if (code !== 0 && code !== null) reject(new Error(`API process exited (code ${code}):\n${stderr.slice(-2000)}`));
    });
    waitForPort(apiPort).then(resolve).catch(() =>
      reject(new Error(`API port ${apiPort} never opened:\n${stderr.slice(-2000)}`))
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
      DATABASE_URL: dbUrl,
      JWT_SECRET: getOrCreateJwtSecret(),
      PYTHONPATH: `${APP_DIR}:${MODULE_DIR}`,
      MODULE_DIR: MODULE_DIR,
      CELERP_TRUSTED_MODULE_DIRS: DEFAULT_MODULES_SRC,
      CELERP_CONFIG: PYTHON_CONFIG_PATH,
      CELERP_UI_PORT: String(uiPort),
      CELERP_API_PORT: String(apiPort),
      ...resolveStorageEnv(cfg),
    };
    uiProcess = spawn(
      pythonBin(),
      ["-m", "uvicorn", "ui.app:app", "--host", "127.0.0.1", "--port", String(uiPort), "--timeout-graceful-shutdown", "3"],
      { cwd: APP_DIR, env, stdio: "pipe" }
    );
    let stderr = "";
    uiProcess.stderr.on("data", (d) => { stderr += d.toString(); });
    uiProcess.stdout.on("data", (d) => { stderr += d.toString(); });
    uiProcess.on("error", reject);
    uiProcess.on("exit", (code) => {
      if (code !== 0 && code !== null) reject(new Error(`UI process exited (code ${code}):\n${stderr.slice(-2000)}`));
    });
    waitForPort(uiPort).then(resolve).catch(() =>
      reject(new Error(`UI port ${uiPort} never opened:\n${stderr.slice(-2000)}`))
    );
  });
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
 * When active:
 *   - Checks for updates silently on launch
 *   - Downloads in background
 *   - Forwards update events to the renderer via IPC
 *   - Errors are logged only — never surfaced as crashes
 */
// ── Pending-update stamp ─────────────────────────────────────────────────────
// On Mac, autoInstallOnAppQuit hands off to Squirrel which replaces the bundle
// in the background AFTER the app exits. Relaunching before Squirrel finishes
// (typically 1-3 min) runs the old binary, which sees no update applied and
// starts downloading again — creating an infinite loop.
//
// We break the loop with a stamp file:
//   - Written to userData when download completes, recording the pending version.
//   - On next launch, if the stamp exists and app.getVersion() == stamp.from,
//     Squirrel hasn't finished yet — skip checkForUpdates and show a waiting UI.
//   - If the stamp exists and app.getVersion() == stamp.to, install succeeded —
//     delete the stamp and show a "Updated to vX" toast.
const PENDING_UPDATE_STAMP = path.join(app.getPath("userData"), "pending-update.json");

function readPendingUpdateStamp() {
  try {
    return JSON.parse(fs.readFileSync(PENDING_UPDATE_STAMP, "utf8"));
  } catch (_) {
    return null;
  }
}

function writePendingUpdateStamp(fromVersion, toVersion) {
  try {
    fs.writeFileSync(PENDING_UPDATE_STAMP, JSON.stringify({ from: fromVersion, to: toVersion, ts: Date.now() }));
  } catch (_) {}
}

function clearPendingUpdateStamp() {
  try { fs.unlinkSync(PENDING_UPDATE_STAMP); } catch (_) {}
}

function setupAutoUpdater() {
  if (!app.isPackaged) return;

  autoUpdater.autoDownload = true;
  autoUpdater.autoInstallOnAppQuit = true;
  autoUpdater.allowPrerelease = false;

  const currentVersion = app.getVersion();
  const stamp = readPendingUpdateStamp();

  // Check if we just successfully updated
  if (stamp && stamp.to === currentVersion) {
    clearPendingUpdateStamp();
    // Notify renderer of successful update once the window loads
    if (mainWindow) {
      mainWindow.webContents.once("did-finish-load", () => {
        mainWindow.webContents.send("update-just-applied", { version: currentVersion });
      });
    }
  }

  // If stamp exists and version hasn't changed, Squirrel is still working —
  // skip checkForUpdates entirely to avoid the download loop.
  const installPending = stamp && stamp.from === currentVersion;
  if (installPending) {
    console.log("[updater] install pending for v" + stamp.to + " — skipping update check");
    if (mainWindow) {
      mainWindow.webContents.once("did-finish-load", () => {
        mainWindow.webContents.send("update-install-pending", { version: stamp.to });
      });
    }
    return;
  }

  function sendLog(msg) {
    if (mainWindow) mainWindow.webContents.send("update-log", String(msg));
  }

  autoUpdater.on("checking-for-update", () => {
    sendLog("Checking for update...");
  });

  autoUpdater.on("update-available", (info) => {
    sendLog("Found version " + info.version + " — downloading...");
    if (mainWindow) mainWindow.webContents.send("update-available", info);
  });

  autoUpdater.on("update-not-available", () => {
    sendLog("Already up to date.");
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
    sendLog("Download complete — quit Celerp to install v" + info.version);
    writePendingUpdateStamp(currentVersion, info.version);
    if (mainWindow) mainWindow.webContents.send("update-downloaded", info);
  });

  autoUpdater.on("error", (err) => {
    // Log only — update failures must never interrupt the user's work.
    // Also notify renderer so the UI can reset from "Checking..." state.
    const msg = err?.message ?? String(err);
    console.error("[updater] error:", msg);
    sendLog("Error: " + msg);
    if (mainWindow) mainWindow.webContents.send("update-not-available");
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

  mainWindow.loadURL(`http://127.0.0.1:${uiPort}`);

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

// ── App lifecycle ────────────────────────────────────────────────────────────

app.whenReady().then(async () => {
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
      return;
    }

    const dbPort = await getFreePort();
    const cfg = readConfig();
    const dbConfig = resolveDatabaseConfig(dbPort, cfg);

    // Show a loading state while services boot. A splash window can replace this later.
    const loadingWin = new BrowserWindow({
      width: 400, height: 200, frame: false, alwaysOnTop: true, resizable: false,
      webPreferences: { nodeIntegration: false },
    });

    // Show grace period warning if applicable
    const graceBanner = dbConfig.gracePeriod
      ? " Your Celerp Team subscription has lapsed. External database remains active for up to 15 days. Please renew at celerp.com/subscribe."
      : "";
    loadingWin.loadURL(`data:text/html,<body style="font-family:sans-serif;display:flex;align-items:center;justify-content:center;height:100vh;margin:0;background:#111827;color:#fff"><p>Starting Celerp…${graceBanner}</p></body>`);

    if (dbConfig.useBundledPg) {
      await startPostgres(dbPort);
    }
    seedDefaultModules();
    runModuleSetup();
    runMigrations(dbConfig.url);
    // Pre-allocate both ports so each process env carries both values.
    // GatewayClient runs inside the API process and needs to know the UI port
    // for its reverse-proxy routing; allocating upfront avoids a null race.
    apiPort = await getFreePort();
    uiPort = await getFreePort();
    await startApi(dbConfig.url, cfg);
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
        dialog.showErrorBox("Celerp crashed", err?.message ?? String(err));
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

    loadingWin.close();
    createWindow();

    if (!IS_DEV) {
      setupAutoUpdater();
    }
  } catch (err) {
    dialog.showErrorBox("Celerp failed to start", err?.message ?? String(err));
    app.quit();
  }
});

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") app.quit();
});

app.on("activate", () => {
  if (mainWindow === null && uiPort) createWindow();
});

app.on("before-quit", async () => {
  if (uiProcess) uiProcess.kill();
  if (apiProcess) apiProcess.kill();
  if (pgInstance) await pgInstance.stop();
});
