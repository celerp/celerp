// Copyright (c) 2026 Noah Severs
// SPDX-License-Identifier: BUSL-1.1
//
// A Windows package carries the Visual C++ runtime next to every bundled
// PostgreSQL binary, and the build stops when the runtime is not there to copy.
// It also leaves out every file win-prune.json lists, after checking each one
// belongs to the owner the list names.
"use strict";

const fs = require("fs");
const os = require("os");
const path = require("path");
const afterPack = require("../after-pack").default;
const PRUNE = require("../win-prune.json");

const RUNTIME = ["vcruntime140.dll", "vcruntime140_1.dll", "msvcp140.dll"];
const KEPT = [
  `${PRUNE.postgres}/bin/postgres.exe`,
  `${PRUNE.postgres}/lib/libpq.lib`,
  `${PRUNE.sitePackages}/numpy/linalg/linalg.py`,
  `${PRUNE.sitePackages}/pip/__init__.py`,
  `${PRUNE.python}/Lib/os.py`,
  "resources/app/celerp/main.py",
];

// A packaged Windows tree holding every win-prune.json entry, each owned as
// the entry says, plus the files around them that must survive.
function windowsPackage() {
  const appOutDir = fs.mkdtempSync(path.join(os.tmpdir(), "celerp-winpack-"));
  const at = (rel) => path.join(appOutDir, rel);
  const put = (rel, text = "") => {
    fs.mkdirSync(path.dirname(at(rel)), { recursive: true });
    fs.writeFileSync(at(rel), text);
  };
  const records = {};
  for (const e of PRUNE.entries) {
    if (e.match) continue;
    const file = path.extname(e.path) ? e.path : `${e.path}/x.py`;
    put(file, e.sameAs ? "licence text" : "");
    if (e.sameAs) put(e.sameAs, "licence text");
    if (e.path.startsWith(`${PRUNE.sitePackages}/`)) {
      (records[e.owner] ||= []).push(path.posix.relative(PRUNE.sitePackages, file));
    }
  }
  for (const [owner, files] of Object.entries(records)) {
    put(`${PRUNE.sitePackages}/${owner}-1.0.dist-info/RECORD`, files.map((f) => `${f},,`).join("\n"));
  }
  const serverBin = at(`${PRUNE.postgres}/bin`);
  const toolsBin = at("resources/pg-win/bin");
  for (const f of KEPT) put(f);
  put(`${PRUNE.postgres}/bin/wxbase329u_vc_x64_custom.dll`);
  put(`${PRUNE.postgres}/bin/libcurl.dll`);
  fs.mkdirSync(toolsBin, { recursive: true });
  return { appOutDir, at, put, serverBin, toolsBin };
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

  test("removes every win-prune.json entry and nothing around them", async () => {
    const pkg = windowsPackage();
    process.env.CELERP_VC_RUNTIME_DIR = runtimeDir();
    await run(pkg.appOutDir);
    expect(PRUNE.entries.filter((e) => !e.match && fs.existsSync(pkg.at(e.path))).map((e) => e.path)).toEqual([]);
    expect(fs.readdirSync(pkg.serverBin).sort()).toEqual(["postgres.exe", ...RUNTIME].sort());
    for (const f of KEPT.filter((f) => !f.includes("/bin/"))) expect(fs.existsSync(pkg.at(f))).toBe(true);
    for (const e of PRUNE.entries.filter((e) => e.sameAs)) expect(fs.existsSync(pkg.at(e.sameAs))).toBe(true);
  });

  test("stops the build when a listed entry is not in the package", async () => {
    const pkg = windowsPackage();
    fs.rmSync(pkg.at("resources/python-x64/python/tcl"), { recursive: true });
    process.env.CELERP_VC_RUNTIME_DIR = runtimeDir();
    await expect(run(pkg.appOutDir)).rejects.toThrow(/python\/tcl is listed in win-prune.json but not in the package/);
  });

  test("stops the build when a package folder holds a file its package did not install", async () => {
    const pkg = windowsPackage();
    pkg.put(`${PRUNE.sitePackages}/numpy/tests/added_later.py`);
    process.env.CELERP_VC_RUNTIME_DIR = runtimeDir();
    await expect(run(pkg.appOutDir)).rejects.toThrow(/added_later\.py under .*numpy\/tests is not installed by numpy/);
    expect(fs.existsSync(pkg.at(`${PRUNE.sitePackages}/numpy/tests/added_later.py`))).toBe(true);
  });

  test("stops the build when a duplicate licence differs from the copy that is kept", async () => {
    const pkg = windowsPackage();
    const dup = PRUNE.entries.find((e) => e.sameAs);
    pkg.put(dup.sameAs, "a different licence text");
    process.env.CELERP_VC_RUNTIME_DIR = runtimeDir();
    await expect(run(pkg.appOutDir)).rejects.toThrow(/is not identical to/);
    expect(fs.existsSync(pkg.at(dup.path))).toBe(true);
  });

  test("stops the build when the runtime folder is not set", async () => {
    delete process.env.CELERP_VC_RUNTIME_DIR;
    await expect(run(windowsPackage().appOutDir)).rejects.toThrow(/CELERP_VC_RUNTIME_DIR/);
  });

  test("stops the build when a runtime DLL is missing", async () => {
    process.env.CELERP_VC_RUNTIME_DIR = runtimeDir(["vcruntime140.dll", "vcruntime140_1.dll"]);
    await expect(run(windowsPackage().appOutDir)).rejects.toThrow(/msvcp140\.dll/);
  });

  test("stops the build when the server bin folder is missing", async () => {
    const pkg = windowsPackage();
    fs.rmSync(pkg.serverBin, { recursive: true });
    process.env.CELERP_VC_RUNTIME_DIR = runtimeDir();
    await expect(run(pkg.appOutDir)).rejects.toThrow(/native/);
  });
});

test("other platforms need no runtime folder and keep every win-prune.json path", async () => {
  const { appOutDir, at } = windowsPackage();
  delete process.env.CELERP_VC_RUNTIME_DIR;
  await expect(afterPack({ electronPlatformName: "linux", appOutDir })).resolves.toBeUndefined();
  expect(PRUNE.entries.filter((e) => !fs.existsSync(at(e.path))).map((e) => e.path)).toEqual([]);
});
