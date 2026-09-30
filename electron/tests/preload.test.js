// Copyright (c) 2026 Noah Severs
// SPDX-License-Identifier: BUSL-1.1
"use strict";

// Load preload.js against a stand-in for Electron's bridge and IPC, then drive
// the API the page sees.
const exposed = {};
const invoked = [];
const listeners = {};

jest.mock("electron", () => ({
  contextBridge: { exposeInMainWorld: (name, api) => { exposed[name] = api; } },
  ipcRenderer: {
    invoke: (channel, ...args) => { invoked.push(channel); return Promise.resolve({ status: "downloaded", version: "2.0.1", percent: 100, message: "" }); },
    on: (channel, fn) => { listeners[channel] = fn; },
    send: () => {},
  },
}), { virtual: true });

require("../preload");

test("getUpdateState reads the main process state and offers no way to set it",
  async function test_preload_get_update_state_reads_main() {
    const api = exposed.celerp;
    await expect(api.getUpdateState()).resolves.toEqual(
      { status: "downloaded", version: "2.0.1", percent: 100, message: "" });
    expect(invoked).toEqual(["get-update-state"]);
    expect(Object.keys(api).filter((k) => /^set.*update/i.test(k))).toEqual([]);

    // Live update events hand the page the same state object.
    const seen = [];
    api.onUpdateNotAvailable((s) => seen.push(s));
    listeners["update-not-available"]({}, { status: "idle", version: "", percent: 0, message: "" });
    expect(seen).toEqual([{ status: "idle", version: "", percent: 0, message: "" }]);
  });
