// Copyright (c) 2026 Noah Severs
// SPDX-License-Identifier: BUSL-1.1
//
// The Windows installer is built with Celerp's NSIS include, which says what
// running it over an existing install will do. The behaviour itself is exercised
// on Windows by the "Installer version check" step of Build Binaries.
"use strict";

const fs = require("fs");
const path = require("path");

const pkg = require("../package.json");

function include() {
  const rel = pkg.build.nsis.include;
  expect(rel).toBe("build/installer.nsh");
  return fs.readFileSync(path.join(__dirname, "..", rel), "utf8");
}

test("the installer is built with Celerp's include", function test_nsis_include_is_configured() {
  expect(include()).toContain("!macro customInit");
  expect(pkg.build.nsis.oneClick).toBe(false);
});

test("the include says what will happen in plain words",
  function test_nsis_include_messages() {
    const nsh = include();
    expect(nsh).toContain('"Celerp $R0 is installed. This will update it to ${VERSION}. Your data is kept."');
    expect(nsh).toContain('"Celerp ${VERSION} is already installed. Reinstall?"');
    expect(nsh).toContain(
      '"A newer Celerp ($R0) is already installed. This file is an older version (${VERSION}). ' +
      'Open Celerp from the Start menu; it keeps itself up to date."');
    expect(nsh).toContain('Caption "${PRODUCT_NAME} ${VERSION} Setup"');
  });

test("the in-app updater is never prompted", function test_nsis_include_skips_updates() {
  const init = include().split("!macro customInit")[1];
  expect(init.trimStart().startsWith("${IfNot} ${isUpdated}")).toBe(true);
});

// The NSIS templates electron-builder compiles into the installer. Reading a
// Windows-allocated string with a fixed `&w${NSIS_MAX_STRLEN}` struct field copies
// 16 KB from a buffer sized for the actual text, which crashes setup whenever that
// buffer sits near the end of a heap page (patches/app-builder-lib@25.1.8.patch).
function templatesDir() {
  const builder = path.dirname(require.resolve("electron-builder/package.json"));
  const lib = path.dirname(require.resolve("app-builder-lib/package.json", { paths: [builder] }));
  return path.join(lib, "templates", "nsis");
}

function nsisTemplates() {
  const files = [];
  (function walk(dir) {
    for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
      const full = path.join(dir, entry.name);
      if (entry.isDirectory()) walk(full);
      else if (entry.name.endsWith(".nsh") || entry.name.endsWith(".nsi")) files.push(full);
    }
  })(templatesDir());
  return files;
}

test("no installer script reads a Windows-allocated string with a fixed 16 KB copy",
  function test_nsis_no_fixed_size_string_read() {
    const files = [...nsisTemplates(), path.join(__dirname, "..", pkg.build.nsis.include)];
    expect(files.some((f) => f.endsWith("multiUser.nsh"))).toBe(true);
    const unsafe = /System::Call\s+['"]\*\$\w+\(&[wt]\$\{NSIS_MAX_STRLEN\}/;
    const offenders = files.filter((f) => unsafe.test(fs.readFileSync(f, "utf8")));
    expect(offenders).toEqual([]);
  });

// While setup copies, unpacks and installs the app files, an interactive
// install names the stage on the line above the progress bar, so a long copy
// does not look stalled (patches/app-builder-lib@25.1.8.patch). Each stage is
// announced just before the step it names; nothing else about the steps changes.
test("an interactive install names each stage before it starts",
  function test_nsis_install_stages() {
    const read = (rel) => fs.readFileSync(path.join(templatesDir(), rel), "utf8");
    const extract = read("include/extractAppPackage.nsh");
    const section = read("installSection.nsh");
    const stage = extract.split("!macro installStage TEXT")[1].split("!macroend")[0];
    expect(stage.trim().split(/\s*\n\s*/)).toEqual([
      "${IfNot} ${Silent}", "SetDetailsPrint textonly", 'DetailPrint "${TEXT}"', "SetDetailsPrint none", "${endif}",
    ]);
    const before = (text, label, step) => {
      const at = text.indexOf(`!insertmacro installStage "${label}"`);
      expect(at).toBeGreaterThan(-1);
      expect(text.slice(at).split("\n")[1].trim().startsWith(step)).toBe(true);
    };
    before(extract, "Preparing application files...", "!insertmacro compute_files_for_current_arch");
    before(extract, "Extracting application files...", 'Nsis7z::Extract "${FILE}"');
    before(extract, "Installing application files...", "CopyFiles /SILENT");
    before(section, "Finishing installation...", "!insertmacro registryAddInstallInfo");
  });
