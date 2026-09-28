"""热更上传脚本不得默认指向已退役的 CloudBase 生产入口。

AGENTS.md 规定的发布命令是裸跑
`node 后台管理系统/scripts/ops/hu-upload.mjs --name ... --notes ...`，
不传 `--site`。如果脚本自己还带一个 CloudBase 默认站点，这条"标准流程"就会把
热更包发到已经退役的云端去，而且日志看起来一切正常——这正是退役 CloudBase
要消灭的静默依赖。

`test_endpoint_migration_contract.py` 只检查 `IptvRepository.kt`，覆盖不到运维脚本，
所以这里单独锁死发布入口。
"""

import pathlib
import unittest


REPO = pathlib.Path(__file__).resolve().parents[2]
HU_UPLOAD = REPO / "后台管理系统" / "scripts" / "ops" / "hu-upload.mjs"


class HotupdateUploadTargetTest(unittest.TestCase):
    def test_script_exists(self):
        self.assertTrue(HU_UPLOAD.is_file(), f"缺少热更上传脚本：{HU_UPLOAD}")

    def test_no_hardcoded_cloudbase_default_site(self):
        source = HU_UPLOAD.read_text(encoding="utf-8")
        self.assertNotIn(
            "service.tcloudbase.com",
            source,
            "hu-upload.mjs 仍硬编码 CloudBase 站点作为默认值；"
            "裸跑 AGENTS.md 的发布命令会把热更发到已退役的云端。"
            "站点必须改为由 --site / SITE_URL 显式传入。",
        )

    def test_publishing_requires_an_explicit_site(self):
        """不带 --site 且没有 SITE_URL 时必须直接拒绝，不能猜一个默认站点。"""
        source = HU_UPLOAD.read_text(encoding="utf-8")
        self.assertNotIn(
            "DEFAULT_SITE",
            source,
            "hu-upload.mjs 仍定义了 DEFAULT_SITE，发布目标会回落到内置默认值；"
            "CloudBase 已退役，必须要求 --site / SITE_URL 显式传入。",
        )
        self.assertRegex(
            source,
            r"(?:console\.error|throw new Error)\([^)]*(?:--site|SITE_URL)",
            "hu-upload.mjs 缺少「未指定站点即失败」的守卫；"
            "发布目标必须显式传入，不能回落到任何内置默认值。",
        )


if __name__ == "__main__":
    unittest.main()
