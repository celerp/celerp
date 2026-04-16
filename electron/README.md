# Celerp Electron Desktop App

Wraps the Celerp backend (FastAPI) and UI (FastHTML) into a native desktop application with a bundled Postgres database.

## Architecture

```
Electron main process
  ├── embedded-postgres  → runs Postgres as a background service in userData/celerp-data/postgres/
  ├── alembic upgrade head  → migrations on every launch
  ├── uvicorn celerp.main:app  → FastAPI on a random port
  ├── uvicorn ui.app:app  → FastHTML UI on a random port
  └── BrowserWindow  → opens UI URL
```

Data lives in the OS user data directory (`app.getPath("userData")/celerp-data/`). Postgres data persists across app restarts.

A `.jwt_secret` file is generated on first launch and reused on subsequent starts — the session stays valid across restarts.

Demo data is seeded automatically on first boot via `scripts/seed_demo.py`. A `.seed_done` flag prevents re-seeding.

## Development

```bash
cd electron
npm install
npm start
```

Requires Python + the `.venv` to be set up in the parent directory:
```bash
cd ..
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

## Building

```bash
cd electron
npm install
npm run build:mac    # → dist/Celerp-*.dmg
npm run build:win    # → dist/Celerp-*-Setup.exe
npm run build:linux  # → dist/Celerp-*.AppImage
```

**Note:** Building for Mac requires macOS. Building for Windows requires Windows or a Linux cross-compile environment (electron-builder handles this with Wine).

## Packaging Python

For distribution, the Python runtime and `.venv` must be bundled alongside the app. This is handled by electron-builder's `extraResources` config in `package.json`. The build pipeline is:

1. Build the Python venv with `pip install -e .` (no dev deps)
2. Copy `celerp/`, `ui/`, `default_modules/`, `alembic/` into the build
3. `electron-builder` packages everything into the installer

A CI/CD GitHub Action for this lives in `.github/workflows/` (to be added in Sprint S3).

## LAN multi-user

Teammates can connect to a running Celerp instance over the local network by pointing their browser at `http://<host-machine-ip>:<uiPort>`. The UI port is shown in the app's title bar in dev mode. In production, a "Share with team" option in Settings will display the local network URL.
