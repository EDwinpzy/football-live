"""Debug 包必须能跳过卡密门控，否则全新安装无法做 UI 回归测试。

问题：`android/app/build.gradle` 对**所有** build type 硬编码
`LICENSE_ENABLED = true`。但下游代码（LicenseManager / App / MainActivity /
TvScreen / PlayerScreen / FavoritesScreen / PaywallShared 共 10 处）本来就全部
判断 `BuildConfig.LICENSE_ENABLED`，PaywallShared 的注释还写着
「内测版（LICENSE_ENABLED=false）直接透传，零行为差异」——这个开关是设计好的，
只是没人把它接到 build type 上。

后果：任何全新安装的 Debug 包都会停在激活页进不了主界面，于是搜索 / 分类 /
滚动 / 详情 / 播放 / 遥控器焦点这些 UI 回归，每次都得消耗一个正式卡密槽位
（一张卡上限 2 台设备）。测试基础设施反过来绑架了正式资源。

Debug 跳过门控独立于正式包配置：两端 Release 当前也按用户要求保持 false；
Release 另有已验收正式域名的硬门槛（`otvApiOriginProblem`），不会因这个改动而放宽。
"""

import pathlib
import re
import unittest


# 2026-10-08 用户再次确认对外分发 APK 也应免激活 → 两端 Release 保持 'false'。
# 整套授权链路（LicenseManager / PaywallShared / activate 端点 / 卡密库）都还在，
# 只是不再拦人。仅按用户明确要求更改该发布策略。
EXPECTED_RELEASE_GATE = "false"


REPO = pathlib.Path(__file__).resolve().parents[2]
TV_GRADLE = REPO / "android" / "app" / "build.gradle"
MOBILE_GRADLE = REPO / "internal" / "mobile" / "app" / "build.gradle"


class DebugLicenseGateTest(unittest.TestCase):
    def test_both_gradle_projects_define_the_flag(self):
        for gradle in (TV_GRADLE, MOBILE_GRADLE):
            with self.subTest(gradle=gradle.name):
                self.assertTrue(gradle.is_file(), f"缺少构建脚本：{gradle}")
                self.assertIn("'LICENSE_ENABLED'", gradle.read_text(encoding="utf-8"))

    def test_flag_is_not_hardcoded_true(self):
        """LICENSE_ENABLED 必须由 buildType 决定，不能全局写死 true。"""
        for gradle in (TV_GRADLE, MOBILE_GRADLE):
            with self.subTest(gradle=gradle.name):
                source = gradle.read_text(encoding="utf-8")
                match = re.search(
                    r"buildConfigField\s+'boolean'\s*,\s*'LICENSE_ENABLED'\s*,\s*(.+)",
                    source,
                )
                self.assertIsNotNone(
                    match, f"{gradle.name}: 未找到 LICENSE_ENABLED 的 buildConfigField 定义"
                )
                self.assertNotEqual(
                    match.group(1).strip().strip("'\""),
                    "true",
                    f"{gradle.name}: LICENSE_ENABLED 仍被硬编码为 true，"
                    "Debug 包无法跳过卡密门控，全新安装的 UI 回归测试做不了。",
                )

    def test_release_declares_the_current_gate_policy(self):
        """Release 的卡密门禁必须**显式**声明，并遵循当前用户选择的关闭口径。

        2026-10-08 用户确认正式分发版也应免激活，所以当前期望值是 'false'。
        本测试同时守住显式声明与所选值，避免 Release 静默继承默认值或意外重开门禁。
        只有用户明确要求改变发布策略时，才同步修改 EXPECTED_RELEASE_GATE。
        """
        for gradle in (TV_GRADLE, MOBILE_GRADLE):
            with self.subTest(gradle=gradle.name):
                source = gradle.read_text(encoding="utf-8")
                self.assertRegex(
                    source,
                    r"release\s*\{(?:[^{}]|\{[^{}]*\})*?"
                    r"buildConfigField\s+'boolean'\s*,\s*'LICENSE_ENABLED'\s*,\s*'([^']+)'",
                    f"{gradle.name}: release buildType 必须显式声明 LICENSE_ENABLED，"
                    "不能删掉这行让它继承 defaultConfig 的隐式值。",
                )
                match = re.search(
                    r"release\s*\{(?:[^{}]|\{[^{}]*\})*?"
                    r"buildConfigField\s+'boolean'\s*,\s*'LICENSE_ENABLED'\s*,\s*'([^']+)'",
                    source,
                )
                self.assertEqual(
                    match.group(1), EXPECTED_RELEASE_GATE,
                    f"{gradle.name}: release 的卡密门禁当前应为 {EXPECTED_RELEASE_GATE}。"
                    "若用户明确要求启用门禁，应同步修改两端 build.gradle 和本常量。",
                )

    def test_debug_builds_explicitly_disable_the_gate(self):
        """Debug 必须显式写 false，而不是只依赖 defaultConfig 的隐式继承。"""
        for gradle in (TV_GRADLE, MOBILE_GRADLE):
            with self.subTest(gradle=gradle.name):
                source = gradle.read_text(encoding="utf-8")
                self.assertRegex(
                    source,
                    r"debug\s*\{(?:[^{}]|\{[^{}]*\})*?"
                    r"buildConfigField\s+'boolean'\s*,\s*'LICENSE_ENABLED'\s*,\s*'false'",
                    f"{gradle.name}: debug buildType 必须显式把 LICENSE_ENABLED 设为 false，"
                    "这样即便有人改默认值，Debug 也不会被卡密门控挡住。",
                )


if __name__ == "__main__":
    unittest.main()
