#!/bin/bash

# Custom deb postinst (replaces electron-builder's default via build.deb.afterInstall).
#
# Why we override it: electron-builder's default postinst decides the
# chrome-sandbox mode with a `unshare --user` test that runs as root. That test
# passes on Ubuntu 23.10+/24.04 (root can always unshare), so it sets
# chrome-sandbox to 0755. But those releases set
# kernel.apparmor_restrict_unprivileged_userns=1, which stops the app (running
# as a normal user) from using the namespace sandbox. Electron then falls back
# to the SUID chrome-sandbox and, finding it at 0755 instead of 4755, aborts on
# launch with SIGTRAP - so the app crashes the first time every 24.04 user opens
# it. We set 4755 unconditionally; the SUID sandbox works on every supported
# system, so this is safe everywhere.
#
# Keep the rest in sync with electron-builder's default (the /usr/bin symlink
# via update-alternatives, mime and desktop database refresh) so uninstall still
# pairs with the default postrm.

if type update-alternatives 2>/dev/null >&1; then
    # Remove previous link if it doesn't use update-alternatives
    if [ -L '/usr/bin/celerp' -a -e '/usr/bin/celerp' -a "`readlink '/usr/bin/celerp'`" != '/etc/alternatives/celerp' ]; then
        rm -f '/usr/bin/celerp'
    fi
    update-alternatives --install '/usr/bin/celerp' 'celerp' '/opt/Celerp/celerp' 100 || ln -sf '/opt/Celerp/celerp' '/usr/bin/celerp'
else
    ln -sf '/opt/Celerp/celerp' '/usr/bin/celerp'
fi

# Always install the SUID sandbox (see note above).
chmod 4755 '/opt/Celerp/chrome-sandbox' || true

if hash update-mime-database 2>/dev/null; then
    update-mime-database /usr/share/mime || true
fi

if hash update-desktop-database 2>/dev/null; then
    update-desktop-database /usr/share/applications || true
fi
