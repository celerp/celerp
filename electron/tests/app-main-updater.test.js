// Copyright (c) 2026 Noah Severs
// SPDX-License-Identifier: BUSL-1.1
//
// Exercises the updater wiring app-main.js does at load time, not just
// update-state.js.

"use strict";

const fs = require("fs");
const os = require("os");
const path = require("path");
const { loadAppMain } = require("./app-main-loader.lib");

test("app-main serves get-update-state from the updater it tracks",
  function test_app_main_serves_the_tracked_updater_state() {
    const { handlers, updater } = loadAppMain();
    expect(typeof handlers["get-update-state"]).toBe("function");
    updater.emit("update-available", { version: "2.0.1" });
    updater.emit("update-downloaded", { version: "2.0.1" });
    const state = handlers["get-update-state"]();
    expect([state.status, state.version]).toEqual(["downloaded", "2.0.1"]);
    expect(state.log).toEqual(["Found v2.0.1, downloading...", "v2.0.1 ready. Restart to install."]);
  });

test("app-main's check-for-updates dismisses a failed download",
  function test_app_main_check_dismisses_a_failed_download() {
    const { handlers, updater } = loadAppMain();
    updater.emit("update-available", { version: "2.0.1" });
    updater.emit("error", new Error("sha512 checksum mismatch"));
    expect(handlers["get-update-state"]().downloadFailed).toBe(true);
    handlers["check-for-updates"]();
    expect(handlers["get-update-state"]().downloadFailed).toBe(false);
  });

test("app-main sends the updater's changes to its window as they happen",
  function test_app_main_forwards_updater_events_to_the_window() {
    const { updater, setMainWindow } = loadAppMain();
    const sent = [];
    setMainWindow({ webContents: { send: (channel, state) => sent.push([channel, state.status]) } });
    updater.emit("update-available", { version: "2.0.1" });
    updater.emit("update-downloaded", { version: "2.0.1" });
    expect(sent).toEqual([["update-available", "downloading"], ["update-downloaded", "downloaded"]]);
  });

// The installed (patched) electron-updater's own install step, on a fake
// updater whose installer launches fail `launchDelay` ms after they start, as
// spawn errors do on Windows (the first launch, then the elevated retry), or
// that start (`launches`) after that delay.
jest.mock("electron", () => ({ autoUpdater: { emit: () => {} } }));
const { BaseUpdater } = require("electron-updater/out/BaseUpdater");
const { NsisUpdater } = require("electron-updater/out/NsisUpdater");

function useInstallStep(updater, app, launchDelay, launches = false) {
  process.resourcesPath = os.tmpdir();   // where the elevated retry finds elevate.exe
  Object.assign(updater, {
    app,
    _logger: { info: () => {}, warn: () => {}, error: () => {} },
    quitAndInstallCalled: false,
    downloadedUpdateHelper: { file: "C:\\pending\\Celerp-Setup.exe", packageFile: null,
                              downloadedFileInfo: { isAdminRightsRequired: false } },
    quitAndInstall: BaseUpdater.prototype.quitAndInstall,
    install: BaseUpdater.prototype.install,
    doInstall: NsisUpdater.prototype.doInstall,
    dispatchError: (e) => updater.emit("error", e),
    spawnLog: () => new Promise((resolve, reject) => setTimeout(() => (launches ? resolve(true)
      : reject(Object.assign(new Error("spawn UNKNOWN"), { code: "UNKNOWN" }))), launchDelay)),
  });
  Object.defineProperty(updater, "installerPath", { get: () => updater.downloadedUpdateHelper.file });
}

// The packaged app's resources folder: resources/app is the repository, as the
// installer lays it out, so the main process reads the UI's own translations.
function resourcesFolder() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "celerp-resources-"));
  fs.symlinkSync(path.resolve(__dirname, "..", ".."), path.join(dir, "app"));
  return dir;
}

// A packaged app whose window has loaded. quit() runs before-quit and then
// will-quit as Electron does, and the app has quit only if no handler held it.
// Dialogs wait until the test answers them with answer(response).
function packagedApp({ argv = process.argv } = {}) {
  const shown = [];
  const answers = [];
  const opened = [];
  const relaunched = [];
  const quits = [];
  const events = {};
  const app = {
    isPackaged: true,
    getLocale: () => "en-US",
    on: (event, fn) => { (events[event] ||= []).push(fn); },
    relaunch: (options) => relaunched.push(options),
    exit: () => {},
    quit: () => {
      let held = false;
      const event = { preventDefault: () => { held = true; } };
      for (const name of ["before-quit", "will-quit"]) {
        for (const fn of events[name] || []) fn(event);
        if (held) return;
      }
      quits.push(true);
    },
  };
  const loaded = loadAppMain({
    app,
    argv,
    dialog: { showMessageBox: (...args) => {
      shown.push(args[args.length - 1]);
      return new Promise((resolve) => answers.push((response) => resolve({ response })));
    } },
    shell: { openExternal: (url) => { opened.push(url); return Promise.resolve(); } },
    resourcesPath: resourcesFolder(),
  });
  const checks = [];
  loaded.updater.checkForUpdates = () => { checks.push(true); return Promise.resolve(); };
  let loadedHandler = null;
  loaded.setMainWindow({ isDestroyed: () => false,
                         webContents: { once: (e, fn) => { loadedHandler = fn; }, send: () => {} } });
  loaded.setupAutoUpdater();
  if (loadedHandler) loadedHandler();
  const answer = async (response) => { answers.shift()(response); await settle(); };
  return { ...loaded, app, shown, answer, opened, relaunched, quits, checks };
}

const settle = () => new Promise((resolve) => setImmediate(resolve));
const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const TAG = "https://github.com/celerp/celerp/releases/tag/v2.6.1";

test("an installer that fails to start after quitting has begun keeps Celerp open, shows the failure and offers the installer",
  async function test_delayed_install_launch_failure_keeps_the_app_open() {
    const t = packagedApp();
    useInstallStep(t.updater, t.app, 20);
    t.updater.emit("update-available", { version: "2.6.1" });
    t.updater.emit("update-downloaded", { version: "2.6.1" });
    await t.listeners["install-update"]();
    await settle();
    expect(t.quits).toEqual([]);            // the installer has not started yet
    await wait(60);                         // the launch and the elevated retry both fail
    expect(t.quits).toEqual([]);
    const state = t.handlers["get-update-state"]();
    expect([state.status, state.failure, state.releaseUrl]).toEqual(["error", "install", TAG]);
    expect(t.shown.length).toBe(1);
    expect(t.shown[0].message).toBe("Celerp could not start the installer.");
    expect(t.shown[0].buttons).toEqual(["Download the installer", "Close"]);
    expect(t.shown[0].detail).toMatch(/spawn UNKNOWN/);
    await t.answer(0);
    expect(t.opened).toEqual([TAG]);
    // The servers stopped for the installer, so Celerp restarts and shows the failed install.
    expect(t.relaunched.length).toBe(1);
    expect(t.relaunched[0].args).toContain("--update-install-failed=2.6.1");
    expect(t.quits).toEqual([true]);
  });

test("Celerp quits for the installer only once the installer has started",
  async function test_quit_waits_for_the_installer_to_start() {
    const t = packagedApp();
    useInstallStep(t.updater, t.app, 20, true);
    t.updater.emit("update-available", { version: "2.6.1" });
    t.updater.emit("update-downloaded", { version: "2.6.1" });
    await t.listeners["install-update"]();
    await settle();
    expect(t.quits).toEqual([]);
    await wait(40);
    expect(t.quits).toEqual([true]);
    expect(t.shown).toEqual([]);
  });

test("Celerp restarted after an installer failed to start shows the failed install and does not check by itself",
  function test_restart_after_failed_install_shows_it() {
    const t = packagedApp({ argv: [process.execPath, "--update-install-failed=2.6.1"] });
    const state = t.handlers["get-update-state"]();
    expect([state.status, state.failure, state.version, state.releaseUrl]).toEqual(["error", "install", "2.6.1", TAG]);
    expect(t.checks).toEqual([]);
    expect(t.shown).toEqual([]);
  });

test("a download that fails its security check offers Check again and support, never the installer",
  async function test_verification_failure_fails_closed() {
    const t = packagedApp();
    t.checks.length = 0;
    t.handlers["check-for-updates"]();
    t.updater.emit("update-available", { version: "2.6.1" });
    t.updater.emit("error", new Error("sha512 checksum mismatch, expected abc, got def"));
    await settle();
    expect(t.shown.length).toBe(1);
    const [dialog] = t.shown;
    expect(dialog.buttons).toEqual(["Check again", "Close"]);
    expect(dialog.message).toMatch(/security check/);
    expect(dialog.detail).toMatch(/GitHub Issues/);
    expect(`${dialog.message} ${dialog.detail}`).not.toMatch(/installer|—/i);
    expect(t.handlers["get-update-state"]().releaseUrl).toBe("");
    await t.answer(0);
    expect(t.opened).toEqual([]);
    expect(t.checks.length).toBe(2);   // the user's check, then Check again
  });

test("failures of the app's own checks and downloads stay on the update card; a check the user asked for gets a dialog",
  async function test_background_failures_never_open_a_dialog() {
    const t = packagedApp();
    t.updater.emit("error", new Error("getaddrinfo ENOTFOUND github.com"));
    t.updater.emit("update-available", { version: "2.6.1" });
    t.updater.emit("error", new Error("EPERM: operation not permitted, rename"));
    await settle();
    expect(t.shown).toEqual([]);
    expect(t.handlers["get-update-state"]().failure).toBe("save");
    t.handlers["check-for-updates"]();
    t.updater.emit("update-available", { version: "2.6.1" });
    t.updater.emit("error", new Error("EPERM: operation not permitted, rename"));
    await settle();
    expect(t.shown.length).toBe(1);
    expect(t.shown[0].buttons).toEqual(["Download the installer", "Close"]);
    expect(t.shown[0].message).toMatch(/would not let Celerp save/);
    await t.answer(1);
    expect(t.opened).toEqual([]);
  });
