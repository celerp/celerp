# winget packaging

Manifests for publishing Celerp to the Windows Package Manager
([microsoft/winget-pkgs](https://github.com/microsoft/winget-pkgs)), so users can
`winget install celerp`.

## First submission (manual, once)

1. After the release is published, download `Celerp-Setup.exe` from the tag and
   compute its hash: `sha256sum Celerp-Setup.exe`.
2. Update `PackageVersion` (three files) and `InstallerSha256`/`InstallerUrl`
   (installer file) to match the release.
3. Fork `microsoft/winget-pkgs`, copy the three manifests to
   `manifests/c/Celerp/Celerp/<version>/`, and open a PR. Automated validation
   plus a human review runs; expect a few days.

## Every release after that (automated)

`.github/workflows/winget.yml` submits the version bump PR automatically when a
release is published. It needs the `WINGET_TOKEN` repository secret: a classic
personal access token with the `public_repo` scope (used to push the fork branch
and open the winget-pkgs PR). Until both the secret and the initial listing
exist, the workflow skips itself.
