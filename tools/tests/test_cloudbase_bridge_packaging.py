import importlib.util
import io
import sys
import tempfile
import unittest
import urllib.request
import zipfile
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tools.pack_bridge_function import pack  # noqa: E402

IPTV_SPEC = importlib.util.spec_from_file_location(
    "cloudbase_iptv_server", ROOT / "cloudfunctions" / "iptv" / "index.py")
IPTV = importlib.util.module_from_spec(IPTV_SPEC)
IPTV_SPEC.loader.exec_module(IPTV)


class BridgePackagingTests(unittest.TestCase):
    def test_scripts_are_stored_with_lf_and_the_executable_bit(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "fn"
            source.mkdir()
            (source / "scf_bootstrap").write_bytes(b"#!/bin/bash\r\nexec true\r\n")
            (source / "index.js").write_bytes(b'"use strict";\r\n')
            output = Path(tmp) / "fn.zip"
            report = pack(source, output)

            self.assertEqual(report["executables"], ["scf_bootstrap"])
            with zipfile.ZipFile(output) as archive:
                bootstrap = archive.read("scf_bootstrap")
                self.assertEqual(bootstrap, b"#!/bin/bash\nexec true\n")
                self.assertEqual(archive.read("index.js"), b'"use strict";\r\n')
                info = archive.getinfo("scf_bootstrap")
                self.assertEqual(info.external_attr >> 16, 0o100755)
                self.assertEqual(archive.getinfo("index.js").external_attr >> 16, 0o100644)

    def test_missing_bootstrap_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "fn"
            source.mkdir()
            (source / "index.js").write_bytes(b"")
            with self.assertRaises(ValueError):
                pack(source, Path(tmp) / "fn.zip")


class IptvServerTests(unittest.TestCase):
    def test_server_adapts_http_requests_into_platform_events(self):
        server = IPTV.create_server(host="127.0.0.1", port=0)
        port = server.server_address[1]
        import threading

        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with patch.object(IPTV, "main_handler", return_value={
                "statusCode": 200,
                "headers": {"Content-Type": "audio/x-mpegurl; charset=utf-8"},
                "body": "#EXTM3U\n",
            }) as handler:
                with urllib.request.urlopen(
                        f"http://127.0.0.1:{port}/latest", timeout=5) as response:
                    self.assertEqual(response.status, 200)
                    self.assertEqual(response.read(), b"#EXTM3U\n")
            event = handler.call_args[0][0]
            self.assertEqual(event["httpMethod"], "GET")
            self.assertEqual(event["path"], "/latest")
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
