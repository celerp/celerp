// Copyright (c) 2026 Noah Severs
// SPDX-License-Identifier: BUSL-1.1
//
// The UI's translations (ui/locales), for the few messages the main process
// shows itself, such as an update that failed while the servers are stopped.
// No Electron imports: safe to require in Jest.

"use strict";

const fs = require("fs");
const path = require("path");

/**
 * A t(key, params) for `locale` (an OS locale such as "pt-BR"), falling back
 * to English and then to the key, as the UI's own t() does. `{name}` in a
 * message is replaced by params.name.
 *
 * @param {string} localesDir the ui/locales folder
 * @param {string} locale
 * @returns {(key: string, params?: object) => string}
 */
function translator(localesDir, locale) {
  const read = (lang) => {
    try {
      return JSON.parse(fs.readFileSync(path.join(localesDir, `${lang}.json`), "utf8"));
    } catch {
      return {};
    }
  };
  const en = read("en");
  const own = read(String(locale || "en").split(/[-_]/)[0].toLowerCase());
  return (key, params = {}) => Object.entries(params).reduce(
    (text, [name, value]) => text.split(`{${name}}`).join(String(value)), own[key] ?? en[key] ?? key);
}

module.exports = { translator };
