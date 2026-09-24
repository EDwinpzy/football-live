# Task 1 report — remove CloudBase from the current release path

## Result

The release command now builds the local Douban snapshot, catalogue seed, Web static assets, and embedded backend ZIP. With `--deploy`, it calls only the configured self-hosted Docker Compose entry. The old CloudBase/SCF builders, bridge packer, deployment helpers, `db_import.py`, legacy bridge tests, and tracked `cloudfunctions/` sources have been removed. The QuickJS vendor files were moved into `tools/backend-src/vendor/` for the self-hosted Web image.

The self-hosted admin now imports license batches through `tools/selfhosted/import_licenses.py`. It checks the generated CSV/ticket correspondence and ticket signatures before inserting into PostgreSQL. The admin image installs the PostgreSQL driver and receives the private Compose database connection settings. Runtime containers mount only the self-hosted helper modules they need; the one-time CloudBase migration and reconciliation tools are not included in those mounts.

The self-hosted API accepts `action=restore`. It sends no card code to the RPC, which selects the latest active, unexpired, non-banned authorization bound to the requesting device, writes an audit result, and returns the normal ticket fields. It does not modify activation time, devices, or expiry. The smoke SQL and API/client contract tests cover this behavior.

The Compose configuration retains the required internal metadata API URL and self-hosted object-storage URL, and removes the CloudBase relay, VOD fallback, and admin mode switches. The self-hosted deployment README now describes building and starting this stack. Two adjacent source comments in `tools/admin.html` and `tools/license_gen.py` were also updated because they named the deleted CloudBase packaging path.

## Verification

- `python -X utf8 -m pytest tools/tests/test_current_release_has_no_cloudbase.py -q` — **5 passed**. The deployment call is recorded by a fake runner; Docker is not started.
- Required release/config/import/restore regressions — **51 passed, 1 skipped**. The skipped check is POSIX file-permission coverage on Windows.
- `python -X utf8 -m pytest tools/backend-src/tests -q` — **379 passed**.
- `git diff --check HEAD` — passed.

## Limits

`tools/build_web_assets.py` and its `dist/` output are owned by Web Task 1 and are not present in this worktree yet. The release contract verifies the frozen no-argument call, but a real full-chain build and Docker image build could not be run until that dependency is integrated. No Compose deployment, production credential access, or production state change was performed.

## Commit

Required subject: `refactor(release): remove cloudbase production path`.
