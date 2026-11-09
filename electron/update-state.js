// Copyright (c) 2026 Noah Severs
// SPDX-License-Identifier: BUSL-1.1
//
// Updater state kept in the main process, so a page that loads after an
// updater event can still show it. The renderer asks for this state when a
// page loads or is restored from history, and renders it with the same code
// it uses for live events.
//
// No Electron imports: safe to require in Jest, and the state lives only in
// memory, so a relaunch starts idle.

"use strict";

// Log lines kept for the update card, oldest dropped first.
const MAX_LOG_LINES = 200;

const errorLine = (message) => (message ? "Update error: " + message : "Update error");

const RELEASES = "https://github.com/celerp/celerp/releases";

// A release number such as 2.8.1 or 2.8.1-beta.2, so the link stays on a release page.
const RELEASE_VERSION = /^\d+\.\d+\.\d+(-[0-9A-Za-z.]+)?$/;

// The release page with the installer for `version`, or the latest release.
function releasePageUrl(version) {
  return RELEASE_VERSION.test(version || "") ? `${RELEASES}/tag/v${version}` : `${RELEASES}/latest`;
}

// A PEP 440 version, as packaging.version reads it: epoch, release, pre (a, b, rc), post, dev,
// local. Its spellings include the feed's (2.8.1-beta.2 is 2.8.1b2, 2.8.1-dev.1 is 2.8.1.dev1).
const PEP440 = new RegExp(String.raw`^v?(?:(\d+)!)?(\d+(?:\.\d+)*)` +
  String.raw`(?:[-_.]?(alpha|a|beta|b|preview|pre|c|rc)[-_.]?(\d+)?)?` +
  String.raw`(?:-(\d+)|[-_.]?(post|rev|r)[-_.]?(\d+)?)?` +
  String.raw`(?:[-_.]?(dev)[-_.]?(\d+)?)?` +
  String.raw`(?:\+([a-z0-9]+(?:[-_.][a-z0-9]+)*))?$`, "i");
const PRE_RANK = { a: 0, alpha: 0, b: 1, beta: 1, c: 2, rc: 2, pre: 2, preview: 2 };

// The sort key packaging.version gives `v`, or null when it is not a version.
function versionKey(v) {
  const m = PEP440.exec(String(v ?? "").trim());
  if (!m) return null;
  const [, epoch, release, pre, preN, postImplicit, postWord, postN, dev, devN, local] = m;
  const post = postImplicit ?? (postWord ? postN ?? "0" : undefined);
  const segments = release.split(".").map(BigInt);
  while (segments.length && segments.at(-1) === 0n) segments.pop();
  return [
    BigInt(epoch ?? 0),
    segments,
    // A dev release with no pre or post sorts before every pre release; no pre sorts after them.
    pre ? [1, PRE_RANK[pre.toLowerCase()], BigInt(preN ?? 0)] : dev && post === undefined ? [0] : [2],
    post === undefined ? [0] : [1, BigInt(post)],
    dev ? [0, BigInt(devN ?? 0)] : [1],
    // A local label sorts after none; its number parts after its word parts.
    local ? [1, ...local.toLowerCase().split(/[-_.]/).map((p) => (/^\d+$/.test(p) ? [1, BigInt(p)] : [0, p]))] : [0],
  ];
}

// Order two keys part by part; a key that runs out first is the smaller.
function compareKeys(x, y) {
  if (!Array.isArray(x)) return x < y ? -1 : x > y ? 1 : 0;
  for (let i = 0; i < Math.min(x.length, y.length); i++) {
    const c = compareKeys(x[i], y[i]);
    if (c) return c;
  }
  return Math.sign(x.length - y.length);
}

/**
 * Compare release numbers as packaging.version does, the order that decides whether a release
 * can open the data (tests/fixtures/version_order.json holds both to it): below 0, 0 or above 0
 * as `a` is older than, the same as or newer than `b`; NaN when either is not a version.
 */
function compareVersions(a, b) {
  const x = versionKey(a);
  const y = versionKey(b);
  return x && y ? compareKeys(x, y) : NaN;
}

// Whether release `version` can open data `recorded` opened: it is at least that version.
const opensData = (recorded, version) => compareVersions(version, recorded) >= 0;

/**
 * The state as it is shown and acted on, judged against `recorded`, the version that opened
 * the data (empty: no rule). A state that names a release (one found, or any other with a
 * version) which cannot open the data is "too-old": nothing of that release is downloaded,
 * offered, installed or told as failed. This is the only place a release is compared with the data.
 */
function judgeUpdateState(state, recorded) {
  const namesRelease = state.status === "downloading" || Boolean(state.version);
  if (!recorded || !namesRelease || opensData(recorded, state.version)) return state;
  return Object.freeze({ ...initialUpdateState(), status: "too-old", version: state.version });
}

function initialUpdateState() {
  return Object.freeze({ status: "idle", version: "", percent: 0, message: "", checking: false,
                         downloadFailed: false, failure: "", releaseUrl: "", upToDate: false, installing: false });
}

// The one state of a process that must restart before it can update for the data (trackUpdater).
const RELAUNCH = Object.freeze({ ...initialUpdateState(), status: "relaunch" });

/**
 * What failed, from the updater's error: "install" (the installer did not
 * start), "save" (Windows refused to save the download, usually antivirus
 * still scanning it), "verify" (the download failed its checksum or signature
 * check), "download", or "check". "inactive" (this copy cannot update itself) is
 * not an updater error: it is a check that ran nothing (serveUpdateState). A download that failed verification must not
 * be installed by hand, so it is the one failure with no installer to offer.
 *
 * @param {string} message the updater's error
 * @param {boolean} downloading whether a download was under way
 */
function failureKind(message, downloading) {
  if (/^(EPERM|EACCES|EBUSY):/.test(message)) return "save";
  if (/sha512|checksum|signature|not signed/i.test(message)) return "verify";
  return downloading ? "download" : "check";
}

// The failed state for `failure`, with the release page to offer, if any.
function failedState(state, failure, fields) {
  return Object.freeze({ ...state, status: "error", percent: 0, checking: false, upToDate: false, installing: false, failure,
                         releaseUrl: failure === "verify" ? "" : releasePageUrl(fields.version ?? state.version),
                         ...fields });
}

// A download percent from 0 to 100; anything that is not a number is 0.
function toPercent(value) {
  const n = Number(value);
  return Number.isFinite(n) ? Math.min(100, Math.max(0, n)) : 0;
}

/**
 * Apply one updater event. Returns the new state, or null when the event is
 * ignored.
 *
 * - "downloaded" is terminal for the running app: nothing moves it.
 * - not-available while downloading is ignored: the download in progress still
 *   ends in downloaded or error.
 * - a check marks an idle or failed state as checking, keeping the rest of it,
 *   until the check has a result. A failed check is replaced only by that
 *   result: not-available (up to date), found, or another error.
 * - a failed download (downloadFailed) stays until a retried download finishes
 *   or the user dismisses it: checks, found and progress leave it as it is.
 * - "install-failed" (the installer did not start, failure "install", or setup
 *   started and then stopped, failure "stopped") replaces any state, even
 *   downloaded, with a failed download that offers the installer.
 * - "installing" marks a downloaded update as handed to its installer.
 * - "inactive" is a check the user asked for that ran nothing: a failed check.
 * - not-available sets upToDate, which any later result clears.
 *
 * @param {{status: string, version: string, percent: number, message: string,
 *          checking: boolean, downloadFailed: boolean, failure: string, releaseUrl: string,
 *          upToDate: boolean, installing: boolean}} state
 * @param {{type: "check"|"dismiss"|"found"|"progress"|"downloaded"|"not-available"|"error"|"install-failed"|
 *                "installing"|"inactive",
 *          version?: string, percent?: number, message?: string, failure?: string}} event
 */
function nextUpdateState(state, event) {
  if (event.type === "install-failed") {
    return failedState(state, event.failure, { version: event.version || state.version,
                                                 message: event.message || "", downloadFailed: true });
  }
  if (state.status === "downloaded") {
    return event.type === "installing" && !state.installing ? Object.freeze({ ...state, installing: true }) : null;
  }
  const failedDownload = state.status === "error" && state.downloadFailed;
  const next = (fields) =>
    Object.freeze({ ...state, message: "", checking: false, downloadFailed: false, failure: "", releaseUrl: "",
                    upToDate: false, ...fields });
  switch (event.type) {
    case "check":
      if ((state.status !== "idle" && state.status !== "error") || failedDownload) return null;
      return Object.freeze({ ...state, checking: true });
    case "dismiss":
      if (!failedDownload) return null;
      return Object.freeze({ ...state, downloadFailed: false });
    case "found": {
      if (failedDownload) return null;
      const version = event.version || "";
      const same = state.status === "downloading" && state.version === version;
      return next({ status: "downloading", version, percent: same ? state.percent : 0 });
    }
    case "progress":
      if (failedDownload) return null;
      return next({ status: "downloading", percent: toPercent(event.percent) });
    case "downloaded":
      return next({ status: "downloaded", version: event.version || state.version, percent: 100 });
    case "not-available":
      if (state.status === "downloading" || failedDownload) return null;
      return Object.freeze({ ...initialUpdateState(), upToDate: true });
    case "error": {
      const message = event.message || "";
      const downloadFailed = state.status === "downloading" || failedDownload;
      return failedState(state, failureKind(message, downloadFailed), { message, downloadFailed });
    }
    case "inactive":
      return failedState(state, "inactive", { message: "", downloadFailed: false });
    default:
      return null;
  }
}

/**
 * Track an electron-updater instance. The state and its log lines go to the
 * window together, as `send(channel, {...state, log, revision})`, whenever
 * either changes, so the window can never be shown a demoted state. `revision`
 * rises with each change, so a state read earlier but delivered later (the
 * page's get-update-state answer racing a live event) is recognisably older. The log is the
 * history of every check, kept until relaunch. An error is always logged, even
 * when it no longer changes the state. `onFailure(state)` hears of the first
 * failure after a check the user asked for (userChecks); the app's own checks
 * and downloads fail on the update card only.
 *
 * Once downloadOnlyFrom(recorded) names a version (data a newer Celerp opened), every state
 * read, sent or acted on is judged against it (judgeUpdateState). Every check then runs with
 * the updater's own downloading off, a release found is downloaded only if it can open the
 * data, and only a downloaded release that can is installed. The updater's autoDownload is
 * put back when the check ends. That holds only for a process whose updater was first used
 * under the rule: what electron-updater did before (a download in flight, its cached file)
 * cannot be steered from here. So a rule set after the updater was used, or over a recorded
 * release that cannot open the data, leaves this process with one state, "relaunch": it
 * checks, downloads and installs nothing more, and the screen offers a restart, whose new
 * process sets the rule before its first check.
 *
 * @param {{on: (event: string, fn: Function) => void, checkForUpdates: () => Promise,
 *          downloadUpdate: () => Promise, autoDownload: boolean}} updater
 * @param {(channel: string, state: object) => void} send
 * @param {(state: object) => void} [onFailure]
 * @returns {{getUpdateState: () => object, dismissError: () => void, userChecks: () => void,
 *            installFailed: (version: string, message: string, failure: "install"|"stopped") => void,
 *            installing: () => boolean, checkedNothing: () => void, check: () => Promise,
 *            downloadOnlyFrom: (recorded: string) => void}}
 *   getUpdateState returns a copy with its log and revision; dismissError clears a failed
 *   download, at the user's request; userChecks marks the next check as the user's;
 *   installFailed records an installer that did not start, or with "stopped" a setup
 *   that started and then stopped; with no message it adds no log line; installing marks
 *   the downloaded update as handed to its installer and returns whether it did, so the
 *   hand-over happens once and only for a downloaded release the data allows; checkedNothing records a
 *   check the user asked for that ran nothing (failure "inactive"); check runs one updater
 *   check (see below).
 */
function trackUpdater(updater, send, onFailure = () => {}) {
  let state = initialUpdateState();
  let log = [];
  // Progress is logged once per 10% step, so the log stays short.
  let loggedStep = -1;
  // Rises with every change sent, so the window can tell an older state from a newer one.
  let revision = 0;
  // Set while a check the user asked for has no result yet.
  let userAsked = false;
  // Counts the updater's "error" events, so a check can tell whether its failure was told.
  let errors = 0;
  // The version that opened the data, once a release must open it to be downloaded (downloadOnlyFrom).
  // Empty (no version on record, as for an unknown schema): any release downloads.
  let recorded = "";
  // Set by the first check, the updater's first use (a download only follows a check).
  let used = false;
  // Set when the rule came after updater state it cannot judge (see above): only a restart is offered.
  let relaunch = false;

  const judged = () => (relaunch ? RELAUNCH : judgeUpdateState(state, recorded));
  // Whether the judged state is a release the user is told about: neither too-old nor waiting on a restart.
  const tells = () => judged().status !== "too-old" && judged().status !== "relaunch";
  const snapshot = () => ({ ...judged(), log: [...log], revision });

  // Apply `event`; `lineFor(state)` gives the log line for it, if any.
  // `logIgnored` still logs the line when the state ignores the event.
  // Returns whether anything changed.
  function apply(channel, event, lineFor, logIgnored) {
    const next = nextUpdateState(state, event);
    if (!next && !logIgnored) return false;
    if (next) state = next;
    const line = lineFor(state);
    if (line) log = log.concat(line).slice(-MAX_LOG_LINES);
    revision += 1;
    send(channel, snapshot());
    return true;
  }

  function failed(err) {
    errors += 1;
    const message = typeof err === "string" ? err : (err && err.message) || "";
    apply("update-error", { type: "error", message }, () => errorLine(message), true);
    if (userAsked) {
      userAsked = false;
      if (tells()) onFailure(snapshot());
    }
  }

  // Run one updater call (a check or a download). electron-updater tells a failure with an
  // "error" event and a rejection; a rejection that came without its event fails the same
  // way, so the failure is told once and nothing is left checking or downloading. Resolves
  // { result } when the call ran, else null.
  function run(call) {
    const seen = errors;
    let running;
    try {
      running = Promise.resolve(call());
    } catch (err) {
      running = Promise.reject(err);
    }
    return running.then((result) => ({ result }), (err) => {
      if (errors === seen) failed(err);
      return null;
    });
  }

  // Run one updater check (see run).
  function check() {
    if (relaunch) return Promise.resolve(null);
    used = true;
    const autoDownload = updater.autoDownload;
    if (recorded) updater.autoDownload = false;
    return run(() => updater.checkForUpdates()).finally(() => { updater.autoDownload = autoDownload; });
  }

  // Once downloaded the state is final, so a later check changes nothing, not
  // even the log that says the update is ready.
  updater.on("checking-for-update", () =>
    apply("update-log", { type: "check" }, () => "Checking for update..."));
  updater.on("update-available", (info) => {
    loggedStep = -1;
    apply("update-available", { type: "found", version: info && info.version },
      (s) => (s.version ? "Found v" + s.version : "Found an update") +
        (judged().status === "too-old" ? ", which cannot open this data." : ", downloading..."));
    // A release that cannot open the data is the check's result: nothing downloads, nothing is told.
    if (judged().status === "too-old") userAsked = false;
    // Under the rule the updater's own downloading is off, so the release is downloaded here.
    else if (recorded && judged().status === "downloading") run(() => updater.downloadUpdate());
  });
  updater.on("download-progress", (progress) =>
    apply("download-progress", { type: "progress", percent: progress && progress.percent }, (s) => {
      const step = Math.floor(s.percent / 10);
      if (step <= loggedStep) return "";
      loggedStep = step;
      const kbps = Math.round(((progress && Number(progress.bytesPerSecond)) || 0) / 1024);
      return "Downloading: " + Math.round(s.percent) + "% (" + kbps + " KB/s)";
    }));
  updater.on("update-downloaded", (info) => {
    userAsked = false;
    apply("update-downloaded", { type: "downloaded", version: info && info.version },
      (s) => (s.version ? "v" + s.version : "The update") + " ready. Restart to install.");
  });
  updater.on("update-not-available", () => {
    userAsked = false;
    apply("update-not-available", { type: "not-available" }, () => "");
  });
  updater.on("error", failed);

  return {
    getUpdateState: snapshot,
    dismissError: () => apply("update-log", { type: "dismiss" }, () => ""),
    userChecks: () => { userAsked = true; },
    installFailed: (version, message, failure) =>
      apply("update-error", { type: "install-failed", version, message, failure }, () => (message ? errorLine(message) : "")),
    installing: () => judged().status === "downloaded" && apply("update-log", { type: "installing" }, () => ""),
    checkedNothing: () => {
      userAsked = false;
      apply("update-error", { type: "inactive" }, () => "This copy of Celerp cannot update itself.");
    },
    check,
    downloadOnlyFrom: (version) => {
      recorded = version;
      relaunch = Boolean(version) && (used || judgeUpdateState(state, version).status === "too-old");
    },
  };
}

/**
 * Serve the updater's state to the window: track `updater`, forward every
 * change to the current window, answer "get-update-state", and run the checks
 * the user asks for ("check-for-updates"). The state stays idle with an empty
 * log until the updater checks, which dev builds (`canCheck` false) never do.
 *
 * @param {{handle: (channel: string, fn: Function) => void}} ipcMain
 * @param {() => ({webContents: {send: Function}}|null)} getWindow
 * @param {{on: (event: string, fn: Function) => void, checkForUpdates: () => Promise}} updater
 * @param {boolean} canCheck
 * @param {(state: object) => void} [onFailure] see trackUpdater
 * @returns {{getUpdateState: () => object, installFailed: Function, installing: Function, check: () => Promise,
 *            checkForUser: () => void, downloadOnlyFrom: (recorded: string) => void}}
 *   installFailed, installing, downloadOnlyFrom and check (the app's own checks) as in trackUpdater;
 *   checkForUser runs a check as the user's.
 *   A check that resolves with no result (electron-updater does that, with no event, when
 *   this copy cannot update itself: an AppImage run without APPIMAGE, a snap) is a failed
 *   check, so the user is never left waiting on it.
 */
function serveUpdateState(ipcMain, getWindow, updater, canCheck, onFailure) {
  const { getUpdateState, dismissError, userChecks, installFailed, installing, checkedNothing, check, downloadOnlyFrom } = trackUpdater(updater, (channel, state) => {
    const win = getWindow();
    if (win) win.webContents.send(channel, state);
  }, onFailure);
  ipcMain.handle("get-update-state", () => getUpdateState());
  // A check the user asks for dismisses a failed download; the checks the app
  // runs by itself never do.
  const checkForUser = () => {
    dismissError();
    if (!canCheck) return;
    userChecks();
    return check().then((ran) => { if (ran && !ran.result) checkedNothing(); });
  };
  ipcMain.handle("check-for-updates", checkForUser);
  return { getUpdateState, installFailed, installing, check, checkForUser, downloadOnlyFrom };
}

module.exports = { compareVersions, initialUpdateState, nextUpdateState, trackUpdater, serveUpdateState };
