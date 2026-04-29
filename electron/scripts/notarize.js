// Notarize the macOS app after signing.
// Invoked by electron-builder via "afterSign" in package.json.
// Only runs when APPLE_ID env var is set (i.e. CI with secrets populated).

const { notarize } = require('@electron/notarize');
const { execSync } = require('child_process');

const NOTARIZE_TIMEOUT_MS = 120 * 60 * 1000; // 120 minutes (matches CI step timeout)

exports.default = async function notarizing(context) {
  const { electronPlatformName, appOutDir } = context;
  if (electronPlatformName !== 'darwin') return;

  const appleId = process.env.APPLE_ID;
  const appleIdPassword = process.env.APPLE_APP_SPECIFIC_PASSWORD;
  const teamId = process.env.APPLE_TEAM_ID;

  // Skip notarization when secrets are not present (local dev builds)
  if (!appleId || !appleIdPassword || !teamId) {
    console.log('Notarization skipped: APPLE_ID / APPLE_APP_SPECIFIC_PASSWORD / APPLE_TEAM_ID not set');
    return;
  }

  const appName = context.packager.appInfo.productFilename;
  const appPath = `${appOutDir}/${appName}.app`;

  // Guard: fail loudly if the app is not signed with a Developer ID.
  // Notarizing an ad-hoc or unsigned app always fails with "code has no resources".
  let sigInfo;
  try {
    sigInfo = execSync(`codesign -dv "${appPath}" 2>&1`).toString();
  } catch (e) {
    throw new Error(`App is not signed - codesign check failed: ${e.message}`);
  }
  if (sigInfo.includes('adhoc') || sigInfo.includes('TeamIdentifier=not set')) {
    throw new Error(
      'App was built with ad-hoc or no Developer ID signature. ' +
      'Check that CSC_LINK and CSC_KEY_PASSWORD are set correctly in CI. ' +
      'Refusing to notarize unsigned app.'
    );
  }

  console.log(`Notarizing ${appPath} … (timeout: ${NOTARIZE_TIMEOUT_MS / 60000} min)`);

  const timeoutPromise = new Promise((_, reject) =>
    setTimeout(
      () => reject(new Error(`Notarization timed out after ${NOTARIZE_TIMEOUT_MS / 60000} minutes. Apple notarization service may be slow or unreachable.`)),
      NOTARIZE_TIMEOUT_MS
    )
  );

  await Promise.race([
    notarize({
      tool: 'notarytool',
      appPath,
      appleId,
      appleIdPassword,
      teamId,
    }),
    timeoutPromise,
  ]);

  console.log('Notarization complete.');
};
