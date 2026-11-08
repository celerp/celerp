// Copyright (c) 2026 Noah Severs
// SPDX-License-Identifier: BUSL-1.1
"use strict";

const fs = require("fs");
const os = require("os");
const path = require("path");
const { EventEmitter } = require("events");
const { loadAppMain } = require("./app-main-loader.lib");
const {
  DOWNLOAD_URL, HELP_URL, REFUSED_EXIT, UPDATE_FAILURES, compatibilityArgs, readDecision, refusalView, invalidRecordDialog, mayOpenData,
} = require("../compatibility");
const { translator } = require("../locale");
const { openData, reopenData } = require("../boot");

const NEWER = {
  status: REFUSED_EXIT,
  stdout: JSON.stringify({ status: "newer_app", running: "2.5.3", recorded: "2.6.0", revisions: [],
                           message: "..." }) + "\n",
};
const UNKNOWN = {
  status: REFUSED_EXIT,
  stdout: JSON.stringify({ status: "unknown_schema", running: "2.5.3", recorded: null,
                           revisions: ["ffff00c0ffee"], message: "..." }),
};
const INVALID = {
  status: REFUSED_EXIT,
  stdout: JSON.stringify({ status: "invalid_version_record", running: "2.5.3", recorded: "banana", revisions: [] }),
};
const COMPATIBLE = {
  status: 0,
  stdout: "a log line first\n" + JSON.stringify({ status: "compatible", running: "2.5.3", recorded: "2.5.3" }),
};

test("asks the celerp CLI, with the database URL verbatim", function test_compatibility_args() {
  expect(compatibilityArgs("postgresql://x/y")).toEqual(["-m", "celerp", "compatibility", "--db-url", "postgresql://x/y"]);
});

describe("readDecision", () => {
  test("exit 0 is compatible, exit 3 a refusal with its reason", function test_read_decision() {
    expect(readDecision(COMPATIBLE).ok).toBe(true);
    expect(readDecision(NEWER)).toMatchObject({ ok: false, status: "newer_app", recorded: "2.6.0", running: "2.5.3" });
    expect(readDecision(UNKNOWN)).toMatchObject({ ok: false, status: "unknown_schema" });
  });

  test("a check that could not run is an error, never a pass", function test_read_decision_fails_closed() {
    for (const result of [
      { status: 1, stdout: "", stderr: "Traceback\nOperationalError: connection refused" },
      { status: null, error: new Error("spawn ENOENT") },
      { status: 0, stdout: "not json" },
      { status: REFUSED_EXIT, stdout: "" },
      { status: 0, stdout: JSON.stringify({ ok: true }) },
    ]) {
      expect(() => readDecision(result)).toThrow(/could not check your data before opening it/);
    }
    expect(() => readDecision({ status: 1, stderr: "x\nconnection refused" })).toThrow(/connection refused$/);
  });
});

describe("refusal screen", () => {
  const t = translator(path.join(__dirname, "..", "..", "ui", "locales"), "en");

  test("newer data names both versions and offers the update", function test_view_newer() {
    expect(refusalView(readDecision(NEWER), t)).toEqual({
      message: "Your data was last opened with Celerp 2.6.0, which is newer than this copy (2.5.3).",
      detail: "Your data has not been changed. Update Celerp to open it.",
      update: "Update Celerp",
      restart: "Restart to install",
      download: "Download the installer",
      downloadUrl: DOWNLOAD_URL,
      quit: "Quit",
      checking: "Checking...",
      downloading: "Downloading... {pct}%",
      ready: "v{version} ready to install",
      noUpdate: "No newer version is available yet.",
      notReleased: "Your data has not been changed. Celerp can open it once version 2.6.0 or newer is available.",
      checkAgain: "Check again",
      restarting: "Restarting...",
      installing: "Installing v{version}. Celerp will be back in about a minute.",
      failed: {
        check: "Celerp couldn't check for updates. Check your internet connection and try again later.",
        download: "Update download failed",
        save: "Celerp downloaded the update, but Windows would not let Celerp save it.",
        verify: "The downloaded update did not pass its security check, so Celerp did not use it.",
        install: "Celerp could not start the installer.",
        stopped: "The update stopped before it finished, so Celerp is still on the version you had.",
        inactive: "This copy of Celerp cannot update itself. Download the installer and run it yourself.",
      },
    });
  });

  test("on Windows the update installs in its setup window, which takes longer", function test_view_windows() {
    expect(refusalView(readDecision(NEWER), t, true).installing).toBe(
      "Installing v{version}. Celerp will be back in about two minutes.");
  });

  test("an unknown schema was written by a newer Celerp", function test_view_unknown() {
    const view = refusalView(readDecision(UNKNOWN), t);
    expect(view.message).toBe("Your data was last opened with a newer version of Celerp than this copy (2.5.3).");
    expect(view.notReleased).toBe("Your data has not been changed. Celerp can open it once a newer version is available.");
  });

  test("every string is translated in every locale", function test_view_translated() {
    const locales = fs.readdirSync(path.join(__dirname, "..", "..", "ui", "locales")).map((f) => f.replace(".json", ""));
    for (const locale of locales.filter((l) => l !== "en")) {
      const own = translator(path.join(__dirname, "..", "..", "ui", "locales"), locale);
      const view = refusalView(readDecision(NEWER), own);
      const english = refusalView(readDecision(NEWER), t);
      for (const key of ["message", "detail", "update", "quit", "noUpdate", "notReleased", "restarting", "installing"]) {
        expect([locale, view[key]]).not.toEqual([locale, english[key]]);
      }
      for (const failure of UPDATE_FAILURES) {
        expect([locale, failure, view.failed[failure]]).not.toEqual([locale, failure, english.failed[failure]]);
      }
      expect(view.notReleased).toContain("2.6.0");
      expect(refusalView(readDecision(UNKNOWN), own).notReleased)
        .not.toBe(refusalView(readDecision(UNKNOWN), t).notReleased);
      expect(view.message).toContain("2.6.0");
      expect(invalidRecordDialog(own).message).not.toBe(invalidRecordDialog(t).message);
    }
  });

  test("an unreadable record says so, with Get help and Quit", function test_dialog_invalid() {
    expect(invalidRecordDialog(t)).toMatchObject({
      message: "Celerp cannot read which version last opened your data, so it has not opened it. " +
               "Your data has not been changed.",
      buttons: ["Get help on GitHub Issues", "Quit"],
      cancelId: 1,
    });
  });
});

describe("mayOpenData", () => {
  async function run(result, choice) {
    const calls = [];
    const ok = await mayOpenData({
      check: () => result,
      t: (key) => key,
      stop: async () => { calls.push("stop"); },
      offerUpdate: async (view) => { calls.push(`offer:${view.message}`); },
      dialog: { showMessageBox: async (o) => { calls.push(`dialog:${o.message}`); return { response: choice }; } },
      shell: { openExternal: async (u) => { calls.push(`open:${u}`); } },
      quit: () => calls.push("quit"),
    });
    return { ok, calls };
  }

  test("compatible data opens and nothing stops", async function test_may_open_compatible() {
    expect(await run(COMPATIBLE, 0)).toEqual({ ok: true, calls: [] });
  });

  test("newer data stops the database and offers the update; Celerp stays open",
    async function test_may_open_offers_update() {
      expect(await run(NEWER, 0)).toEqual({ ok: false, calls: ["stop", "offer:shell.data_newer"] });
      expect(await run(UNKNOWN, 0)).toEqual({ ok: false, calls: ["stop", "offer:shell.data_newer_schema"] });
    });

  test("an unreadable record gets its dialog, then quits; Get help opens the issues page",
    async function test_may_open_invalid_record() {
      expect(await run(INVALID, 0)).toEqual({
        ok: false, calls: ["stop", "dialog:shell.data_invalid_record", `open:${HELP_URL}`, "quit"] });
      expect(await run(INVALID, 1)).toEqual({ ok: false, calls: ["stop", "dialog:shell.data_invalid_record", "quit"] });
    });
});

describe("boot order", () => {
  const STEPS = ["applyDbModePersist", "applyStoragePersist", "seedDefaultModules",
                 "runMigrations", "startApi", "startUi"];

  function steps(allowed, { bundled = true } = {}) {
    const ran = [];
    const s = { mayOpenData: async () => { ran.push("check"); return allowed; } };
    if (bundled) s.startPostgres = async () => { ran.push("startPostgres"); };
    for (const name of STEPS) s[name] = async () => { ran.push(name); };
    return { s, ran };
  }

  test("cold start checks the database after starting it and before anything else",
    async function test_boot_order_compatible() {
      const { s, ran } = steps(true);
      expect(await openData(s)).toBe(true);
      expect(ran).toEqual(["startPostgres", "check", ...STEPS]);
    });

  test("a refused database runs none of the seven steps", async function test_boot_order_refused() {
    for (const bundled of [true, false]) {
      const { s, ran } = steps(false, { bundled });
      expect(await openData(s)).toBe(false);
      expect(ran).toEqual(bundled ? ["startPostgres", "check"] : ["check"]);
    }
  });

  test("a restart checks again before migrating and starting the API", async function test_restart_order() {
    const { s, ran } = steps(true);
    expect(await reopenData(s)).toBe(true);
    expect(ran).toEqual(["check", "runMigrations", "startApi"]);
    const refused = steps(false);
    expect(await reopenData(refused.s)).toBe(false);
    expect(refused.ran).toEqual(["check"]);
  });
});

describe("app-main startup", () => {
  // Starts the real app-main.js as a packaged app on an external database, with the
  // compatibility check answering `result`. Records every child process; the first
  // server fails to start, which ends the run.
  function snapshot(dataDir) {
    const files = {};
    for (const rel of fs.readdirSync(dataDir, { recursive: true }).sort()) {
      const full = path.join(dataDir, rel);
      if (fs.statSync(full).isFile()) files[rel] = fs.readFileSync(full, "utf8");
    }
    return files;
  }

  const EXTERNAL = {
    db_mode: "external", external_db_url: "postgresql+asyncpg://u:p@db.example/celerp",
    feature_flags: { external_db: true },
  };

  // The app window: records the pages it loads, the refusal screen it is sent and
  // when it shows. The loading page's status lines are not recorded. Every page it
  // loads finishes loading at once. `page.foreign` stands for a page that is not
  // loading.html (a dropped file): it has no showRefusal.
  function recordingWindow(events, page) {
    return function BrowserWindow() {
      const ignore = () => {};
      const loaded = [];
      const finish = () => { page.foreign = false; [...loaded].forEach((fn) => fn()); };
      const win = {
        loadFile: async (file) => { events.push(`loadFile:${path.basename(file)}`); finish(); },
        loadURL: (url) => events.push(`loadURL:${url}`),
        show: () => events.push("show"),
        isDestroyed: () => false,
        once: ignore, on: ignore,
        webContents: {
          on: (event, fn) => { if (event === "did-finish-load") loaded.push(fn); },
          once: (event, fn) => {
            if (event !== "did-finish-load") return;
            const once = () => { loaded.splice(loaded.indexOf(once), 1); fn(); };
            loaded.push(once);
          },
          send: ignore, setWindowOpenHandler: ignore, getURL: () => "",
          executeJavaScript: async (js) => {
            if (js.includes("setLoadingStatus")) return undefined;
            if (page.foreign) return false;
            events.push(`js:${js}`);
            return true;
          },
        },
        // The page finishes loading again, as on View > Reload.
        reload: () => [...loaded].forEach((fn) => fn()),
      };
      page.win = win;
      return win;
    };
  }

  async function startPackaged(result, marker = "2.6.0", { config = EXTERNAL, preflight, argv } = {}) {
    const userData = fs.mkdtempSync(path.join(os.tmpdir(), "celerp-compat-"));
    const dataDir = path.join(userData, "celerp-data");
    const moduleDir = path.join(dataDir, "modules");
    fs.mkdirSync(path.join(moduleDir, "celerp-inventory"), { recursive: true });
    fs.writeFileSync(path.join(moduleDir, "celerp-inventory", "__init__.py"), "# newer copy's module\n");
    if (marker) fs.writeFileSync(path.join(moduleDir, ".default-modules-version"), marker);
    fs.mkdirSync(path.join(dataDir, "attachments"));
    fs.writeFileSync(path.join(dataDir, "attachments", "invoice-0001.pdf"), "business document\n");
    fs.writeFileSync(path.join(dataDir, "celerp-config.json"), JSON.stringify(config));
    const resources = fs.mkdtempSync(path.join(os.tmpdir(), "celerp-res-"));
    const bundled = path.join(resources, "app", "default_modules", "celerp-inventory");
    fs.mkdirSync(bundled, { recursive: true });
    fs.writeFileSync(path.join(bundled, "__init__.py"), "# this copy's module\n");
    fs.cpSync(path.join(__dirname, "..", "..", "ui", "locales"), path.join(resources, "app", "ui", "locales"),
              { recursive: true });
    const before = snapshot(dataDir);

    const events = [];
    const name = (bin, args) => args.slice(0, 3).map((a) => path.basename(a)).join(" ");
    let started;
    const page = {};
    const loaded = loadAppMain({
      app: {
        isPackaged: true,
        getPath: () => userData,
        getVersion: () => "2.5.3",
        getLocale: () => "en-US",
        whenReady: () => ({ then: (fn) => { started = Promise.resolve().then(fn); } }),
        exit: (code) => events.push(`exit:${code}`),
        quit: () => events.push("quit"),
        relaunch: () => events.push("relaunch"),
      },
      dialog: {
        showMessageBoxSync: (opts) => { events.push(`messageSync:${opts.message}`); return 1; },
        // With or without the window it belongs to.
        showMessageBox: async (...args) => { events.push(`message:${args.at(-1).message}`); return { response: 1 }; },
        showErrorBox: (title) => events.push(`error:${title}`),
      },
      shell: { openExternal: (url) => events.push(`open:${url}`) },
      BrowserWindow: recordingWindow(events, page),
      extraFakes: {
        child_process: {
          spawnSync: (bin, args) => { events.push(`spawnSync:${name(bin, args)}`); return result; },
          execFileSync: (bin, args, opts) => {
            events.push(`execFileSync:${name(bin, args)}`);
            if (args[1] === "celerp.entitlement_preflight" && preflight) preflight(opts.env.CELERP_DATA_DIR);
            return "";
          },
          spawn: (bin, args) => {
            events.push(`spawn:${name(bin, args)}`);
            const server = Object.assign(new EventEmitter(), { stdout: new EventEmitter(), stderr: new EventEmitter() });
            setImmediate(() => server.emit("error", new Error("test boundary")));
            return server;
          },
        },
      },
      resourcesPath: resources,
      ...(argv ? { argv } : {}),
    });
    loaded.updater.checkForUpdates = async () => { events.push("checkForUpdates"); return { updateInfo: {} }; };
    await started;
    return { events, before, after: snapshot(dataDir), listeners: loaded.listeners, handlers: loaded.handlers,
             updater: loaded.updater, loaded, page };
  }

  // The refusal screen, as the window receives it, after the cold start's loading page.
  const REFUSAL_SCREEN = [
    "loadFile:loading.html",
    expect.stringMatching(/^js:.*window\.showRefusal\(\{"message":"Your data was last opened with Celerp 2\.6\.0, which is newer than this copy \(2\.5\.3\)\."/),
    "show",
  ];

  // The database decides, whatever the modules folder says: it may have no marker
  // (a reinstall) or this copy's own (a module folder rebuilt by this copy).
  test.each([["2.6.0"], ["2.5.3"], [null]])(
    "an older copy stops before touching data a newer one opened (modules marker %s)",
    async function test_app_main_refuses_before_any_step(marker) {
      const { events, before, after } = await startPackaged(NEWER, marker);
      expect(events).toEqual(["loadFile:loading.html", "spawnSync:-m celerp compatibility", ...REFUSAL_SCREEN]);
      expect(after).toEqual(before);
    });


  test("the refused copy stays open with its updater ready, checks nothing by itself, and Quit quits",
    async function test_app_main_refusal_offers_update() {
      const { events, listeners, updater } = await startPackaged(NEWER);
      expect(events).not.toContain("exit:0");
      expect(events).not.toContain("quit");
      expect(events).not.toContain("checkForUpdates");
      expect(updater.autoDownload).toBe(true);
      expect(updater.autoRunAppAfterInstall).toBe(true);
      expect(updater.listenerCount("error")).toBeGreaterThan(0);
      listeners["quit-app"]();
      expect(events.at(-1)).toBe("quit");
    });

  test("a reload, or a page that replaced the screen, ends on the refusal screen with the updater's state",
    async function test_app_main_refusal_survives_reload() {
      const { events, page, updater, handlers } = await startPackaged(NEWER);
      events.length = 0;
      page.win.reload();
      await new Promise(setImmediate);
      expect(events).toEqual([REFUSAL_SCREEN[1]]);
      // A dropped file replaced loading.html: the window goes back to it and draws the screen.
      events.length = 0;
      page.foreign = true;
      page.win.reload();
      await new Promise(setImmediate);
      expect(events).toEqual(["loadFile:loading.html", REFUSAL_SCREEN[1]]);
      // The page reads where the update stands from the main process.
      updater.emit("update-available", { version: "2.6.0" });
      expect(handlers["get-update-state"]()).toMatchObject({ status: "downloading", version: "2.6.0" });
    });

  test("File menu doors land on the refusal screen: Import Module stays, Restart Celerp relaunches",
    async function test_app_main_refusal_menu() {
      const { events, loaded } = await startPackaged(NEWER);
      events.length = 0;
      loaded.menuItem("Import Module…").click();
      expect(events).toEqual([]);
      loaded.menuItem("Restart Celerp").click();
      expect(events).toEqual(["relaunch", "quit"]);
    });

  test("a setup that stopped is told when Celerp reopens on the refusal screen, once",
    async function test_app_main_refusal_tells_stopped_update() {
      const { events, handlers, updater, loaded } = await startPackaged(NEWER, "2.6.0", {
        argv: [process.execPath, "--updated", "--update-install-failed=2.6.0"] });
      await new Promise(setImmediate);
      const stopped = "message:The update stopped before it finished, so Celerp is still on the version you had.";
      expect(events.slice(2)).toEqual([...REFUSAL_SCREEN, stopped]);
      expect(handlers["get-update-state"]()).toMatchObject({ status: "error", failure: "stopped" });
      // The updater is set up once, whichever path asks first: no second dialog, no second listener.
      const errorListeners = updater.listenerCount("error");
      loaded.setupAutoUpdater();
      await new Promise(setImmediate);
      expect(events.filter((e) => e === stopped)).toHaveLength(1);
      expect(updater.listenerCount("error")).toBe(errorListeners);
    });

  test("Restart to install is told to the next page that loads", async function test_app_main_refusal_installing() {
    const { listeners, handlers, updater } = await startPackaged(NEWER);
    updater.quitAndInstall = () => {};
    updater.emit("update-downloaded", { version: "2.6.0" });
    await listeners["install-update"]();
    expect(handlers["get-update-state"]()).toMatchObject({ status: "downloaded", installing: true });
  });

  test("an unreadable version record gets its own dialog, then quits", async function test_app_main_invalid_record() {
    const { events, before, after } = await startPackaged(INVALID);
    expect(events).toEqual([
      "loadFile:loading.html",
      "spawnSync:-m celerp compatibility",
      "message:Celerp cannot read which version last opened your data, so it has not opened it. " +
        "Your data has not been changed.",
      "exit:0",
    ]);
    expect(after).toEqual(before);
  });

  // The entitlement preflight runs before the check and may refresh the config, but
  // a refused database still has its modules and every data file left as they were.
  test("a refused database is left unchanged when the preflight refreshed the subscription",
    async function test_app_main_preflight_then_refused() {
      const lapsed = { ...EXTERNAL, feature_flags: { external_db: false } };
      const { events, before, after } = await startPackaged(NEWER, "2.6.0", {
        config: lapsed,
        // As celerp.entitlement_preflight does on a renewal: the refreshed flags land
        // in the config Electron reads.
        preflight: (dataDir) => fs.writeFileSync(path.join(dataDir, "celerp-config.json"), JSON.stringify(EXTERNAL)),
      });
      expect(events).toEqual([
        "execFileSync:-m celerp.entitlement_preflight",
        "loadFile:loading.html",
        "spawnSync:-m celerp compatibility",
        ...REFUSAL_SCREEN,
      ]);
      expect(JSON.parse(after["celerp-config.json"])).toEqual(EXTERNAL);
      // Configuration only: the refreshed config, and the password a bundled database
      // would use, minted while choosing a database when no bundled one exists yet.
      const settings = (files) => {
        const { ["celerp-config.json"]: _c, ["pg-password"]: _p, ...data } = files;
        return data;
      };
      const dataAfter = settings(after);
      expect(dataAfter).toEqual(settings(before));
      expect(dataAfter["attachments/invoice-0001.pdf"]).toBe("business document\n");
    });

  test("compatible data opens as before, after the check", async function test_app_main_compatible_starts() {
    const { events, before, after } = await startPackaged(COMPATIBLE);
    expect(events.filter((e) => /^(spawn|execFileSync|message)/.test(e))).toEqual([
      "spawnSync:-m celerp compatibility",
      "execFileSync:-m celerp migrate",
      process.platform === "win32" ? "spawn:-m uvicorn celerp.main:app" : "spawn:-m celerp.desktop_child uvicorn",
    ]);
    expect(after["modules/celerp-inventory/__init__.py"]).not.toEqual(before["modules/celerp-inventory/__init__.py"]);
  });
});
