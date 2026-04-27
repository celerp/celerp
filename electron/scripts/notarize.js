// Notarize the macOS app after signing.
// Invoked by electron-builder via "afterSign" in package.json.
// Only runs when APPLE_ID env var is set (i.e. CI with secrets populated).

const { notarize } = require('@electron/notarize');
const { execSync } = require('child_process');

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

  // Guard: abort if the app was not signed with a Developer ID (ad-hoc or no signature).
  // Notarizing an unsigned app always fails with "code has no resources".
  try {
    const result = execSync(`codesign -dv "${appPath}" 2>&1`).toString();
    if (result.includes('adhoc') || result.includes('TeamIdentifier=not set')) {
      console.log('Notarization skipped: app is not signed with a Developer ID certificate.');
      return;
    }
  } catch (e) {
    console.log('Notarization skipped: could not verify app signature:', e.message);
    return;
  }

  console.log(`Notarizing ${appPath} …`);
  await notarize({
    tool: 'notarytool',
    appPath,
    appleId,
    appleIdPassword,
    teamId,
  });
  console.log('Notarization complete.');
};
