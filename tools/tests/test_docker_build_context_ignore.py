from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_root_docker_context_excludes_local_secrets_and_nonrelease_outputs():
    ignore = (ROOT / ".dockerignore").read_text(encoding="utf-8").lower()

    for pattern in (
        ".git",
        ".env",
        ".env.*",
        "*.pem",
        "*.key",
        "*.jks",
        "*.keystore",
        "tools/keys/",
        "**/secrets/",
        "**/keys/",
        "**/*-secret*",
        "**/*-credentials*",
        "**/*-password*",
        "**/*private-key*",
        "**/*signing-key*",
        "artifacts/",
        "apk/",
        "releases/",
        "reports/",
        "shots/",
        "img_cache/",
        "**/*.apk",
        "**/*.aab",
        "后台管理系统/",
    ):
        assert pattern in ignore

    assert "dist/" not in ignore.splitlines()
