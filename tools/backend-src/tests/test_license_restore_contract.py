"""重装免重激活（设备维度恢复）契约。

2026-09-16 用户报障：同一台机器重装后必须重新输入卡密。原因=卡密只存在 App 私有
DataStore，卸载即清空，而客户端从不问服务端「这台设备以前激活过哪张卡」。设备指纹
sha256(ANDROID_ID|包名|机型) 在同一台机器重装后不变，所以补一条 restore 路径：
客户端首启动自愈 + 服务端按指纹找回已绑定的授权（不新增设备位、不改有效期）。
"""
import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[3]
TV = ROOT / "android/app/src/main/java/com/qiubo/optimaltv/license/LicenseManager.kt"
MOBILE = ROOT / "internal/mobile/app/src/main/java/com/qiubo/optimaltv/license/LicenseManager.kt"
WORKER = ROOT / "后台管理系统/worker/index.js"


class LicenseRestoreContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TV.read_text(encoding="utf-8")
        cls.mobile = MOBILE.read_text(encoding="utf-8")
        cls.worker = WORKER.read_text(encoding="utf-8")

    def test_client_restores_only_when_not_activated(self):
        self.assertIn("private suspend fun tryRestore()", self.client)
        start = self.client.index("if (_state.value is LicenseState.NotActivated) {")
        block = self.client[start:start + 220]
        self.assertIn("tryRestore()", block)

    def test_client_sends_restore_action_without_code(self):
        start = self.client.index("private suspend fun tryRestore()")
        block = self.client[start:self.client.index("private suspend fun applySuccess")]
        self.assertIn('callEndpoint("restore", "", deviceId(), null)', block)
        # 恢复必须复用同一套票据校验/落盘（不能自己写一份宽松路径）
        self.assertIn("applySuccess(resp)", block)

    def test_worker_accepts_restore_without_card_code(self):
        start = self.worker.index("async function activateCode")
        block = self.worker[start:self.worker.index("if (action === \"renew\")", start)]
        self.assertIn('"restore"', block)
        # 空卡密的 restore 不能被 CODE_RE 提前拒掉
        self.assertIn('if (action !== "restore" && !CODE_RE.test(code))', block)
        self.assertIn("restore_ok", block)
        self.assertIn("restore_not_found", block)

    def test_worker_restore_never_adds_device_or_extends_expiry(self):
        start = self.worker.index('if (action === "restore")')
        block = self.worker[start:self.worker.index("if (action === \"renew\")", start)]
        self.assertNotIn("nextDevices.push", block)
        self.assertNotIn("UPDATE licenses", block)
        self.assertIn("licenseResponse(row, serverNow)", block)

    def test_both_app_trees_identical(self):
        self.assertEqual(self.client, self.mobile, "两工程不一致，请跑 tools/sync_shared.py")


if __name__ == "__main__":
    unittest.main()
