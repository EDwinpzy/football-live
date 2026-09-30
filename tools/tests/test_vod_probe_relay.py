"""Contract tests for the device-side playback-source probe relay.

The relay runs on the user's own machine and is reachable from the VPS over an
SSH reverse tunnel, so two things matter as much as the probing itself:

  1. it must refuse anything that is not a known source (no open proxy), and
  2. it must refuse LAN targets even when they are allowlisted (no SSRF).

It must also keep working on a machine whose DNS is hijacked by a local proxy
(Clash fake-IP band), which is the normal case for the machine hosting it.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
RELAY_PATH = ROOT / "tools" / "selfhosted" / "vod_probe_relay.py"


def load_relay():
    spec = importlib.util.spec_from_file_location("vod_probe_relay", RELAY_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["vod_probe_relay"] = module
    spec.loader.exec_module(module)
    return module


relay = load_relay()


def dns_mock(ip):
    return mock.patch.object(relay.socket, "getaddrinfo",
                             return_value=[(2, 1, 6, "", (ip, 443))])


class AllowlistTests(unittest.TestCase):
    def setUp(self):
        self._saved = relay.ALLOWED_HOST_SUFFIXES
        relay.ALLOWED_HOST_SUFFIXES = ("lzcdn28.com", "cj.lziapi.com", "lovedan.net")

    def tearDown(self):
        relay.ALLOWED_HOST_SUFFIXES = self._saved

    def test_allows_a_known_source_host(self):
        with dns_mock("93.184.216.34"):
            self.assertTrue(relay.validate_target("https://cj.lziapi.com/api.php/provide/vod/"))

    def test_allows_a_subdomain_of_an_allowed_host(self):
        with dns_mock("1.2.3.4"):
            self.assertTrue(relay.validate_target("https://v.lzcdn28.com/a/index.m3u8"))

    def test_refuses_an_unlisted_host_so_it_is_not_an_open_proxy(self):
        for url in ("https://evil.example/x", "https://pastebin.com/raw"):
            with self.subTest(url=url), self.assertRaises(relay.ProbeRefused):
                relay.validate_target(url)

    def test_refuses_non_http_schemes(self):
        for url in ("file:///etc/passwd", "gopher://x/", "ftp://x/"):
            with self.subTest(url=url), self.assertRaises(relay.ProbeRefused):
                relay.validate_target(url)

    def test_refuses_loopback_even_when_allowlisted(self):
        relay.ALLOWED_HOST_SUFFIXES = ("localhost",)
        with self.assertRaises(relay.ProbeRefused):
            relay.validate_target("http://localhost:8765/health")

    def test_refuses_an_allowlisted_host_that_resolves_to_lan(self):
        with dns_mock("192.168.16.5"), self.assertRaises(relay.ProbeRefused):
            relay.validate_target("https://cj.lziapi.com/api.php/provide/vod/")

    def test_refuses_metadata_service_address(self):
        with dns_mock("169.254.169.254"), self.assertRaises(relay.ProbeRefused):
            relay.validate_target("https://cj.lziapi.com/latest/meta-data/")

    def test_permits_the_clash_fake_ip_band(self):
        with dns_mock("198.18.0.62"):
            self.assertTrue(relay.validate_target("https://cj.lziapi.com/api.php/provide/vod/"))

    def test_empty_allowlist_refuses_everything(self):
        relay.ALLOWED_HOST_SUFFIXES = ()
        with self.assertRaises(relay.ProbeRefused):
            relay.validate_target("https://cj.lziapi.com/api.php/provide/vod/")


class SamplingTests(unittest.TestCase):
    def test_short_list_is_returned_whole(self):
        self.assertEqual(relay._spread_sample(["a", "b"], 8), ["a", "b"])

    def test_sample_spans_the_whole_list(self):
        picked = relay._spread_sample([str(i) for i in range(83)], 8)
        self.assertEqual(len(picked), 8)
        self.assertEqual(picked[0], "0")
        self.assertEqual(picked[-1], "82")

    def test_sample_of_one_returns_the_newest_entry(self):
        self.assertEqual(relay._spread_sample(["old", "new"], 1), ["new"])


class SourceProbeTests(unittest.TestCase):
    def setUp(self):
        self._saved = relay.ALLOWED_HOST_SUFFIXES
        relay.ALLOWED_HOST_SUFFIXES = ("cj.lziapi.com", "v.cdnlz29.com")

    def tearDown(self):
        relay.ALLOWED_HOST_SUFFIXES = self._saved

    @staticmethod
    def _play_url(host):
        return ("E01$https://" + host + "/2025/index.m3u8#"
                "E02$https://" + host + "/2025/ep2.m3u8")

    def test_reports_ok_when_interface_and_a_line_both_work(self):
        payload = {"list": [{"vod_play_url": self._play_url("v.cdnlz29.com")}]}
        with mock.patch.object(relay, "_search", return_value=payload):
            with mock.patch.object(relay, "_fetch",
                                   return_value=(200, b"#EXTM3U", 120.0)):
                with dns_mock("1.2.3.4"):
                    result = relay.probe_source(
                        "https://cj.lziapi.com/api.php/provide/vod/", "Q")
        self.assertTrue(result["ok"])
        self.assertEqual(result["lines"], 2)
        self.assertIn("index.m3u8", result["preview_url"])

    def test_reports_dead_lines_separately_from_a_dead_interface(self):
        payload = {"list": [{"vod_play_url": self._play_url("v.cdnlz29.com")}]}
        with mock.patch.object(relay, "_search", return_value=payload):
            with mock.patch.object(relay, "_fetch", return_value=(404, b"", 30.0)):
                with dns_mock("1.2.3.4"):
                    result = relay.probe_source(
                        "https://cj.lziapi.com/api.php/provide/vod/", "Q")
        self.assertFalse(result["ok"])
        self.assertEqual(result["lines"], 2)
        self.assertIn("none played", result["error"])

    def test_a_cloudflare_challenge_page_is_not_reported_as_healthy(self):
        body = b"<title>Just a moment...</title>"
        with mock.patch.object(relay, "_fetch", return_value=(403, body, 70.0)):
            with dns_mock("1.2.3.4"):
                result = relay.probe_one("https://cj.lziapi.com/api.php/provide/vod/")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "challenge page")

    def test_error_summary_stays_short_and_leaks_no_query(self):
        summary = relay._summarize_error(urllib.error.URLError("boom"))
        self.assertLessEqual(len(summary), 120)
        self.assertNotIn("token", summary)


class EndpointTests(unittest.TestCase):
    def setUp(self):
        self._saved = relay.ALLOWED_HOST_SUFFIXES
        relay.ALLOWED_HOST_SUFFIXES = ("cj.lziapi.com",)
        self.server = relay.ThreadingHTTPServer(("127.0.0.1", 0), relay.Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        relay.ALLOWED_HOST_SUFFIXES = self._saved

    def _post(self, path, payload):
        request = urllib.request.Request(
            "http://127.0.0.1:" + str(self.port) + path,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))

    def test_health_reports_the_egress_ip(self):
        with mock.patch.object(relay, "_egress_ip", return_value="203.27.106.146"):
            with urllib.request.urlopen(
                    "http://127.0.0.1:" + str(self.port) + "/health", timeout=20) as response:
                body = json.loads(response.read().decode("utf-8"))
        self.assertTrue(body["ok"])
        self.assertEqual(body["egress_ip"], "203.27.106.146")

    def test_probe_endpoint_refuses_an_unlisted_url(self):
        body = self._post("/probe", {"urls": ["https://evil.example/x"]})
        self.assertFalse(body["results"][0]["ok"])
        self.assertIn("not allowlisted", body["results"][0]["error"])

    def test_probe_endpoint_caps_the_batch(self):
        urls = ["https://cj.lziapi.com/" + str(i)
                for i in range(relay.MAX_URLS_PER_REQUEST + 10)]
        stub = {"ok": True, "status": 200, "latency_ms": 1, "error": ""}
        with mock.patch.object(relay, "probe_one", return_value=stub) as probe:
            body = self._post("/probe", {"urls": urls})
        self.assertEqual(len(body["results"]), relay.MAX_URLS_PER_REQUEST)
        self.assertEqual(probe.call_count, relay.MAX_URLS_PER_REQUEST)

    def test_unknown_path_is_404(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self._post("/probe-everything", {})
        self.assertEqual(caught.exception.code, 404)


def load_admin():
    sys.path.insert(0, str(ROOT / "tools"))
    sys.path.insert(0, str(ROOT / "tools" / "backend-src"))
    try:
        import importlib
        return importlib.import_module("admin_server")
    finally:
        sys.path.pop(0)
        sys.path.pop(0)


class AdminWiringTests(unittest.TestCase):
    SOURCE = {"api_url": "https://cj.lziapi.com/api.php/provide/vod/",
              "adapter_type": "macms_json"}

    def setUp(self):
        self.admin = load_admin()
        self._saved = self.admin.VOD_PROBE_RELAY_URL
        self.admin.VOD_PROBE_RELAY_URL = "http://192.168.16.1:18765"

    def tearDown(self):
        self.admin.VOD_PROBE_RELAY_URL = self._saved

    def test_speed_test_reports_relay_results_including_line_health(self):
        report = {"ok": True, "search_latency_ms": 512, "lines": 83,
                  "preview_url": "https://v.example/index.m3u8", "error": ""}
        with mock.patch.object(self.admin, "_relay_post", return_value=report):
            result = self.admin.h_vod_source_speed({"source": self.SOURCE})
        self.assertTrue(result["ok"])
        self.assertEqual(result["via"], "relay")
        self.assertEqual(result["latency_ms"], 512)
        self.assertEqual(result["lines"], 83)

    def test_dead_lines_are_not_reported_as_a_healthy_source(self):
        report = {"ok": False, "search_latency_ms": 700, "lines": 83,
                  "preview_url": "", "error": "interface ok, 8 lines probed, none played"}
        with mock.patch.object(self.admin, "_relay_post", return_value=report):
            result = self.admin.h_vod_source_speed({"source": self.SOURCE})
        self.assertFalse(result["ok"])
        self.assertEqual(result["via"], "relay")

    def test_unreachable_relay_is_reported_not_silently_replaced(self):
        with mock.patch.object(self.admin, "_relay_post",
                               side_effect=OSError("connection refused")):
            result = self.admin.h_vod_source_speed({"source": self.SOURCE})
        self.assertFalse(result["ok"])
        self.assertIn("中继不可达", result["error"])


class ComposeContractTests(unittest.TestCase):
    def test_compose_points_the_admin_service_at_the_relay(self):
        import yaml
        compose = yaml.safe_load(
            (ROOT / "deploy" / "selfhosted" / "compose.yaml").read_text(encoding="utf-8"))
        url = compose["services"]["admin"]["environment"].get("OTV_VOD_PROBE_RELAY_URL")
        self.assertEqual(url, "http://192.168.16.1:18765")
        # The relay listens on the docker bridge, never on a public interface.
        self.assertTrue(url.startswith("http://192.168."))


class NoUndefinedNamesTests(unittest.TestCase):
    """`UA` never existed as a module global; referencing it broke both the
    device-probe button and the relay speed test at runtime.  Pin it down."""

    def test_admin_module_defines_no_bare_UA_reference(self):
        admin = load_admin()
        self.assertTrue(hasattr(admin, "_PROBE_UA"))
        # Every outbound request header must use the existing constant.
        import inspect
        source = inspect.getsource(admin)
        self.assertNotIn('"User-Agent": UA}', source)

    def test_relay_speed_path_is_callable(self):
        admin = load_admin()
        saved = admin.VOD_PROBE_RELAY_URL
        admin.VOD_PROBE_RELAY_URL = "http://192.168.16.1:18765"
        try:
            with mock.patch.object(admin, "_relay_post",
                                   return_value={"ok": True, "search_latency_ms": 5,
                                                 "lines": 1, "preview_url": "u", "error": ""}):
                # Must not raise NameError.
                result = admin.h_vod_source_speed({"source": {
                    "api_url": "https://cj.lziapi.com/api.php/provide/vod/",
                    "adapter_type": "macms_json"}})
        finally:
            admin.VOD_PROBE_RELAY_URL = saved
        self.assertTrue(result["ok"])


if __name__ == "__main__":
    unittest.main()
