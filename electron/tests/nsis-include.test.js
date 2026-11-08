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

test("an update is never asked the questions of an install by hand", function test_nsis_include_skips_updates() {
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
// stops setup through celerpUpdateExit (exit code 2, and in an update the
// installed Celerp reopens). It never unpacks over the top while ignoring
// errors, which reported success with files missing
// (patches/app-builder-lib@25.1.8.patch).
test("setup stops with an error when the app files cannot be copied",
  function test_nsis_copy_failure_stops() {
    const extract = fs.readFileSync(path.join(templatesDir(), "include/extractAppPackage.nsh"), "utf8");
    const macro = extract.split("!macro extractUsing7za FILE")[1].split("!macroend")[0];
    const failed = macro.split("IfErrors 0 DoneExtract7za")[1].split("RetryExtract7za:")[0];
    expect(failed).toContain("/SD IDCANCEL IDRETRY RetryExtract7za");
    expect(failed).toContain("could not copy its files to $INSTDIR. Make sure the disk has enough free space");
    expect(failed.trim().split(/\s*\n\s*/).slice(-1)).toEqual([
      '!insertmacro celerpUpdateExit "the app files could not be copied to $INSTDIR"']);
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
    // in UTF-16, so a path with letters outside the code page reads back as written
    expect(begin).toContain('FileWriteUTF16LE $celerpFile "$celerpMarkerText"');
    expect(prepare).toContain("FileReadUTF16LE $celerpFind $celerpFile");
    expect(begin).toContain("SetOutPath $celerpStage");
  });

test("an installed version is never copied over: setup stops instead",
  function test_nsis_no_direct_copy_over_an_install() {
    const nsh = include();
    const stops = /The installed version is as it was\." \/SD IDOK\s*\n\s*!insertmacro celerpUpdateExit "/;
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
    expect(require).toMatch(/Nothing has been changed\." \/SD IDOK\s*\n\s*!insertmacro celerpUpdateExit "/);
  });

test("a failed swap puts the installed version back and a stopped one is undone next time",
  function test_nsis_swap_recovers() {
    const nsh = include();
    const swap = macro(nsh, "celerpSwapIn");
    expect(swap).toContain('Rename "$INSTDIR" "$celerpOld"');
    expect(swap).toContain('Rename "$celerpStage" "$INSTDIR"');
    const failed = swap.split('Rename "$celerpStage" "$INSTDIR"')[1].split("SetOutPath $INSTDIR")[0];
    expect(failed).toContain('Rename "$celerpOld" "$INSTDIR"');
    expect(failed).toContain("!insertmacro celerpUpdateExit");
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

// The body of a macro or function, in Celerp's include or a template.
function body(name) {
  for (const { lines } of installerScripts()) {
    const start = lines.findIndex((line) => line === `!macro ${name}` || line.startsWith(`!macro ${name} `)
      || line === `Function ${name}`);
    if (start > -1) return lines.slice(start + 1, lines.findIndex((line, i) => i > start
      && (line === "!macroend" || line === "FunctionEnd")));
  }
  return null;
}

// Every line a swap runs between writing its marker and registryAddInstallInfo: the
// install section's steps there, and every macro and function they reach.
function swapLines() {
  const section = template("installSection.nsh");
  const steps = section.split("!insertmacro celerpBeginCopy")[1].split("!insertmacro registryAddInstallInfo")[0];
  const seen = new Set();
  const lines = [];
  (function walk(text) {
    for (const [, name] of text.matchAll(/(?:!insertmacro|Call)\s+([\w.]+)/gi)) {
      if (seen.has(name)) continue;
      seen.add(name);
      const found = body(name);
      if (found === null) continue;
      lines.push(...found.map((line) => ({ name, line })));
      walk(found.join("\n"));
    }
  })(`!insertmacro celerpBeginCopy\n${steps}`);
  return lines;
}

test("a swap changes nothing outside its own folders that a stopped setup leaves unrecovered",
  function test_nsis_swap_writes_are_recovered() {
    const lines = swapLines();
    expect(lines.some(({ name }) => name === "celerpOldVersionLocation")).toBe(true);
    // celerpMoveRegStr's own write is the setting back
    const writes = lines.filter(({ name, line }) => name !== "celerpMoveRegStr"
      && /^(WriteReg\w+|DeleteReg\w+|CreateShortCut|WinShell::|WriteINIStr|DeleteINI\w+)\b/i.test(line));
    expect(writes.length).toBeGreaterThan(0);
    const finish = body("celerpFinishReplace");
    const markerGone = finish.findIndex((line) => line === 'Delete "$celerpMarker"');
    for (const { name, line } of writes) {
      // a registry value set to the moved-aside folder is set back before the marker goes
      const m = line.match(/^WriteRegStr (\S+) ("[^"]+") (\S+) "\$celerpOld"$/);
      expect([name, line, m !== null]).toEqual([name, line, true]);
      const restore = `!insertmacro celerpMoveRegStr ${m[1]} ${m[2]} ${m[3]} "$celerpOld" "$INSTDIR"`;
      const at = finish.indexOf(restore);
      expect([line, at > -1 && at < markerGone]).toEqual([line, true]);
    }
  });

// Runs a macro body as NSIS would in one build, over a model of the registry, the files
// and the setup mutex. An instruction it does not know fails the test.
function runNsis(lines, build, world, args = {}) {
  const defines = { PRODUCT_NAME: "Celerp", INSTALL_REGISTRY_KEY: "K", CELERP_SETUP_MUTEX: "CelerpSetupRunning",
    ...args };
  const stack = [];
  let errors = false;
  const ev = (s) => s.replace(/^"(.*)"$/, "$1").replace(/\$\{(\w+)\}/g, (all, k) => defines[k] ?? all)
    .replace(/\$\w+/g, (v) => world.vars[v] ?? "");
  const root = (r) => ({ HKEY_CURRENT_USER: "HKCU", HKEY_LOCAL_MACHINE: "HKLM" }[ev(r)] ?? ev(r));
  const cond = (t) => {
    if (t[0] === "${Errors}") return errors;
    if (t[0] === "${isUpdated}") return world.updated;
    const [a, b] = [ev(t[0]), ev(t[2])];
    if (t[1] === "==") return a.toLowerCase() === b.toLowerCase();
    if (t[1] === "!=") return a.toLowerCase() !== b.toLowerCase();
    throw new Error(`condition: ${t.join(" ")}`);
  };
  const pre = [];
  const run = [];
  for (const line of lines) {
    const t = line.match(/"[^"]*"|'[^']*'|\S+/g) || [];
    if (t.length === 0) continue;
    if (t[0] === "!ifndef" || t[0] === "!ifdef") { pre.push((t[0] === "!ifdef") === build.has(t[1])); continue; }
    if (t[0] === "!else") { pre.push(!pre.pop()); continue; }
    if (t[0] === "!endif") { pre.pop(); continue; }
    if (!pre.every(Boolean)) continue;
    if (t[0] === "${If}") { run.push(cond(t.slice(1))); continue; }
    if (t[0] === "${IfNot}") { run.push(!cond(t.slice(1))); continue; }
    if (t[0] === "${AndIf}") { run.push(run.pop() && cond(t.slice(1))); continue; }
    if (t[0] === "${EndIf}") { run.pop(); continue; }
    if (!run.every(Boolean)) continue;
    const v = (i) => ev(t[i] ?? "");
    let m;
    if (t[0] === "Push") stack.push(v(1));
    else if (t[0] === "Pop") world.vars[t[1]] = stack.pop();
    else if (t[0] === "StrCpy") {
      let s = v(2);
      if (t[4] !== undefined) s = s.slice(Number(v(4)) < 0 ? s.length + Number(v(4)) : Number(v(4)));
      if (t[3] !== undefined && v(3) !== "") s = Number(v(3)) < 0 ? s.slice(0, Number(v(3))) : s.slice(0, Number(v(3)));
      world.vars[t[1]] = s;
    } else if (t[0] === "StrLen") world.vars[t[1]] = String(v(2).length);
    else if (t[0] === "${GetParent}") world.vars[t[2]] = v(1).includes("\\") ? v(1).replace(/\\[^\\]*$/, "") : "";
    else if (t[0] === "ClearErrors") errors = false;
    else if (t[0] === "FileOpen") { world.vars[t[1]] = v(2); errors = !(v(2) in world.files); }
    else if (t[0] === "FileReadUTF16LE") world.vars[t[2]] = world.files[v(1)];
    else if (t[0] === "FileClose") continue;
    else if (t[0] === "ReadRegStr") world.vars[t[1]] = world.reg[`${root(t[2])}:${v(4)}`] ?? "";
    else if (t[0] === "WriteRegStr") world.reg[`${root(t[1])}:${v(3)}`] = v(4);
    else if (t[0] === "!insertmacro" && t[1] === "celerpMoveRegStr") {
      const [ROOT, KEY, NAME, FROM, TO] = t.slice(2).map((x) => x.replace(/^"(.*)"$/, "$1"))
        .map((x) => x.replace(/\$\{(\w+)\}/g, (all, k) => defines[k] ?? all).replace(/\$\w+/g, (n) => world.vars[n] ?? ""));
      const saved = { ...world.vars };
      runNsis(body("celerpMoveRegStr"), build, world, { ROOT, KEY, NAME, FROM, TO });
      Object.assign(world.vars, saved);
    } else if ((m = line.match(/^System::Call 'kernel32::OpenMutexW\(i 0x00100000, i 0, w "\$\{CELERP_SETUP_MUTEX\}"\) p \.r(\d) \?e'$/))) {
      world.vars[`$${m[1]}`] = world.mutex.handle;
      stack.push(world.mutex.error);
      if (world.mutex.handle !== "0") world.opened += 1;
    } else if ((m = line.match(/^System::Call 'kernel32::CloseHandle\(p r(\d)\)'$/))) {
      expect(world.vars[`$${m[1]}`]).toBe(world.mutex.handle);
      world.opened -= 1;
    } else throw new Error(`not modelled: ${line}`);
  }
  expect([pre.length, run.length]).toEqual([0, 0]);
}

// The template reads InstallLocation in these places without making it $INSTDIR.
const INSTALL_LOCATION_PRESENCE = {
  "assistedInstaller.nsh": "only whether there is an install, for the install mode page",
  "installUtil.nsh": "the folder of the version being replaced, read after setup set InstallLocation back",
};

// The steps of a swap, in the order the install section runs them, and what each leaves
// for the next reader of InstallLocation: whether the marker is there and what
// InstallLocation names (null: no value, so the default folder, which is the install).
const SWAP_STEPS = [
  { step: "before the marker", marker: false, location: "live" },
  { step: "celerpBeginCopy", marker: true, location: "live" },
  { step: "celerpSwapIn", marker: true, location: "live" },
  { step: "celerpOldVersionLocation", marker: true, location: "old" },
  { step: "the old uninstaller removed its keys", marker: true, location: null },
  { step: "celerpFinishReplace", marker: false, location: null },
  { step: "registryAddInstallInfo", marker: false, location: "live" },
];

// A setup's own folder choice (setInstallModePer*) runs before its install section creates
// the setup mutex, so the mutex is only ever another setup's: alive while that setup runs.
const WRITERS = {
  "alive": { handle: "1234", error: "0" },
  // A call that succeeds leaves the thread's last error as it was.
  "alive, stale last error": { handle: "1234", error: "2" },
  "alive, not openable by this account": { handle: "0", error: "5" },
  "gone": { handle: "0", error: "2" },
};

const ROLES = {
  "setup": { build: new Set(), updated: false },
  "an uninstaller a setup started with --updated": { build: new Set(["BUILD_UNINSTALLER"]), updated: true },
  "an uninstaller the user started": { build: new Set(["BUILD_UNINSTALLER"]), updated: false },
};

test("a setup or uninstaller started by a running setup works on the folder it was handed, "
  + "and only a setup started when none is running finishes a stopped swap",
  function test_nsis_install_dir_of_stopped_swap() {
    const section = template("installSection.nsh").split("\n").map((l) => l.trim());
    const at = (step) => section.lastIndexOf(`!insertmacro ${step}`);
    const steps = ["celerpCleanStoppedSetups", "celerpBeginCopy", "celerpSwapIn", "celerpUninstallOldVersions",
      "celerpFinishReplace", "registryAddInstallInfo"].map(at);
    expect(steps.every((i, n) => i > -1 && (n === 0 || i > steps[n - 1]))).toBe(true);
    expect(body("uninstallOldVersion")).toContain("!insertmacro celerpOldVersionLocation");
    // the mutex the folder choice reads is created only in the install section, and no
    // folder choice runs from there
    expect(macro(include(), "celerpCleanStoppedSetups"))
      .toContain('System::Call \'kernel32::CreateMutexW(p 0, i 0, w "${CELERP_SETUP_MUTEX}") p .s\'');
    for (const { name, lines } of installerScripts()) {
      const creates = lines.filter((l) => l.includes("CreateMutexW") && l.includes("CELERP_SETUP_MUTEX"));
      expect([name, creates.length]).toEqual([name, name === "build/installer.nsh" ? 1 : 0]);
      if (name === "installSection.nsh") expect(lines.filter((l) => /setInstallModePer/.test(l))).toEqual([]);
      if (name === "installer.nsi") {
        const inc = lines.indexOf('!include "installSection.nsh"');
        lines.forEach((l, i) => { if (/setInstallModePer/.test(l)) expect([l, i < inc]).toEqual([l, true]); });
      }
    }
    const parent = "C:\\Users\\a\\AppData\\Local\\Programs";
    const folders = { live: `${parent}\\celerp`, old: `${parent}\\~Olerp` };
    const cases = [];
    for (const [role, { build, updated }] of Object.entries(ROLES)) {
      for (const [writer, mutex] of Object.entries(WRITERS)) {
        for (const scope of ["HKCU", "HKLM"]) {
          for (const { step, marker, location } of SWAP_STEPS) {
            const read = location === null ? folders.live : folders[location];
            const reg = location === null ? {} : { [`${scope}:InstallLocation`]: read };
            const world = {
              vars: { $INSTDIR: read }, reg: { ...reg }, mutex, updated, opened: 0,
              files: marker ? { [`${parent}\\~Slerp`]: `Celerp setup is replacing ${folders.live}` } : {},
            };
            runNsis(body("celerpInstallDirOfStoppedSwap"), build, world, { ROOT: scope });
            const stopped = role === "setup" && writer === "gone" && marker && read === folders.old;
            const want = stopped ? folders.live : read;
            const wantReg = stopped ? { [`${scope}:InstallLocation`]: folders.live } : reg;
            cases.push([role, writer, scope, step, world.vars.$INSTDIR === want
              && JSON.stringify(world.reg) === JSON.stringify(wantReg) && world.opened === 0]);
          }
        }
      }
    }
    expect(cases.filter((c) => !c[4])).toEqual([]);
    expect(cases.length).toBe(Object.keys(ROLES).length * Object.keys(WRITERS).length * 2 * SWAP_STEPS.length);
    // the uninstaller a running setup started must have been handed the moved-aside folder
    // through the very InstallLocation value modelled at that step
    expect(macro(include(), "celerpOldVersionLocation")).toContain("StrCpy $installationDir $celerpOld");

    let readers = 0;
    for (const { name, lines } of installerScripts()) {
      lines.forEach((line, i) => {
        const m = line.match(/^(?:ReadRegStr|!insertmacro readReg) (\$\w+) "?(\$?\w+)"? "\$\{INSTALL_REGISTRY_KEY\}" InstallLocation$/);
        if (!m) return;
        readers += 1;
        const end = lines.findIndex((l, j) => j > i && (l === "!macroend" || l === "FunctionEnd"));
        const rest = lines.slice(i + 1, end);
        const into = rest.findIndex((l) => l === `StrCpy $INSTDIR ${m[1]}`);
        if (into === -1) {
          expect([name, line, INSTALL_LOCATION_PRESENCE[name] !== undefined]).toEqual([name, line, true]);
          return;
        }
        const resolve = rest.findIndex((l) => l === `!insertmacro celerpInstallDirOfStoppedSwap ${m[2]}`);
        expect([name, line, resolve > into]).toEqual([name, line, true]);
        // nothing between them reads $INSTDIR
        expect(rest.slice(into + 1, resolve).filter((l) => l.includes("$INSTDIR") && !l.startsWith("StrCpy $INSTDIR")))
          .toEqual([]);
      });
    }
    expect(readers).toBeGreaterThanOrEqual(4);
    const resolve = macro(include(), "celerpInstallDirOfStoppedSwap");
    // the folder comes only from a marker setup itself wrote, beside the moved-aside folder
    expect(resolve).toContain('FileOpen $2 "$1\\~S$3" r');
    expect(resolve).toContain('StrCpy $2 "${PRODUCT_NAME} setup is replacing $1\\"');
    expect(resolve).toContain('!insertmacro celerpMoveRegStr ${ROOT} "${INSTALL_REGISTRY_KEY}" InstallLocation "$4" "$INSTDIR"');
    expect(macro(include(), "celerpPrepareReplace"))
      .toContain('StrCpy $celerpMarkerText "${PRODUCT_NAME} setup is replacing $INSTDIR"');
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
    expect(body).toMatch(/\/SD IDOK\s*\n\s*!insertmacro celerpUpdateExit "/);
    expect(pkg.build.nsis.preCompressedFileExtensions).toEqual([]);
  });

// ---------------------------------------------------------------------------------
// An update run by the app (setup started with --updated). It shows setup's own
// progress window, closes a Celerp the user reopened meanwhile, reopens Celerp when
// it is done, and in every way it can stop it logs why and reopens the installed
// Celerp with --update-install-failed, so the user is never left with nothing open.
// ---------------------------------------------------------------------------------

const template = (rel) => fs.readFileSync(path.join(templatesDir(), rel), "utf8");

// Every Quit and Abort setup can reach. Each one either goes through celerpUpdateExit,
// or is listed here with the reason it never ends an update. A new Quit or Abort
// anywhere fails until it is placed in a row. A section that aborts (a file write
// answered Cancel) ends in .onInstFailed, which goes through celerpUpdateExit.
const ALLOWED_ENDS = [
  { file: "build/installer.nsh", within: "!macro customInit", count: 3,
    reason: "inside the not-updated guard" },
  { file: "build/installer.nsh", within: "!macro celerpUpdateExit", count: 2,
    reason: "the exit itself: the installer and the uninstaller build" },
  { file: "common.nsh", after: 'MessageBox MB_OK "$(win7Required)"', count: 1,
    reason: "an existing install met the Windows version check when it was installed" },
  { file: "common.nsh", after: 'MessageBox MB_OK|MB_ICONEXCLAMATION "$(x64WinRequired)"', count: 1,
    reason: "an existing install met the x64 check when it was installed" },
  { file: "common.nsh", after: "SetErrorLevel 0", count: 1, reason: "quitSuccess, after the relaunch" },
  { file: "assistedInstaller.nsh", after: "SetErrorLevel 0x666666", count: 1,
    reason: "inner instance that is not admin: the outer instance handles that result" },
  { file: "multiUserUi.nsh", after: "${AndIf} $1 = 1", count: 1,
    reason: "the elevated inner instance ran the install, its relaunch or its failure reopen" },
  { file: "multiUserUi.nsh", after: "${If} $1 = 1", count: 1,
    reason: "install-mode page leave: the page is skipped in an update" },
  { file: "multiUserUi.nsh", after: "!insertmacro UAC_RunElevated", count: 1, reason: "uninstaller build only" },
  { file: "installer.nsi", after: "${EndSwitch}", count: 1,
    reason: "silent per-machine elevation: an update is no longer silent" },
  { file: "uninstaller.nsh", count: 1, reason: "one-click uninstaller only" },
  { file: "multiUser.nsh", count: 1, reason: "uninstaller build only" },
  { file: "webPackage.nsh", count: 3, reason: "web installer, not built" },
  { file: "UAC.nsh", count: 1, reason: "UAC_PageElevation_OnInit is never inserted" },
  { kind: "Abort", file: "common.nsh", after: "${if} ${isUpdated}", count: 1,
    reason: "skips a page in an update; never ends setup" },
  { kind: "Abort", file: "multiUserUi.nsh", count: 6,
    reason: "install-mode page functions: each skips the page or stays on it, never ends setup" },
  { kind: "Abort", file: "uninstaller.nsh", count: 1, reason: "uninstaller build only" },
  { kind: "Abort", file: "allowOnlyOneInstallerInstance.nsh", after: "notfound:", count: 1,
    reason: "another setup is running and owns the relaunch" },
];

// Where setup stops through celerpUpdateExit, by file.
const UPDATE_EXITS = {
  "build/installer.nsh": 9,
  "extractAppPackage.nsh": 2,
  "allowOnlyOneInstallerInstance.nsh": 2,
  "installUtil.nsh": 1,
  "multiUserUi.nsh": 1,
};

function installerScripts() {
  return [path.join(__dirname, "..", pkg.build.nsis.include), ...nsisTemplates()].map((full) => ({
    name: full.startsWith(templatesDir()) ? path.basename(full) : "build/installer.nsh",
    lines: fs.readFileSync(full, "utf8").split("\n").map((line) => line.replace(/^\s*[;#].*$/, "").trim()),
  }));
}

test("every way an update can stop reopens the installed Celerp",
  function test_every_updated_exit_reopens_celerp() {
    const found = new Map(ALLOWED_ENDS.map((row) => [row, 0]));
    const unplaced = [];
    const exits = {};
    for (const { name, lines } of installerScripts()) {
      const opened = [];
      lines.forEach((line, i) => {
        if (/^!macro\s/.test(line)) opened.push(line);
        if (line === "!macroend") opened.pop();
        if (line.startsWith("!insertmacro celerpUpdateExit")) exits[name] = (exits[name] || 0) + 1;
        const kind = /(^|\$\{\|\}\s*)Quit(\s|$)/i.test(line) ? "Quit" : /^Abort(\s|$)/.test(line) ? "Abort" : null;
        if (!kind) return;
        let before = i - 1;
        while (before >= 0 && lines[before] === "") before -= 1;
        const row = ALLOWED_ENDS.find((r) => (r.kind || "Quit") === kind && r.file === name
          && (!r.within || opened.some((m) => m.startsWith(r.within)))
          && (!r.after || (before >= 0 && lines[before].endsWith(r.after))));
        if (row) found.set(row, found.get(row) + 1);
        else unplaced.push(`${name}: ${lines[before]} / ${line}`);
      });
    }
    expect(unplaced).toEqual([]);
    expect(ALLOWED_ENDS.filter((row) => found.get(row) !== row.count).map((row) => row.reason)).toEqual([]);
    expect(exits).toEqual(UPDATE_EXITS);
    expect(macro(include(), "celerpInstallerFunctions")).toContain("Function .onInstFailed");
  });

// Once the copy into the new folder has started, every way setup stops removes that
// folder and its marker, so a stopped setup leaves nothing beside the install. The
// exit itself abandons the copy, so no exit can skip it, and the copy counts as
// started only from the marker to the swap: a setup that stops before it leaves
// alone the folders it found, and one that stops after it keeps the new install.
test("every exit after the copy starts removes the new folder and its marker",
  function test_every_exit_abandons_the_copy() {
    const nsh = include();
    const scripts = installerScripts();
    const lines = (pattern) => scripts.flatMap(({ name, lines: all }) => {
      const opened = [];
      return all.flatMap((line) => {
        if (/^!macro\s/.test(line)) opened.push(line);
        if (line === "!macroend") opened.pop();
        return pattern.test(line) ? [`${name} ${opened.join(" > ")}: ${line}`] : [];
      });
    });
    expect(lines(/^!insertmacro celerpAbandonCopy$/))
      .toEqual(["build/installer.nsh !macro celerpUpdateExit REASON: !insertmacro celerpAbandonCopy"]);
    expect(lines(/\$celerpCopying\b/)).toEqual([
      'build/installer.nsh !macro celerpBeginCopy: StrCpy $celerpCopying "1"',
      'build/installer.nsh !macro celerpAbandonCopy: ${If} $celerpCopying == "1"',
      'build/installer.nsh !macro celerpAbandonCopy: StrCpy $celerpCopying "0"',
      'build/installer.nsh !macro celerpSwapIn: StrCpy $celerpCopying "0"',
    ]);
    const begin = macro(nsh, "celerpBeginCopy");
    expect(begin.split("FileClose $celerpFile")[1].trim().split(/\s*\n\s*/)[0]).toBe('StrCpy $celerpCopying "1"');
    const swapped = macro(nsh, "celerpSwapIn").split('!insertmacro celerpUpdateExit "could not move the new files into $INSTDIR"')[1];
    expect(swapped.trim().split(/\s*\n\s*/).slice(0, 2)).toEqual(["${EndIf}", 'StrCpy $celerpCopying "0"']);
    // the stock prompt for a Celerp left open, and the one that cannot be closed,
    // both stop through the exit: celerpSwapIn reaches them after the copy
    const running = template("include/allowOnlyOneInstallerInstance.nsh");
    for (const prompt of ['"$(appRunning)" /SD IDOK IDOK doStopProcess', '"$(appCannotBeClosed)" /SD IDCANCEL IDRETRY loop']) {
      expect(running.split(prompt)[1].trim().split("\n")[0]).toMatch(/^!insertmacro celerpUpdateExit "/);
    }
  });

test("an update that stops logs why and reopens the installed Celerp, and a fresh install just stops",
  function test_update_exit_reopens_installed_celerp() {
    const nsh = include();
    const [, installer] = nsh.split("!ifndef BUILD_UNINSTALLER\n  !macro celerpUpdateExit REASON");
    const body = installer.split("!macroend")[0].trim().split(/\s*\n\s*/);
    expect(body).toEqual([
      "!insertmacro celerpAbandonCopy",
      '!insertmacro celerpUpdateLog "stopped: ${REASON}"',
      "${If} ${isUpdated}",
      "HideWindow",
      '${StdUtils.ExecShellAsUser} $0 "$INSTDIR\\${APP_EXECUTABLE_FILENAME}" "open" "--updated --update-install-failed=${VERSION}"',
      '!insertmacro celerpUpdateLog "reopened $INSTDIR\\${APP_EXECUTABLE_FILENAME}: $0"',
      "${EndIf}",
      "SetErrorLevel 2",
      "Quit",
    ]);
  });

test("the uninstaller build stops without a log or a relaunch",
  function test_uninstaller_build_never_relaunches() {
    const nsh = include();
    const [, uninstaller] = nsh.split("!ifdef BUILD_UNINSTALLER\n  !macro celerpUpdateExit REASON");
    expect(uninstaller.split("!macroend")[0].trim().split(/\s*\n\s*/)).toEqual(["SetErrorLevel 2", "Quit"]);
    // the variables of an update exist in the installer build only
    const installerOnly = nsh.split("!ifndef BUILD_UNINSTALLER\n  ; swap:")[1].split("!endif")[0];
    for (const name of ["celerpFound", "celerpSetupMutex", "celerpCopying"]) expect(installerOnly).toContain(`Var ${name}`);
    expect(nsh.match(/^\s*Var /gm).length).toBe(installerOnly.match(/^\s*Var /gm).length);
  });

test("the update functions compile after every symbol they use, in the installer build only",
  function test_new_functions_compile_after_their_symbols() {
    const nsh = include();
    // each Function is defined once, inside the installer-only macro, never at the top level
    const functions = macro(nsh, "celerpInstallerFunctions");
    const names = ["celerpWriteUpdateLog", "celerpCloseReopenedApp", ".onInstFailed"];
    for (const name of names) expect(functions).toContain(`Function ${name}\n`);
    expect(nsh.match(/^\s*Function /gm).length).toBe(names.length);
    expect(functions.match(/^\s*Function /gm).length).toBe(names.length);
    const guarded = nsh.split("!ifndef BUILD_UNINSTALLER\n  !macro celerpUpdateExit REASON")[1].split(/^!endif/m)[0];
    expect(guarded).toContain("!macro celerpInstallerFunctions");
    // inserted once, after the install section and after the includes that define
    // FIND_PROCESS, $installMode, GetTime, LogicLib and APP_EXECUTABLE_FILENAME
    const nsi = template("installer.nsi");
    expect(nsi.match(/!insertmacro celerpInstallerFunctions/g).length).toBe(1);
    const at = (text) => {
      const i = nsi.indexOf(text);
      expect(i).toBeGreaterThanOrEqual(0);
      return i;
    };
    const insert = at("!insertmacro celerpInstallerFunctions");
    for (const header of ['!include "common.nsh"', '!include "multiUser.nsh"', '!include "allowOnlyOneInstallerInstance.nsh"',
      '!include "installSection.nsh"']) expect(at(header)).toBeLessThan(insert);
    const after = nsi.split('Section "install" INSTALL_SECTION_ID')[1].split("SectionEnd")[1];
    expect(after.trimStart().split(/\s*\n\s*/).slice(0, 3))
      .toEqual(["!ifndef BUILD_UNINSTALLER", "!insertmacro celerpInstallerFunctions", "!endif"]);
  });

test("a file write answered Cancel ends the update with the old Celerp reopened",
  function test_section_failure_reopens_celerp() {
    const nsh = include();
    const installerOnly = nsh.split("!ifndef BUILD_UNINSTALLER\n  ; swap:")[1].split("!endif")[0];
    expect(installerOnly.split(/\s*\n\s*/)).toContain("AllowSkipFiles off");
    expect(nsh.match(/AllowSkipFiles/g).length).toBe(1);
    const failed = macro(nsh, "celerpInstallerFunctions").split("Function .onInstFailed")[1].split("FunctionEnd")[0];
    expect(failed.trim().split(/\s*\n\s*/)).toEqual([
      '!insertmacro celerpUpdateExit "setup stopped while writing its files"',
    ]);
  });

test("an old version's uninstaller that fails is logged and setup goes on, except in a direct install",
  function test_old_uninstall_failure_is_logged() {
    const check = macro(include(), "celerpUninstallCheck");
    const [errors, rest] = check.split("${ElseIf} $R0 != 0");
    expect(errors).toMatch(/!insertmacro celerpUpdateLog "old version uninstall: [^"\n]*\$R0"/);
    expect(rest).toContain('!insertmacro celerpUpdateLog "old version uninstall: code $R0"');
    const [direct, swap] = rest.split("${EndIf}");
    expect(direct).toContain('${If} $celerpMode == "direct"');
    expect(direct).toContain("!insertmacro celerpUpdateExit");
    expect(swap).not.toContain("celerpUpdateExit");
    expect(swap).toContain("celerpUpdateLog");
  });

test("an update shows setup's progress window, also when the updater asked for a silent install",
  function test_update_runs_visible() {
    const init = macro(include(), "customInit");
    const visible = init.split("${If} ${isUpdated}")[1];
    expect(visible).toBeDefined();
    const lines = visible.trim().split(/\s*\n\s*/);
    // the log says whether the updater asked for a silent install, then setup shows its window
    expect(lines.slice(0, 4)).toEqual(['StrCpy $R9 "no"', "${If} ${Silent}", 'StrCpy $R9 "yes"', "${EndIf}"]);
    expect(lines[4]).toBe('!insertmacro celerpUpdateLog "start: ${VERSION}, mode $installMode, silent asked: $R9"');
    expect(lines[5]).toBe("SetSilent normal");
    // the not-updated prompts still come first and are unchanged
    expect(init.indexOf("${IfNot} ${isUpdated}")).toBeLessThan(init.indexOf("${If} ${isUpdated}"));
  });

test("an update skips every page but the progress page, and a fresh install keeps the finish page",
  function test_update_skips_every_page_but_progress() {
    const mode = macro(include(), "customInstallMode");
    expect(mode.trim().split(/\s*\n\s*/)).toEqual([
      "!ifndef BUILD_UNINSTALLER",
      "${If} ${isUpdated}",
      '${If} $hasPerMachineInstallation == "1"',
      'StrCpy $isForceMachineInstall "1"',
      '!insertmacro celerpUpdateLog "mode forced: machine"',
      "${Else}",
      'StrCpy $isForceCurrentInstall "1"',
      '!insertmacro celerpUpdateLog "mode forced: current"',
      "${EndIf}",
      "${EndIf}",
      "!endif",
    ]);
    const pre = template("multiUserUi.nsh").split('Function "${UNINSTALLER_FUNCPREFIX}${PRE}"')[1];
    expect(pre.indexOf("!insertmacro customInstallMode")).toBeLessThan(pre.indexOf('${if} $isForceMachineInstall == "1"'));
    const assisted = template("assistedInstaller.nsh");
    expect(assisted.split("!insertmacro skipPageIfUpdated").length).toBe(3);   // licence and directory
    expect(assisted.split("!insertmacro skipPageIfUpdated\n    !insertmacro licensePage").length).toBe(2);
    expect(assisted.split("!insertmacro skipPageIfUpdated\n    !insertmacro MUI_PAGE_DIRECTORY").length).toBe(2);
    expect(include()).not.toContain("!macro customFinishPage");
    expect(include()).not.toContain("customWelcomePage");
    expect(include()).not.toContain("MUI_FINISHPAGE_NOAUTOCLOSE");
  });

test("an update reopens the new Celerp when it is done and ends without the finish page",
  function test_updated_install_relaunches_when_not_silent() {
    const section = template("installSection.nsh");
    const assisted = section.split("!ifdef ONE_CLICK").pop().split("\n!else\n")[1].split("\n!endif")[0];
    expect(assisted.trim().split(/\s*\n\s*/).filter((line) => !line.startsWith("#"))).toEqual([
      "${if} ${isForceRun}",
      "${if} ${Silent}",
      "${orIf} ${isUpdated}",
      "!insertmacro doStartApp",
      "${endIf}",
      "${endIf}",
      "${if} ${isUpdated}",
      '!insertmacro celerpUpdateLog "done: ${VERSION} is installed"',
      "!insertmacro quitSuccess",
      "${endIf}",
    ]);
  });

test("before the swap, setup closes a Celerp the user opened during the update and says so",
  function test_swap_closes_a_reopened_celerp_first() {
    const nsh = include();
    const swap = macro(nsh, "celerpSwapIn").trim().split(/\s*\n\s*/);
    expect(swap.slice(0, 4)).toEqual(['${If} $celerpMode != "direct"', "SetOutPath $TEMP",
      '${If} $celerpMode == "swap"', "Call celerpCloseReopenedApp"]);
    const fn = macro(nsh, "celerpInstallerFunctions").split("Function celerpCloseReopenedApp")[1]
      .split("FunctionEnd")[0].trim().split(/\s*\n\s*/);
    const at = (line) => {
      const i = fn.indexOf(line);
      expect([line, i > -1]).toEqual([line, true]);
      return i;
    };
    const find = at('!insertmacro FIND_PROCESS "${APP_EXECUTABLE_FILENAME}" $celerpFound');
    const check = at("!insertmacro CHECK_APP_RUNNING");
    expect(fn.slice(check - 3, check + 4)).toEqual([
      "${IfNot} ${Silent}", "SetDetailsPrint textonly", "${EndIf}", "!insertmacro CHECK_APP_RUNNING",
      "${IfNot} ${Silent}", "SetDetailsPrint none", "${EndIf}"]);
    // the progress line gets the short notice, the update log the full one
    const said = at('!insertmacro installStage "Closed Celerp. It opens again after the update."');
    const logged = at('!insertmacro celerpUpdateLog "Closed the Celerp that was opened during the update. ' +
      'It opens again when the update is done."');
    expect(fn[said - 2]).toBe("${If} $celerpFound == 0");
    expect(find < check && check < said && said < logged).toBe(true);
    // the registers the stock check uses are the caller's again afterwards
    const pushed = fn.filter((line) => line.startsWith("Push "));
    expect(pushed.length).toBeGreaterThan(0);
    expect(fn.filter((line) => line.startsWith("Pop ")).reverse()).toEqual(pushed.map((l) => l.replace("Push", "Pop")));
    // the stock check stays once in the install section; its labels are plain, so a second copy there would clash
    expect(template("installSection.nsh").split("!insertmacro CHECK_APP_RUNNING").length).toBe(3);   // one-click and assisted
  });

test("every notice setup puts on its progress line fits the line, about 75 characters",
  function test_progress_line_notices_fit_the_status_line() {
    // installStage and DetailPrint write the one-line status above the progress bar, which
    // cuts a longer text off; a notice there is a literal so its length is known here
    const notices = [...include().matchAll(/^\s*(?:!insertmacro installStage|DetailPrint) (["`])(.*)\1\s*$/gm)]
      .map((m) => m[2]);
    expect(notices).toContain("Closed Celerp. It opens again after the update.");
    expect(notices.filter((text) => text.replace(/\$\w+/g, "").length > 75)).toEqual([]);
    expect(include().match(/!insertmacro installStage (?!")/g)).toBeNull();
  });

test("setup logs an update to install.log in the user's own logs folder, in UTF-16",
  function test_update_log_written_per_user_utf16() {
    const nsh = include();
    expect(macro(nsh, "celerpUpdateLog TEXT").trim().split(/\s*\n\s*/))
      .toEqual(['Push "${TEXT}"', "Call celerpWriteUpdateLog"]);
    const fn = macro(nsh, "celerpInstallerFunctions").split("Function celerpWriteUpdateLog")[1].split("FunctionEnd")[0];
    const lines = fn.trim().split(/\s*\n\s*/);
    const current = lines.indexOf("SetShellVarContext current");
    const open = lines.indexOf('FileOpen $R8 "$APPDATA\\${PRODUCT_NAME}\\celerp-data\\logs\\install.log" a');
    const write = lines.findIndex((line) => line.startsWith("FileWriteUTF16LE $R8 "));
    const restore = lines.indexOf('${If} $installMode == "all"');
    expect(current > -1 && current < open && open < write && write < restore).toBe(true);
    expect(lines[restore + 1]).toBe("SetShellVarContext all");
    expect(lines).toContain('CreateDirectory "$APPDATA\\${PRODUCT_NAME}\\celerp-data\\logs"');
    // a line that cannot be written is skipped: the log never stops setup
    expect(lines[open + 1]).toBe("${IfNot} ${Errors}");
    expect(fn).not.toMatch(/Quit|Abort|MessageBox/);
    // the app keeps its logs in the same folder (app.getPath("userData") is %APPDATA%\<productName>)
    expect(pkg.build.productName || pkg.productName).toBe("Celerp");
    expect(fs.readFileSync(path.join(__dirname, "..", "app-main.js"), "utf8"))
      .toContain('const DATA_DIR = path.join(app.getPath("userData"), "celerp-data");');
  });

test("an update that cannot elevate reopens Celerp; one that elevated leaves the reopen to the elevated setup",
  function test_declined_elevation_reopens_celerp() {
    const pre = template("multiUserUi.nsh").split('Function "${UNINSTALLER_FUNCPREFIX}${PRE}"')[1].split("FunctionEnd")[0];
    const elevate = pre.split('${if} $isForceMachineInstall == "1"')[1].split("!insertmacro setInstallModePerAllUsers")[0].replace(/\$\{endIf\}\s*$/, "");
    expect(elevate.trim().split(/\s*\n\s*/).slice(-8)).toEqual([
      "${ifNot} ${UAC_IsAdmin}",
      "ShowWindow $HWNDPARENT ${SW_HIDE}",
      "!insertmacro UAC_RunElevated",
      "${If} $0 = 0",
      "${AndIf} $1 = 1",
      "Quit",
      "${EndIf}",
      '!insertmacro celerpUpdateExit "elevation did not run setup: $0 $1"',
    ]);
  });
