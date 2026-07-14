# Licensing

Celerp is free to download, self-host, and use to run your own business. Different
components use different licenses, as mapped below.

Celerp is multi-licensed. This file is the authoritative map of which license applies to which part of
the repository. Where a source file carries an `SPDX-License-Identifier` header, that header is the
operative statement for that file; this map governs by path otherwise.

| Path | License | Text |
|---|---|---|
| `celerp/` (engine/kernel) | Business Source License 1.1 | `LICENSE` |
| `celerp/gateway/`, `celerp/session_gate.py`, `celerp/ai/`, `celerp/cloud/` | Celerp Official Components License (LicenseRef-Proprietary) | `legal/OFFICIAL-COMPONENTS.md` |
| `celerp/output/` (official document/PDF + public share renderers) | Celerp Official Components License (LicenseRef-Proprietary) | `legal/OFFICIAL-COMPONENTS.md` |
| `ui/` (application UI layer) | Celerp Official Components License (LicenseRef-Proprietary) | `legal/OFFICIAL-COMPONENTS.md` |
| `default_modules/celerp-admin/` | Business Source License 1.1 | `LICENSE` |
| `default_modules/celerp-ai/`, `celerp-backup/`, `celerp-connectors/` | Celerp Official Components License (LicenseRef-Proprietary) | `legal/OFFICIAL-COMPONENTS.md` |
| `default_modules/celerp-accounting/` | MIT | module `LICENSE` |
| `default_modules/celerp-contacts/` | MIT | module `LICENSE` |
| `default_modules/celerp-dashboard/` | MIT | module `LICENSE` |
| `default_modules/celerp-docs/` | MIT | module `LICENSE` |
| `default_modules/celerp-inventory/` | MIT | module `LICENSE` |
| `default_modules/celerp-labels/` | MIT | module `LICENSE` |
| `default_modules/celerp-manufacturing/` | MIT | module `LICENSE` |
| `default_modules/celerp-reports/` | MIT | module `LICENSE` |
| `default_modules/celerp-subscriptions/` | MIT | module `LICENSE` |
| `default_modules/celerp-verticals/` | MIT | module `LICENSE` |

Notes:
- The BSL components use the SPDX identifier `BUSL-1.1` (the official short identifier for Business
  Source License 1.1). They are source-available, not open source, until the Change Date, on which they
  convert to the Change License stated in `LICENSE` (Apache-2.0), at the latest. BSL also converts the
  fourth anniversary of each version's first public distribution, whichever is earlier.
- The MIT modules depend on the BUSL-1.1 engine at runtime; redistributing an MIT module does not change
  the license of the engine it requires, and using the engine remains governed by `LICENSE`.
- **Attribution:** the "Powered by Celerp" mark is produced and required by the proprietary components
  (the official UI and document/PDF rendering layer) and the Cloud Services agreement. It is NOT imposed
  by the MIT modules - MIT cannot carry that restriction. See `legal/OFFICIAL-COMPONENTS.md`.
- Trademarks are not licensed by any of the above; see `legal/TRADEMARK.md`.
- Third-party dependencies retain their own licenses.

Copyright (c) 2026 Noah Severs.
