// Copyright (c) 2026 Noah Severs
// SPDX-License-Identifier: BUSL-1.1
//
// A Windows package carries the Visual C++ runtime next to every bundled
// PostgreSQL binary, and the build stops when the runtime is not there to copy.
"use strict";

const fs = require("fs");
const os = require("os");
const path = require("path");
const afterPack = require("../after-pack").default;

const RUNTIME = ["vcruntime140.dll", "msvcp140.dll"];

function windowsPackage() {
  const appOutDir = fs.mkdtempSync(path.join(os.tmpdir(), "celerp-winpack-"));
  const resources = path.join(appOutDir, "resources");
  const serverBin = path.join(resources, "app.asar.unpacked", "node_modules",
    "@embedded-postgres", "windows-x64", "native", "bin");
  const toolsBin = path.join(resources, "pg-win", "bin");
  fs.mkdirSync(serverBin, { recursive: true });
  fs.mkdirSync(toolsBin, { recursive: true });
  fs.writeFileSync(path.join(serverBin, "postgres.exe"), "");
  fs.writeFileSync(path.join(serverBin, "wxbase329u_vc_x64_custom.dll"), "");
  return { appOutDir, serverBin, toolsBin };
}

function runtimeDir(files = RUNTIME) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "celerp-vcrt-"));
  for (const f of files) fs.writeFileSync(path.join(dir, f), `runtime ${f}`);
  return dir;
}

const run = (appOutDir) => afterPack({ electronPlatformName: "win32", appOutDir });

const saved = process.env.CELERP_VC_RUNTIME_DIR;
afterEach(() => {
  if (saved === undefined) delete process.env.CELERP_VC_RUNTIME_DIR;
  else process.env.CELERP_VC_RUNTIME_DIR = saved;
});

describe("afterPack on Windows", () => {
  test("copies the Visual C++ runtime into the server and the tools bin folders", async () => {
    const pkg = windowsPackage();
    process.env.CELERP_VC_RUNTIME_DIR = runtimeDir();
    await run(pkg.appOutDir);
    for (const dir of [pkg.serverBin, pkg.toolsBin]) {
      for (const f of RUNTIME) expect(fs.readFileSync(path.join(dir, f), "utf8")).toBe(`runtime ${f}`);
    }
    expect(fs.existsSync(path.join(pkg.serverBin, "wxbase329u_vc_x64_custom.dll"))).toBe(false);
  });

  test("stops the build when the runtime folder is not set", async () => {
    delete process.env.CELERP_VC_RUNTIME_DIR;
    await expect(run(windowsPackage().appOutDir)).rejects.toThrow(/CELERP_VC_RUNTIME_DIR/);
  });

  test("stops the build when a runtime DLL is missing", async () => {
    process.env.CELERP_VC_RUNTIME_DIR = runtimeDir(["vcruntime140.dll"]);
    await expect(run(windowsPackage().appOutDir)).rejects.toThrow(/msvcp140\.dll/);
  });

  test("stops the build when the server bin folder is missing", async () => {
    const pkg = windowsPackage();
    fs.rmSync(pkg.serverBin, { recursive: true });
    process.env.CELERP_VC_RUNTIME_DIR = runtimeDir();
    await expect(run(pkg.appOutDir)).rejects.toThrow(/native/);
  });
});

test("other platforms need no runtime folder", async () => {
  const appOutDir = fs.mkdtempSync(path.join(os.tmpdir(), "celerp-linuxpack-"));
  fs.mkdirSync(path.join(appOutDir, "resources"));
  delete process.env.CELERP_VC_RUNTIME_DIR;
  await expect(afterPack({ electronPlatformName: "linux", appOutDir })).resolves.toBeUndefined();
});
