import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
NGO5_IPV4 = "https://raw.githubusercontent.com/ngo5/IPTV/main/m3u/ipv4.m3u"
URL_RE = re.compile(r"https?://[^\"'\s]+")


def list_urls(path: str, block_pattern: str) -> list[str]:
    source = (ROOT / path).read_text(encoding="utf-8")
    match = re.search(block_pattern, source, re.DOTALL)
    if not match:
        raise AssertionError(f"Could not find source list in {path}")
    return URL_RE.findall(match.group(1))


class IptvSourceListsTest(unittest.TestCase):
    def test_upstream_lists_are_synchronized_and_keep_only_supported_sources(self):
        proxy_sources = list_urls(
            "tools/backend-src/proxy.py", r"IPTV_SOURCES\s*=\s*\[(.*?)\n\]"
        )[1:]  # The first entry is the configured hosted latest playlist.
        admin_sources = list_urls(
            "tools/admin_server.py", r"IPTV_UPSTREAMS\s*=\s*\[(.*?)\n\]"
        )
        tv_sources = list_urls(
            "android/app/src/main/java/com/qiubo/optimaltv/data/repo/IptvRepository.kt",
            r"\bval\s+SOURCES\s*=\s*listOf\((.*?)\n\s*\)",
        )  # BuildConfig latest endpoint is a non-literal and is not included by URL_RE.
        mobile_sources = list_urls(
            "internal/mobile/app/src/main/java/com/qiubo/optimaltv/data/repo/IptvRepository.kt",
            r"\bval\s+SOURCES\s*=\s*listOf\((.*?)\n\s*\)",
        )

        for sources in (proxy_sources, tv_sources, mobile_sources):
            self.assertEqual(admin_sources, sources)
        self.assertEqual(10, len(admin_sources))
        self.assertEqual(len(admin_sources), len(set(admin_sources)))
        self.assertIn(NGO5_IPV4, admin_sources)
        self.assertFalse(any("YueChan/Live" in url for url in admin_sources))


if __name__ == "__main__":
    unittest.main()
