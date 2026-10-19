// Copyright (c) 2026 Noah Severs
// SPDX-License-Identifier: BUSL-1.1
//
// Exercises the updater wiring app-main.js does at load time, not just
// update-state.js.

"use strict";

const os = require("os");
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

// A packaged app whose window has loaded, with a dialog that answers `choice`.
function packagedApp(choice) {
  const shown = [];
  const opened = [];
  const relaunched = [];
  const loaded = loadAppMain({
    app: { isPackaged: true, relaunch: () => relaunched.push(true), exit: () => {} },
    dialog: { showMessageBox: (...args) => { shown.push(args[args.length - 1]); return Promise.resolve({ response: choice }); } },
    shell: { openExternal: (url) => { opened.push(url); return Promise.resolve(); } },
    resourcesPath: os.tmpdir(),
  });
  loaded.updater.checkForUpdates = () => Promise.resolve();
  loaded.updater.quitAndInstall = () => loaded.updater.emit("error", new Error("spawn UNKNOWN"));
  loaded.setMainWindow({ isDestroyed: () => false, webContents: { once: () => {}, send: () => {} } });
  loaded.setupAutoUpdater();
  return { ...loaded, shown, opened, relaunched };
}

const settle = () => new Promise((resolve) => setImmediate(resolve));

test("a failed download is shown in a dialog whose Download the installer opens that version's release page",
  async function test_failed_download_offers_the_installer() {
    const { updater, shown, opened } = packagedApp(0);
    updater.emit("update-available", { version: "2.6.1" });
    updater.emit("error", new Error("EPERM: operation not permitted, rename"));
    await settle();
    expect(shown.length).toBe(1);
    expect(shown[0].buttons[0]).toBe("Download the installer");
    expect(shown[0].message).toMatch(/would not let Celerp save/);
    expect(opened).toEqual(["https://github.com/celerp/celerp/releases/tag/v2.6.1"]);
  });

test("closing the failure dialog opens nothing, and a failed check stays on the update card",
  async function test_close_opens_nothing_and_check_failures_do_not_pop_up() {
    const { updater, shown, opened } = packagedApp(1);
    updater.emit("error", new Error("getaddrinfo ENOTFOUND github.com"));
    await settle();
    expect(shown.length).toBe(0);
    updater.emit("update-available", { version: "2.6.1" });
    updater.emit("error", new Error("sha512 checksum mismatch"));
    await settle();
    expect(shown.length).toBe(1);
    expect(opened).toEqual([]);
  });

test("an installer that cannot be started is shown, offers the installer, and Celerp restarts after",
  async function test_install_launch_failure_offers_the_installer() {
    const { updater, listeners, shown, opened, relaunched } = packagedApp(0);
    updater.emit("update-available", { version: "2.6.1" });
    updater.emit("update-downloaded", { version: "2.6.1" });
    await listeners["install-update"]();
    await settle();
    expect(shown.length).toBe(1);
    expect(shown[0].message).toMatch(/could not start the installer/);
    expect(shown[0].detail).toMatch(/restart/i);
    expect(opened).toEqual(["https://github.com/celerp/celerp/releases/tag/v2.6.1"]);
    expect(relaunched.length).toBe(1);
  });
