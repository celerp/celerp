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
    before(section, "Finishing installation...", "!insertmacro celerpSwapIn");
  });

// When the app files cannot be copied into place (a full disk, a file still in
// use), setup retries, then asks. Cancelling, which is also the silent answer,
// stops setup with exit code 2. It never unpacks over the top while ignoring
// errors, which reported success with files missing
// (patches/app-builder-lib@25.1.8.patch).
test("setup stops with an error when the app files cannot be copied",
  function test_nsis_copy_failure_stops() {
    const extract = fs.readFileSync(path.join(templatesDir(), "include/extractAppPackage.nsh"), "utf8");
    const macro = extract.split("!macro extractUsing7za FILE")[1].split("!macroend")[0];
    const failed = macro.split("IfErrors 0 DoneExtract7za")[1].split("RetryExtract7za:")[0];
    expect(failed).toContain("/SD IDCANCEL IDRETRY RetryExtract7za");
    expect(failed).toContain("could not copy its files to $INSTDIR. Make sure the disk has enough free space");
    expect(failed.trim().split(/\s*\n\s*/).slice(-3)).toEqual(["!insertmacro celerpAbandonCopy", "SetErrorLevel 2", "Quit"]);
    expect(macro.split('Nsis7z::Extract "${FILE}"').length).toBe(2);
  });

// /D= is the last argument and is never quoted, so a folder with spaces such as
// C:\Program Files\Celerp is taken whole, the way NSIS itself reads it. Reading it
// as a single parameter stopped at the first space and installed into C:\Program.
// NSIS removes /D= from $CMDLINE, so the folder comes from the process command line
// (patches/app-builder-lib@25.1.8.patch).
test("an install folder given with /D keeps its spaces",
  function test_nsis_install_dir_keeps_spaces() {
    const multiUser = fs.readFileSync(path.join(templatesDir(), "multiUser.nsh"), "utf8");
    expect(multiUser).not.toContain('${StdUtils.GetParameter} $R0 "D" ""');
    const reader = multiUser.split("!macro installDirFromCommandLine")[1].split("!macroend")[0];
    expect(reader).toContain("System::Call 'kernel32::GetCommandLineW() t .r3'");
    expect(reader).not.toContain("$CMDLINE");
    expect(reader).toContain('${If} $0 S== " /D="');
    expect(reader).toContain('StrCpy $INSTDIR $3 "" $1');
    expect(multiUser.split("!insertmacro installDirFromCommandLine").length).toBe(3);
  });

// An install over an existing one copies the new version in full into a folder
// next to the install folder, and only then swaps it in, so a copy that fails
// (a full disk, a stopped setup) leaves the installed version as it was. The old
// version is uninstalled after the swap, from where it moved to. Free space on
// every volume setup writes to is checked before anything is changed. A folder
// that holds something other than Celerp is installed into the stock way
// (build/installer.nsh, patches/app-builder-lib@25.1.8.patch).
function macro(text, name) {
  const parts = text.split(`!macro ${name}`);
  expect(parts.length).toBe(2);
  return parts[1].split("!macroend")[0];
}

test("setup checks the disk and copies the new version aside before replacing anything",
  function test_nsis_replace_order() {
    const section = fs.readFileSync(path.join(templatesDir(), "installSection.nsh"), "utf8");
    const steps = [
      "InitPluginsDir\n!insertmacro celerpCleanStoppedSetups",
      "!insertmacro celerpPrepareReplace",
      "!insertmacro celerpCheckDiskSpace",
      '${if} $celerpMode == "direct"\n  !insertmacro celerpUninstallOldVersions\n${endIf}',
      "!insertmacro celerpBeginCopy",
      "!insertmacro installApplicationFiles",
      "!insertmacro celerpSwapIn",
      '${if} $celerpMode != "direct"\n  !insertmacro celerpUninstallOldVersions\n${endIf}',
      "!insertmacro celerpFinishReplace",
      "!insertmacro registryAddInstallInfo",
    ];
    const at = steps.map((step) => section.indexOf(step));
    expect(at.every((i) => i > -1)).toBe(true);
    expect([...at].sort((a, b) => a - b)).toEqual(at);
    expect(section).not.toContain("SetOutPath $INSTDIR");
    expect(section).not.toContain("!insertmacro uninstallOldVersion ");
    const uninstall = macro(include(), "celerpUninstallOldVersions");
    expect(uninstall).toContain("!insertmacro uninstallOldVersion SHELL_CONTEXT");
    expect(uninstall).toContain("!insertmacro uninstallOldVersion HKEY_CURRENT_USER");
  });

test("the new version is copied next to the install folder under names of the same length",
  function test_nsis_sibling_names() {
    const prepare = macro(include(), "celerpPrepareReplace");
    expect(prepare).toContain('StrCpy $celerpName $celerpName "" 2');
    expect(prepare).toContain('StrCpy $celerpStage "$celerpParent\\~N$celerpName"');
    expect(prepare).toContain('StrCpy $celerpOld "$celerpParent\\~O$celerpName"');
    expect(prepare).toContain('StrCpy $celerpMarker "$celerpParent\\~S$celerpName"');
    // only a swap this setup can prove it started, by a marker it would write, is finished or undone
    expect(prepare).toContain('StrCpy $celerpMarkerText "${PRODUCT_NAME} setup is replacing $INSTDIR"');
    expect(prepare.indexOf("${If} $celerpFile == $celerpMarkerText")).toBeLessThan(prepare.indexOf('Rename "$celerpOld" "$INSTDIR"'));
    expect(prepare).toContain('StrCpy $celerpMode "swap"');
    expect(prepare).toContain('StrCpy $celerpMode "fresh"');
    const begin = macro(include(), "celerpBeginCopy");
    expect(begin).toContain('FileWrite $celerpFile "$celerpMarkerText"');
    expect(begin).toContain("SetOutPath $celerpStage");
  });

test("an installed version is never copied over: setup stops instead",
  function test_nsis_no_direct_copy_over_an_install() {
    const nsh = include();
    const stops = /The installed version is as it was\." \/SD IDOK\s*\n\s*SetErrorLevel 2\s*\n\s*Quit/;
    // siblings it did not make, or any other reason it would copy straight into an install
    const prepare = macro(nsh, "celerpPrepareReplace");
    const found = prepare.slice(prepare.indexOf('DetailPrint "Left as found'));
    expect(found).toMatch(stops);
    const tail = prepare.slice(prepare.lastIndexOf('${If} $celerpMode == "direct"'));
    expect(tail).toContain('${AndIf} ${FileExists} "$INSTDIR\\${APP_EXECUTABLE_FILENAME}"');
    expect(tail).toMatch(stops);
    // a marker it cannot write
    const begin = macro(nsh, "celerpBeginCopy");
    const failed = begin.slice(begin.indexOf("${If} ${Errors}"));
    expect(failed.indexOf('${If} $celerpMode == "swap"')).toBeLessThan(failed.indexOf('StrCpy $celerpMode "direct"'));
    expect(failed).toMatch(stops);
  });

test("setup stops before changing anything when a volume is short of space",
  function test_nsis_disk_space_check() {
    const nsh = include();
    const check = macro(nsh, "celerpCheckDiskSpace");
    for (const where of ['"$LOCALAPPDATA"', '"$PLUGINSDIR"', '"$INSTDIR"']) {
      expect(check).toContain(`!insertmacro celerpFreeSpace ${where}`);
    }
    expect(check).toContain("${APP_64_UNPACKED_SIZE}");
    const require = macro(nsh, "celerpRequireSpace");
    expect(require).toContain("Nothing has been changed.");
    expect(require).toMatch(/Nothing has been changed\." \/SD IDOK\s*\n\s*SetErrorLevel 2\s*\n\s*Quit/);
  });

test("a failed swap puts the installed version back and a stopped one is undone next time",
  function test_nsis_swap_recovers() {
    const nsh = include();
    const swap = macro(nsh, "celerpSwapIn");
    expect(swap).toContain('Rename "$INSTDIR" "$celerpOld"');
    expect(swap).toContain('Rename "$celerpStage" "$INSTDIR"');
    const failed = swap.split('Rename "$celerpStage" "$INSTDIR"')[1].split("SetOutPath $INSTDIR")[0];
    expect(failed).toContain('Rename "$celerpOld" "$INSTDIR"');
    expect(failed).toContain("!insertmacro celerpAbandonCopy");
    expect(failed).toContain("SetErrorLevel 2");
    // a process cannot move or delete its working folder
    expect(swap.trim().split(/\s*\n\s*/)[1]).toBe("SetOutPath $TEMP");
    expect(macro(nsh, "celerpAbandonCopy")).toContain('RMDir /r "$celerpStage"');
    expect(macro(nsh, "celerpFinishReplace")).toContain('RMDir /r "$celerpOld"');
  });

test("the replaced version is uninstalled from where it moved to",
  function test_nsis_old_version_location() {
    const util = fs.readFileSync(path.join(templatesDir(), "include/installUtil.nsh"), "utf8");
    const fn = util.split("Function uninstallOldVersion")[1].split("FunctionEnd")[0];
    const hook = fn.indexOf("!insertmacro celerpOldVersionLocation");
    expect(hook).toBeGreaterThan(fn.indexOf("!insertmacro readReg $installationDir"));
    expect(hook).toBeLessThan(fn.indexOf("!insertmacro copyFile"));
    const loc = macro(include(), "celerpOldVersionLocation");
    expect(loc).toContain('StrCpy $uninstallerFileName "$celerpOld$celerpFile"');
    expect(loc).toContain("StrCpy $installationDir $celerpOld");
    // the old uninstaller reads its folder from InstallLocation, in either registry root
    expect(loc).toContain('WriteRegStr HKEY_CURRENT_USER "${INSTALL_REGISTRY_KEY}" InstallLocation "$celerpOld"');
    expect(loc).toContain('WriteRegStr SHELL_CONTEXT "${INSTALL_REGISTRY_KEY}" InstallLocation "$celerpOld"');
    const multiUser = fs.readFileSync(path.join(templatesDir(), "multiUser.nsh"), "utf8");
    expect(multiUser).toContain('ReadRegStr $perUserInstallationFolder HKCU "${INSTALL_REGISTRY_KEY}" InstallLocation');
    // and InstallLocation names $INSTDIR again once the old uninstaller is done
    const section = fs.readFileSync(path.join(templatesDir(), "installSection.nsh"), "utf8");
    expect(section.indexOf("!insertmacro registryAddInstallInfo"))
      .toBeGreaterThan(section.lastIndexOf("!insertmacro celerpUninstallOldVersions"));
    const installer = fs.readFileSync(path.join(templatesDir(), "include/installer.nsh"), "utf8");
    expect(installer).toContain('WriteRegStr SHELL_CONTEXT "${INSTALL_REGISTRY_KEY}" InstallLocation "$INSTDIR"');
  });

// Setup deletes its temporary folder as it exits, which it cannot do while that folder
// is its working folder. Where setup moves its working folder out of the install
// folder, it goes to the folder that holds the temporary folder, so a setup that stops
// (not enough space, a short unpack, a failed swap) leaves nothing behind.
test("setup never works from its own temporary folder",
  function test_nsis_working_folder_outside_temp() {
    const nsh = include();
    expect(nsh).not.toMatch(/SetOutPath\s+"?\$PLUGINSDIR"?\s*$/m);
    for (const name of ["celerpPrepareReplace", "celerpAbandonCopy", "celerpSwapIn"]) {
      expect(macro(nsh, name)).toContain("SetOutPath $TEMP");
    }
    const prepare = macro(nsh, "celerpPrepareReplace");
    expect(prepare.indexOf("SetOutPath $TEMP")).toBeLessThan(prepare.indexOf("${GetParent}"));
  });

// A setup that was stopped cannot delete its temporary folder, which holds the
// unpacked app. The next setup removes such folders: only ones marked as Celerp
// setup folders whose setup process is no longer running.
test("setup removes the temporary folders of Celerp setups that were stopped",
  function test_nsis_cleans_stopped_setups() {
    const clean = macro(include(), "celerpCleanStoppedSetups");
    expect(clean).toContain('FileOpen $celerpFile "$PLUGINSDIR\\celerp-setup.pid" w');
    expect(clean).toContain('FindFirst $celerpFind $celerpName "$celerpParent\\ns*.tmp"');
    expect(clean).toContain('${AndIf} ${FileExists} "$celerpParent\\$celerpName\\celerp-setup.pid"');
    expect(clean).toContain("kernel32::OpenProcess(i 0x1000, i 0, i $celerpPid)");
    expect(clean.split('RMDir /r "$celerpParent\\$celerpName"').length).toBe(3);
  });

// The 7-Zip plugin reports no error when the disk fills while it unpacks. The
// unpacked files are counted against the size electron-builder recorded before
// anything is copied, and a short unpack stops setup with the installed version
// as it was. Every file is in the archive, so the two sizes are comparable.
test("setup stops before copying when the app did not unpack completely",
  function test_nsis_unpack_checked() {
    const extract = fs.readFileSync(path.join(templatesDir(), "include/extractAppPackage.nsh"), "utf8");
    const unpack = extract.split("!macro extractUsing7za FILE")[1].split("!macroend")[0];
    const check = unpack.indexOf("!insertmacro celerpCheckUnpacked");
    expect(check).toBeGreaterThan(unpack.indexOf('Nsis7z::Extract "${FILE}"'));
    expect(check).toBeLessThan(unpack.indexOf("CopyFiles /SILENT"));
    const body = macro(include(), "celerpCheckUnpacked");
    expect(body).toContain('${GetSize} "$PLUGINSDIR\\7z-out" "/S=0B" $celerpBytes');
    expect(body).toContain("System::Int64Op $celerpBytes + 1023");
    expect(body).toContain("System::Int64Op $celerpKb / 1024");
    expect(body).toContain("${If} $celerpKb != ${APP_64_UNPACKED_SIZE}");
    expect(body).toMatch(/\/SD IDOK\s*\n\s*!insertmacro celerpAbandonCopy\s*\n\s*SetErrorLevel 2\s*\n\s*Quit/);
    expect(pkg.build.nsis.preCompressedFileExtensions).toEqual([]);
  });
