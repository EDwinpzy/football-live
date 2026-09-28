"""卡密导入必须支持先验证、后写库。

卡密迁移实际上是单向门：卡一旦导进错误的库、或签名验证不过，之后很难拆回来。
而 `import_licenses.py` 原本一跑就直接连库写盘，没有「只校验不写」的中间态——
运维只能在「什么都不验证直接写」和「先写进生产库再看对不对」之间二选一。

`--dry-run` 复用既有的 `_read_batch`（签名校验 + cards/tickets 一致性），
完全不碰数据库。真实导入路径仍要求凭据，不受影响。
"""

import pathlib
import subprocess
import sys

import pytest


REPO = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = REPO / "tools" / "selfhosted" / "import_licenses.py"


def _run(batch_dir, *args):
    """Run the importer with a clean env so no ambient DB credentials leak in."""
    env = {
        "PATH": "",
        "SystemRoot": "",
        "OTV_LICENSE_IMPORT_DB": "",
        "OTV_LICENSE_IMPORT_USER": "",
        "OTV_LICENSE_IMPORT_PASSWORD_FILE": "",
    }
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(batch_dir), *args],
        cwd=str(REPO),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )


def _a_signed_batch():
    """Prefer a real issued batch; fall back to whatever signed batch exists."""
    out = REPO / "tools" / "out"
    if out.is_dir():
        for candidate in sorted(out.iterdir()):
            if (candidate / "cards.csv").is_file() and (candidate / "tickets.jsonl").is_file():
                return candidate
    pytest.skip("no signed license batch available to validate against")


def test_script_exists():
    assert SCRIPT.is_file(), f"缺少卡密导入脚本：{SCRIPT}"


def test_dry_run_validates_without_database_credentials():
    batch = _a_signed_batch()
    result = _run(batch, "--dry-run")
    assert result.returncode == 0, f"dry-run 应当成功：{result.stderr}"
    assert "Validated" in result.stdout
    assert "No database was contacted" in result.stdout


def test_real_import_still_requires_credentials():
    """The dry-run must not weaken the guard on the real write path."""
    batch = _a_signed_batch()
    result = _run(batch)
    assert result.returncode == 1
    assert "credentials are not configured" in (result.stderr + result.stdout)


def test_dry_run_rejects_a_tampered_batch(tmp_path):
    """A dry-run that cannot fail is worthless: corrupt input must be reported."""
    batch = _a_signed_batch()
    (tmp_path / "cards.csv").write_text(
        (batch / "cards.csv").read_text(encoding="utf-8"), encoding="utf-8"
    )
    (tmp_path / "tickets.jsonl").write_text(
        (batch / "tickets.jsonl").read_text(encoding="utf-8") + '{"code":"OTV-FAKE"}\n',
        encoding="utf-8",
    )
    result = _run(tmp_path, "--dry-run")
    assert result.returncode == 1, "被篡改的批次必须校验失败"
    assert "validation failed" in result.stderr


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
