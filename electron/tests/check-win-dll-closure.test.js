// Copyright (c) 2026 Noah Severs
// SPDX-License-Identifier: BUSL-1.1
//
// The Windows DLL closure check reads each binary's import table and reports any
// DLL that is neither beside it nor part of Windows.
"use strict";

const fs = require("fs");
const os = require("os");
const path = require("path");
const { execFileSync } = require("child_process");
const { peImports, unresolvedImports } = require("../scripts/check-win-dll-closure");

const SCRIPT = path.join(__dirname, "..", "scripts", "check-win-dll-closure.js");

// A minimal 64-bit PE image with one section holding its import tables.
function pe({ imports = [], delayImports = [] } = {}) {
  const SECTION_VA = 0x1000;
  const SECTION_RAW = 0x200;
  const body = Buffer.alloc(0x400);
  let cursor = 0;
  const strings = [];
  const table = (names, entrySize, nameAt) => {
    if (names.length === 0) return 0;
    const start = cursor;
    cursor += (names.length + 1) * entrySize;
    names.forEach((n, i) => strings.push([start + i * entrySize + nameAt, n]));
    return SECTION_VA + start;
  };
  const importRva = table(imports, 20, 12);
  const delayRva = table(delayImports, 32, 4);
  for (const [slot, name] of strings) {
    body.writeUInt32LE(SECTION_VA + cursor, slot);
    body.write(name + "\0", cursor, "latin1");
    cursor += name.length + 1;
  }
  const head = Buffer.alloc(SECTION_RAW);
  head.write("MZ", 0, "latin1");
  head.writeUInt32LE(0x40, 0x3c);
  head.write("PE\0\0", 0x40, "latin1");
  head.writeUInt16LE(0x8664, 0x44);
  head.writeUInt16LE(1, 0x46);            // one section
  head.writeUInt16LE(240, 0x54);          // optional header size
  const optional = 0x58;
  head.writeUInt16LE(0x20b, optional);    // PE32+
  head.writeUInt32LE(16, optional + 108); // data directory count
  head.writeUInt32LE(importRva, optional + 112 + 8);
  head.writeUInt32LE(delayRva, optional + 112 + 13 * 8);
  const section = optional + 240;
  head.write(".idata", section, "latin1");
  head.writeUInt32LE(body.length, section + 8);
  head.writeUInt32LE(SECTION_VA, section + 12);
  head.writeUInt32LE(body.length, section + 16);
  head.writeUInt32LE(SECTION_RAW, section + 20);
  return Buffer.concat([head, body]);
}

function binDir(files) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "dll-closure-"));
  for (const [name, content] of Object.entries(files)) fs.writeFileSync(path.join(dir, name), content);
  return dir;
}

function run(dirs) {
  try {
    return { code: 0, out: execFileSync(process.execPath, [SCRIPT, ...dirs], { encoding: "utf8", stdio: "pipe" }) };
  } catch (e) {
    return { code: e.status, out: e.stdout + e.stderr };
  }
}

describe("peImports", () => {
  test("lists normal and delay-loaded imports", () => {
    expect(peImports(pe({ imports: ["KERNEL32.dll", "VCRUNTIME140.dll"], delayImports: ["libpq.dll"] })))
      .toEqual(["KERNEL32.dll", "VCRUNTIME140.dll", "libpq.dll"]);
  });

  test("a binary with no imports has none", () => {
    expect(peImports(pe())).toEqual([]);
  });

  test("a file that is not a PE image is an error, never an empty list", () => {
    expect(() => peImports(Buffer.from("not a binary at all, just text padding".repeat(4)))).toThrow(/not a PE file/);
  });
});

describe("unresolvedImports", () => {
  test("the Visual C++ runtime is reported when it is not shipped beside the binary", () => {
    const dir = binDir({
      "postgres.exe": pe({ imports: ["KERNEL32.dll", "api-ms-win-crt-runtime-l1-1-0.dll", "icuuc67.dll", "VCRUNTIME140.dll"] }),
      "icuuc67.dll": pe({ imports: ["MSVCP140.dll", "VCRUNTIME140.dll"] }),
    });
    expect(unresolvedImports(dir)).toEqual([
      { file: "icuuc67.dll", dll: "MSVCP140.dll" },
      { file: "icuuc67.dll", dll: "VCRUNTIME140.dll" },
      { file: "postgres.exe", dll: "VCRUNTIME140.dll" },
    ]);
  });

  test("a DLL beside the binary resolves whatever its case", () => {
    const dir = binDir({
      "postgres.exe": pe({ imports: ["VCRUNTIME140.dll", "ADVAPI32.dll"] }),
      "vcruntime140.dll": pe({ imports: ["kernel32.dll"] }),
    });
    expect(unresolvedImports(dir)).toEqual([]);
  });
});

describe("check-win-dll-closure CLI", () => {
  test("fails and names each unresolved import", () => {
    const dir = binDir({ "initdb.exe": pe({ imports: ["VCRUNTIME140.dll"] }) });
    const { code, out } = run([dir]);
    expect(code).toBe(1);
    expect(out).toContain(`${path.join(dir, "initdb.exe")} imports VCRUNTIME140.dll`);
  });

  test("passes when every folder's imports resolve", () => {
    const a = binDir({ "pg_dump.exe": pe({ imports: ["libpq.dll"] }), "libpq.dll": pe({ imports: ["WS2_32.dll"] }) });
    const b = binDir({ "postgres.exe": pe({ imports: ["Secur32.dll", "ext-ms-win-ntuser-window-l1-1-0.dll"] }) });
    expect(run([a, b])).toEqual({ code: 0, out: `OK: every import resolves in ${a}, ${b}\n` });
  });

  test("a folder's DLLs do not satisfy another folder's binaries", () => {
    const a = binDir({ "vcruntime140.dll": pe() });
    const b = binDir({ "pg_dump.exe": pe({ imports: ["VCRUNTIME140.dll"] }) });
    expect(run([a, b]).code).toBe(1);
  });

  test("no folder is a usage error", () => {
    expect(run([]).code).toBe(2);
  });
});
