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
//   6. Shut everything down cleanly on quit

"use strict";

const { app, BrowserWindow, shell, dialog } = require("electron");
const { autoUpdater } = require("electron-updater");
const path = require("path");
const { spawn, execFileSync } = require("child_process");
const net = require("net");
const EmbeddedPostgres = require("embedded-postgres");

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
  const fs = require("fs");
  fs.mkdirSync(PG_DATA_DIR, { recursive: true });
  fs.mkdirSync(LOG_DIR, { recursive: true });

  pgInstance = new EmbeddedPostgres({
    databaseDir: PG_DATA_DIR,
    user: "celerp",
    password: "celerp",
    port: dbPort,
    persistent: true,  // data survives across app restarts
  });

  await pgInstance.initialise();
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
  const fs = require("fs");
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
  const fs = require("fs");
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
  const fs = require("fs");
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

function startApi(dbUrl) {
  return new Promise(async (resolve, reject) => {
    apiPort = await getFreePort();
    const env = {
      ...process.env,
      DATABASE_URL: dbUrl,
      JWT_SECRET: getOrCreateJwtSecret(),
      PYTHONPATH: `${APP_DIR}:${MODULE_DIR}`,
      MODULE_DIR: MODULE_DIR,
      CELERP_DATA_DIR: DATA_DIR,
      ...resolveStorageEnv(),
    };
    apiProcess = spawn(
      pythonBin(),
      ["-m", "uvicorn", "celerp.main:app", "--host", "127.0.0.1", "--port", String(apiPort)],
      { cwd: APP_DIR, env, stdio: "pipe" }
    );
    apiProcess.on("error", reject);
    waitForPort(apiPort).then(resolve).catch(reject);
  });
}

function startUi(dbUrl) {
  return new Promise(async (resolve, reject) => {
    uiPort = await getFreePort();
    const env = {
      ...process.env,
      API_URL: `http://127.0.0.1:${apiPort}`,
      DATABASE_URL: dbUrl,
      JWT_SECRET: getOrCreateJwtSecret(),
      PYTHONPATH: `${APP_DIR}:${MODULE_DIR}`,
      MODULE_DIR: MODULE_DIR,
      ...resolveStorageEnv(),
    };
    uiProcess = spawn(
      pythonBin(),
      ["-m", "uvicorn", "ui.app:app", "--host", "127.0.0.1", "--port", String(uiPort)],
      { cwd: APP_DIR, env, stdio: "pipe" }
    );
    uiProcess.on("error", reject);
    waitForPort(uiPort).then(resolve).catch(reject);
  });
}

/** Read or generate a persistent JWT secret stored in userData. */
function getOrCreateJwtSecret() {
  const fs = require("fs");
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
  const fs = require("fs");
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
  const fs = require("fs");
  fs.mkdirSync(DATA_DIR, { recursive: true });
  const current = readConfig();
  fs.writeFileSync(CONFIG_PATH, JSON.stringify({ ...current, ...patch }, null, 2), { mode: 0o600 });
}

/**
 * Determine active DATABASE_URL based on config + feature flags.
 * Returns { url, useBundledPg } where useBundledPg drives whether
 * embedded Postgres is started.
 */
function resolveDatabaseConfig(dbPort) {
  const cfg = readConfig();
  const flags = cfg.feature_flags || {};
  const now = new Date();

  // External DB is active if:
  //  1. Feature flag is enabled AND db_mode is "external" AND a URL is configured
  //  2. OR we are within the grace period (lapse ≤ 15 days ago)
  const inGrace = flags.grace_period_ends
    ? new Date(flags.grace_period_ends) > now
    : false;
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
function resolveStorageEnv() {
  const cfg = readConfig();
  const flags = cfg.feature_flags || {};
  const now = new Date();
  const inGrace = flags.grace_period_ends ? new Date(flags.grace_period_ends) > now : false;
  const storageAllowed = (flags.external_storage || inGrace) && cfg.storage_mode === "s3";

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

/** Run demo seed on first boot (if DB is fresh). Runs in background after UI opens. */
async function maybeRunSeed(dbUrl) {
  const fs = require("fs");
  const seedFlagPath = path.join(DATA_DIR, ".seed_done");
  if (fs.existsSync(seedFlagPath)) return;

  // Wait a moment for API to be fully ready
  await new Promise(r => setTimeout(r, 2000));

  try {
    execFileSync(
      pythonBin(),
      ["scripts/seed_demo.py"],
      {
        cwd: APP_DIR,
        env: {
          ...process.env,
          DATABASE_URL: dbUrl,
          JWT_SECRET: getOrCreateJwtSecret(),
          PYTHONPATH: APP_DIR,
          API_BASE: `http://127.0.0.1:${apiPort}`,
        },
        stdio: "pipe",
      }
    );
    fs.writeFileSync(seedFlagPath, new Date().toISOString());
  } catch (e) {
    // Seed failure is non-fatal — user can always import their own data
    console.error("Demo seed failed (non-fatal):", e.message);
  }
}



// ── Auto-updater ─────────────────────────────────────────────────────────────

/**
 * Auto-update via GitHub Releases (electron-updater).
 *
 * Guard: only active when CELERP_UPDATE_ENABLED=true (set at public launch).
 * This keeps the updater a no-op during private dev / pre-release builds so
 * a missing GitHub release file doesn't throw noise at the user.
 *
 * When active:
 *   - Checks for updates silently on launch
 *   - Downloads in background
 *   - Shows a non-blocking dialog when ready to install
 *   - Errors are logged only — never surfaced as crashes
 */
function setupAutoUpdater() {
  if (process.env.CELERP_UPDATE_ENABLED !== "true") return;

  autoUpdater.autoDownload = true;
  autoUpdater.autoInstallOnAppQuit = true;

  autoUpdater.on("update-downloaded", (info) => {
    dialog.showMessageBox(mainWindow, {
      type: "info",
      title: "Update ready",
      message: `Celerp ${info.version} has been downloaded. It will be installed when you quit.`,
      buttons: ["Restart now", "Later"],
      defaultId: 0,
    }).then(({ response }) => {
      if (response === 0) autoUpdater.quitAndInstall();
    });
  });

  autoUpdater.on("error", (err) => {
    // Log only — update failures must never interrupt the user's work
    console.error("[updater] error:", err.message);
  });

  autoUpdater.checkForUpdates();
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
    const dbConfig = resolveDatabaseConfig(dbPort);

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
    await startApi(dbConfig.url);
    await startUi(dbConfig.url);

    loadingWin.close();
    createWindow();

    // Seed demo data on first boot (non-blocking)
    maybeRunSeed(dbConfig.url);

    if (!IS_DEV) {
      setupAutoUpdater();
    }
  } catch (err) {
    dialog.showErrorBox("Celerp failed to start", err.message);
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
