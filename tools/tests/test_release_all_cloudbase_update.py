import unittest

from tools.release_all import cloudbase_code_update_command


class ReleaseAllCloudBaseUpdateTests(unittest.TestCase):
    def test_web_deploy_updates_only_function_code_in_the_selected_environment(self):
        self.assertEqual(
            cloudbase_code_update_command("tcb.cmd"),
            [
                "tcb.cmd",
                "fn",
                "code",
                "update",
                "otv-web",
                "--dir",
                ".",
                "-e",
                "appletv-d5ge1bth794873f76",
                "--json",
            ],
        )


if __name__ == "__main__":
    unittest.main()
