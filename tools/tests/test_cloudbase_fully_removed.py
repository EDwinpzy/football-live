import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _tracked(name: str) -> bool:
    result = subprocess.run(
        ["git", "ls-files", name], cwd=str(ROOT),
        capture_output=True, text=True)
    return bool(result.stdout.strip())


class CloudBaseFullyRemovedTests(unittest.TestCase):
    """CloudBase 已退役，方案 P0 要求彻底删除依赖、兼容入口和片源兜底。

    运行时早已切到自建栈，但本机还留着 cloudbaserc.json（无代码读取）和
    cloudfunctions/（部署流程早已不引用）。它们不在版本控制里，却容易被
    误当成项目的一部分。这里守住两条：既不能进仓库，也不能被部署引用。
    """

    def test_cloudbase_artifacts_are_not_tracked(self):
        """残留文件可以还留在本机，但绝不能进版本控制。

        判定依据是 git 而不是文件系统：本机残留不影响别人 clone 下来的
        仓库，而一旦被提交就等于把已下线平台的配置重新带回主线。
        """
        for name in ("cloudbaserc.json", "cloudfunctions"):
            self.assertFalse(
                _tracked(name), "%s 是 CloudBase 残留，不应被 git 跟踪" % name)

    def test_gitignore_blocks_them_from_coming_back(self):
        ignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("cloudbaserc.json", ignore)
        self.assertIn("cloudfunctions/", ignore)

    def test_deploy_path_does_not_reference_cloud_functions(self):
        compose = (ROOT / "deploy" / "selfhosted" / "compose.yaml").read_text(encoding="utf-8")
        self.assertNotIn("cloudfunctions", compose)
        for path in (ROOT / "deploy" / "selfhosted").glob("Dockerfile*"):
            for line in path.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                self.assertNotIn(
                    "cloudfunctions", stripped,
                    "%s 仍在实际引用 cloudfunctions：%s" % (path.name, stripped))

    def test_quickjs_vendor_lives_in_backend_src(self):
        vendor = ROOT / "tools" / "backend-src" / "vendor"
        self.assertTrue(
            list(vendor.glob("_quickjs*.so")),
            "tools/backend-src/vendor/ 下缺少 quickjs 扩展")

    def test_no_runtime_config_points_at_cloudbase_endpoints(self):
        forbidden = ("appletv-d5ge1bth794873f76", "tcloudbase.com")
        for name in ("deploy/selfhosted/.env.example", "deploy/selfhosted/compose.yaml"):
            path = ROOT / name
            if not path.exists():
                continue
            text = path.read_text(encoding="utf-8")
            for token in forbidden:
                self.assertNotIn(token, text, "%s 仍指向 CloudBase：%s" % (name, token))


if __name__ == "__main__":
    unittest.main()
