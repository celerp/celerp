// Copyright (c) 2026 Noah Severs
// SPDX-License-Identifier: BUSL-1.1
"use strict";

const fs = require("fs");
const os = require("os");
const path = require("path");
const { EventEmitter } = require("events");
const { loadAppMain } = require("./app-main-loader.lib");
const {
  DOWNLOAD_URL, REFUSED_EXIT, compatibilityArgs, readDecision, refusalDialog, mayOpenData,
} = require("../compatibility");
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

describe("refusal dialog", () => {
  test("newer data names both versions", function test_dialog_newer() {
    const d = refusalDialog(readDecision(NEWER));
    expect(d.message).toBe("Your data was last opened with Celerp 2.6.0, which is newer than this copy (2.5.3). " +
                           "Download the latest version to continue.");
    expect(d.detail).toBe("Your data has not been changed.");
    expect(d.buttons).toEqual(["Download the latest version", "Quit"]);
  });

  test("unknown or unreadable data cannot safely be opened", function test_dialog_unknown() {
    for (const status of ["unknown_schema", "invalid_version_record"]) {
      expect(refusalDialog({ status, running: "2.5.3" }).message).toBe(
        "Your data cannot safely be opened by this copy of Celerp (2.5.3). Download the latest version to continue.");
    }
  });
});

describe("mayOpenData", () => {
  function run(result, choice) {
    const shown = [], opened = [];
    const ok = mayOpenData({
      check: () => result,
      dialog: { showMessageBoxSync: (o) => { shown.push(o); return choice; } },
      shell: { openExternal: (u) => opened.push(u) },
    });
    return { ok, shown, opened };
  }

  test("compatible data opens without a dialog", function test_may_open_compatible() {
    expect(run(COMPATIBLE, 0)).toEqual({ ok: true, shown: [], opened: [] });
  });

  test("Download opens the download page; Quit opens nothing", function test_may_open_refused() {
    expect(run(NEWER, 0)).toMatchObject({ ok: false, opened: [DOWNLOAD_URL] });
    expect(run(UNKNOWN, 1)).toMatchObject({ ok: false, opened: [] });
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
  function anything() {
    return new Proxy(function fake() {}, {
      get: (_t, key) => (key === "then" ? undefined : anything()),
      apply: () => anything(),
      construct: () => anything(),
    });
  }

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

  async function startPackaged(result, marker = "2.6.0", { config = EXTERNAL, preflight } = {}) {
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
    const before = snapshot(dataDir);

    const events = [];
    const name = (bin, args) => args.slice(0, 3).map((a) => path.basename(a)).join(" ");
    let started;
    loadAppMain({
      app: {
        isPackaged: true,
        getPath: () => userData,
        getVersion: () => "2.5.3",
        whenReady: () => ({ then: (fn) => { started = Promise.resolve().then(fn); } }),
        exit: (code) => events.push(`exit:${code}`),
        quit: () => events.push("quit"),
      },
      dialog: {
        showMessageBoxSync: (opts) => { events.push(`message:${opts.message}`); return 1; },
        showErrorBox: (title) => events.push(`error:${title}`),
      },
      shell: { openExternal: (url) => events.push(`open:${url}`) },
      BrowserWindow: anything(),
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
    });
    await started;
    return { events, before, after: snapshot(dataDir) };
  }

  // The database decides, whatever the modules folder says: it may have no marker
  // (a reinstall) or this copy's own (a module folder rebuilt by this copy).
  test.each([["2.6.0"], ["2.5.3"], [null]])(
    "an older copy stops before touching data a newer one opened (modules marker %s)",
    async function test_app_main_refuses_before_any_step(marker) {
      const { events, before, after } = await startPackaged(NEWER, marker);
      expect(events).toEqual([
        "spawnSync:-m celerp compatibility",
        "message:Your data was last opened with Celerp 2.6.0, which is newer than this copy (2.5.3). " +
          "Download the latest version to continue.",
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
        "spawnSync:-m celerp compatibility",
        "message:Your data was last opened with Celerp 2.6.0, which is newer than this copy (2.5.3). " +
          "Download the latest version to continue.",
        "exit:0",
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
      "spawn:-m uvicorn celerp.main:app",
    ]);
    expect(after["modules/celerp-inventory/__init__.py"]).not.toEqual(before["modules/celerp-inventory/__init__.py"]);
  });
});
