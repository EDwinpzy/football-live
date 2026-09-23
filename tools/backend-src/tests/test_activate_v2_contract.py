import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[2]
FUNCTION = ROOT.parent / "cloudfunctions" / "activate" / "index.js"
BRIDGE = ROOT.parent / "cloudfunctions" / "activate" / "bridge.js"


class LegacyActivationBridgeContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = FUNCTION.read_text(encoding="utf-8")
        cls.bridge = BRIDGE.read_text(encoding="utf-8")

    def test_cloudbase_function_bridges_to_selfhosted_activation(self):
        self.assertIn('functionName: "activate"', self.src)
        self.assertIn("baseUrl: origin", self.src)
        self.assertIn("ca });", self.src)
        self.assertIn('"/activate"', self.bridge)
        self.assertIn('if (verb === "POST") targetPath = "/activate"', self.bridge)


if __name__ == "__main__":
    unittest.main()
