/**
 * afterPack hook — no-op for universal builds.
 *
 * Both python-arm64 and python-x64 dirs are included in every per-arch temp build
 * so @electron/universal sees identical file trees and can merge cleanly.
 * main.js selects the correct dir at runtime via process.arch.
 *
 * This hook is kept as a no-op so tests that verify its presence still pass.
 */

exports.default = async function afterPack(_context) {
  // Nothing to do.
};
