# Task 1 report — retire CloudBase from the current release path

## Scope and baseline

This worktree is `codex/mytv-selfhost-task1`. Its starting commit was `fb75ac8` (`refactor(release): remove cloudbase production path`), which already contains the main Task 1 implementation. The follow-up fix commit contains the uncommitted review fixes, the regression updates, this report, and the local visual evidence.

## Implementation

The baseline implementation moves the current release path to the self-hosted Compose stack: the release command builds the local Douban snapshot, catalogue seed, Web static assets, and embedded backend ZIP; its deploy option invokes the configured Compose entry. CloudBase/SCF builders, bridge packers, deployment helpers, legacy database import code/tests, and tracked `cloudfunctions/` sources were removed. QuickJS vendor files now live under `tools/backend-src/vendor/` for the self-hosted Web image. The self-hosted restore action uses the device-bound latest active authorization and records its audit result without changing activation time, devices, or expiry.

The follow-up review fixes:

- Explicitly set both `OTV_CLOUD_RELAY` and `OTV_VOD_CLOUD_FALLBACK` to `0` in the self-hosted Web API environment.
- Keep PostgreSQL bootstrap credentials out of the Admin container. License imports use the dedicated `license_importer` role and a host-mounted password file mounted read-only; the README documents the role's limited column grants.
- Report actual inserted rows separately from validated tickets. `ON CONFLICT DO NOTHING` rows no longer inflate the inserted count, and the Admin page displays both numbers.
- Preserve the gateway root Admin route and add `/admin` → `/admin/` redirection plus `/admin/` prefix forwarding. Nginx strips that prefix before forwarding, so the same handler serves `/`, `/logo.png`, and `/api/*`; the page derives its asset and API base from the public path.
- Replace stale cloud-applet and cloud-identity labels in the Admin page with the self-hosted instance and importer-role labels.
- Remove the obsolete test that compared the Admin page to a retired CloudBase copy. Retain the self-hosted Docker image contract test and add a regression for the Admin identity labels.

## Verification

| Check | Command or evidence | Result |
|---|---|---|
| Full tools suite, no exclusions | `python -m pytest -q tools/tests` | **175 passed, 3 skipped** |
| Backend suite | `python -m pytest -q tools/backend-src/tests` | **379 passed** |
| Admin runtime | `python -m pytest -q tools/tests/test_selfhosted_admin_runtime.py` | **9 passed, 1 skipped** |
| Ingress contract | `test_admin_root_and_prefixed_routes_preserve_static_and_api_paths` | Confirms `/admin` redirect, prefix stripping, root routing, static resource and API path conventions. No Nginx container was started. |
| Diff formatting | `git diff --check` | Passed |
| Admin visual state | Local Playwright Chromium preview at 1600×1000 | The actual Admin HTML and logo loaded; the page visibly shows “已插入 2 / 验证 5 个卡密”. Screenshot: [`artifacts/acceptance/2026-09-24/admin/local-import-preview.png`](../../../artifacts/acceptance/2026-09-24/admin/local-import-preview.png). |

The screenshot is **simulated API, local UI visual inspection; not staging E2E evidence**. The preview served the current `tools/admin.html` and local logo on loopback and intercepted API calls with fixture responses. Its `2` inserted and `5` validated values are mock data, not a PostgreSQL write result. The earlier prefix-routing smoke also used a local prefix-strip preview rather than an Nginx container. Neither preview proves staging routing or database behavior.

## Remaining integration acceptance

Staging must verify both public Admin entries (`/` and `/admin/`) through the actual Nginx container at two viewport sizes, then perform a controlled license write and read it back from the database. Those checks remain for the integration phase. This Task 1 completion did not deploy, build APKs, publish, access staging, or touch production.

## Follow-up commit

Subject: `fix(selfhosted): isolate license import credentials and admin routes`.
