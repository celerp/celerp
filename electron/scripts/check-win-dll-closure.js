// Copyright (c) 2026 Noah Severs
// SPDX-License-Identifier: BUSL-1.1
//
// Checks that every Windows binary in the given folders can load on a clean
// Windows install. Windows looks for a program's DLLs beside the program and in
// the system folders, so each DLL that an .exe or .dll in a folder imports must
// either sit in that same folder or be part of Windows itself (the list below).
// Anything else, such as the Visual C++ runtime, is missing on a fresh PC.
//
//   node check-win-dll-closure.js DIR [DIR...]
//
// Exits 1 and lists each unresolved import when any is found.
"use strict";

const fs = require("fs");
const path = require("path");

// DLLs that every supported Windows version ships in System32.
const WINDOWS_DLLS = new Set([
  "advapi32.dll", "bcrypt.dll", "comctl32.dll", "comdlg32.dll", "crypt32.dll",
  "dbghelp.dll", "dnsapi.dll", "gdi32.dll", "imm32.dll", "iphlpapi.dll",
  "kernel32.dll", "mpr.dll", "msvcrt.dll", "mswsock.dll", "ncrypt.dll",
  "netapi32.dll", "normaliz.dll", "ntdll.dll", "ole32.dll", "oleaut32.dll",
  "psapi.dll", "rpcrt4.dll", "secur32.dll", "setupapi.dll", "shell32.dll",
  "shlwapi.dll", "ucrtbase.dll", "user32.dll", "userenv.dll", "uxtheme.dll",
  "version.dll", "winhttp.dll", "wininet.dll", "winmm.dll", "wldap32.dll",
  "ws2_32.dll", "wsock32.dll",
]);

// API sets (the Universal C runtime among them) resolve inside Windows itself.
const WINDOWS_PREFIXES = ["api-ms-win-", "ext-ms-win-"];

function isWindowsDll(name) {
  const lower = name.toLowerCase();
  return WINDOWS_DLLS.has(lower) || WINDOWS_PREFIXES.some((p) => lower.startsWith(p));
}

function cString(buf, offset) {
  const end = buf.indexOf(0, offset);
  if (end < 0) throw new Error("unterminated name");
  return buf.toString("latin1", offset, end);
}

/** The names of the DLLs a PE file imports, normal and delay-loaded. Throws if `buf` is not a PE image. */
function peImports(buf) {
  if (buf.length < 64 || buf.toString("latin1", 0, 2) !== "MZ") throw new Error("not a PE file (no MZ header)");
  const pe = buf.readUInt32LE(0x3c);
  if (pe + 24 > buf.length || buf.toString("latin1", pe, pe + 4) !== "PE\0\0") throw new Error("not a PE file (no PE signature)");
  const sectionCount = buf.readUInt16LE(pe + 6);
  const optionalSize = buf.readUInt16LE(pe + 20);
  const optional = pe + 24;
  const magic = buf.readUInt16LE(optional);
  if (magic !== 0x10b && magic !== 0x20b) throw new Error(`unknown optional header magic 0x${magic.toString(16)}`);
  const directories = optional + (magic === 0x20b ? 112 : 96);
  const directoryCount = buf.readUInt32LE(directories - 4);
  const sections = [];
  for (let i = 0, s = optional + optionalSize; i < sectionCount; i++, s += 40) {
    sections.push({
      va: buf.readUInt32LE(s + 12),
      size: Math.max(buf.readUInt32LE(s + 8), buf.readUInt32LE(s + 16)),
      raw: buf.readUInt32LE(s + 20),
    });
  }
  const offsetOf = (rva) => {
    const s = sections.find((x) => rva >= x.va && rva < x.va + x.size);
    if (!s) throw new Error(`address 0x${rva.toString(16)} is in no section`);
    return rva - s.va + s.raw;
  };
  const names = [];
  // Directory 1: imports (20-byte entries, name at +12). Directory 13: delay-load
  // imports (32-byte entries, name at +4). Both end with an all-zero entry.
  for (const [index, entrySize, nameAt] of [[1, 20, 12], [13, 32, 4]]) {
    if (index >= directoryCount) continue;
    const rva = buf.readUInt32LE(directories + index * 8);
    if (!rva) continue;
    for (let e = offsetOf(rva); ; e += entrySize) {
      const nameRva = buf.readUInt32LE(e + nameAt);
      if (!nameRva) break;
      names.push(cString(buf, offsetOf(nameRva)));
    }
  }
  return names;
}

/** Every import in `dir` that is neither beside its binary nor part of Windows: [{ file, dll }]. */
function unresolvedImports(dir) {
  const files = fs.readdirSync(dir);
  const present = new Set(files.map((f) => f.toLowerCase()));
  const missing = [];
  for (const file of files.filter((f) => /\.(exe|dll)$/i.test(f)).sort()) {
    for (const dll of peImports(fs.readFileSync(path.join(dir, file)))) {
      if (!present.has(dll.toLowerCase()) && !isWindowsDll(dll)) missing.push({ file, dll });
    }
  }
  return missing;
}

function main(dirs) {
  if (dirs.length === 0) {
    console.error("usage: check-win-dll-closure.js DIR [DIR...]");
    return 2;
  }
  let failed = 0;
  for (const dir of dirs) {
    for (const { file, dll } of unresolvedImports(dir)) {
      console.error(`ERROR: ${path.join(dir, file)} imports ${dll}, which is neither in ${dir} nor part of Windows`);
      failed++;
    }
  }
  if (failed) return 1;
  console.log(`OK: every import resolves in ${dirs.join(", ")}`);
  return 0;
}

module.exports = { peImports, unresolvedImports, isWindowsDll };

if (require.main === module) process.exit(main(process.argv.slice(2)));
