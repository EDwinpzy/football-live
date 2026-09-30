import json
import pathlib
import sys
import tempfile
import threading
import time
import unittest


BACKEND = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

import ime_hotwords  # noqa: E402


# A real unmapped CJK codepoint: U+5159 is absent from the shipped pinyin_map.json.
UNMAPPED_CJK = "\u5159"


def item(title, douban_id="1"):
    return {"id": "douban:" + douban_id, "douban_id": douban_id, "title": title}


def home(*sections, **meta):
    payload = {"sections": [{"title": name, "items": list(items)} for name, items in sections]}
    payload.update(meta)
    return payload


def dead_network(url, timeout=None):
    raise OSError("network disabled in tests")


def all_live(homes, empty=()):
    """Mark every category live, so `degraded` is empty and `stale` is false.

    Categories listed in `empty` stay absent, which is how a partial refresh is
    simulated.
    """
    result = dict(homes)
    for category in ime_hotwords.CATEGORIES:
        if category not in result and category not in empty:
            result[category] = home(("最近热门", [item("占位片_%s" % category, "0")]),
                                     stale=False)
    return result


def temp_cache():
    return pathlib.Path(tempfile.mkdtemp()) / "ime.json"


def wait_for(predicate, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


class FakeCatalog:
    """Minimal DoubanCatalog stand-in; only `home` and `snapshot` are used."""

    def __init__(self, homes=None, snapshot=None, error=None, delay=0.0):
        self.homes = homes or {}
        self.snapshot = snapshot if snapshot is not None else {"home": dict(self.homes)}
        self.error = error
        self.delay = delay
        self.calls = []
        self._lock = threading.Lock()
        self._active = 0
        self.max_active = 0

    def home(self, category, force_refresh=False):
        with self._lock:
            self.calls.append((category, force_refresh))
            self._active += 1
            self.max_active = max(self.max_active, self._active)
        try:
            if self.delay:
                time.sleep(self.delay)
            if self.error is not None:
                raise self.error
            return self.homes.get(category)
        finally:
            with self._lock:
                self._active -= 1


class FakeClock:
    def __init__(self, now=1000000.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class PinyinTest(unittest.TestCase):
    def setUp(self):
        self.pinyin = ime_hotwords.load_pinyin_map()

    def test_known_cjk_maps_to_full_pinyin_and_initials(self):
        self.assertEqual(ime_hotwords.convert_text("庆余年", self.pinyin), ("qingyunian", "qyn"))

    def test_uppercase_ascii_is_normalized(self):
        self.assertEqual(ime_hotwords.convert_text("LOVE", self.pinyin), ("love", "l"))

    def test_unknown_cjk_rejects_the_whole_term(self):
        # Known + unknown mixed title: dropping the unknown character would hand
        # back "qinghe" for "庆兙和" -- a wrong full pinyin. Reject the term.
        self.assertEqual(ime_hotwords.convert_text("庆" + UNMAPPED_CJK + "和", self.pinyin), ("", ""))
        self.assertEqual(ime_hotwords.convert_text(UNMAPPED_CJK, self.pinyin), ("", ""))

    def test_ascii_around_unknown_cjk_still_rejected(self):
        self.assertEqual(ime_hotwords.convert_text("ab" + UNMAPPED_CJK, self.pinyin), ("", ""))

    def test_mixed_title_pinyin_is_contiguous(self):
        self.assertEqual(ime_hotwords.convert_text("大明王朝", self.pinyin),
                         ("damingwangchao", "dmwc"))

    def test_punctuation_does_not_break_matching(self):
        self.assertEqual(ime_hotwords.convert_text("年会不能停！2", self.pinyin)[0],
                         "nianhuibunengting2")

    def test_shipped_pinyin_map_loads(self):
        self.assertGreater(len(self.pinyin), 20000)
        self.assertEqual(self.pinyin.get("庆"), "qing")


class PhraseFilterTest(unittest.TestCase):
    def test_sentences_urls_and_overlong_phrases_are_skipped(self):
        keep = ["庆余年", "流浪地球"]
        drop = [
            "这是一个很长的句子，它包含了逗号所以应该被跳过掉才对啊",
            "https://www.example.com/some/very/long/path/that/is/way/too/long",
            "带。句号！还有？问号，这些都不该要",
            "a" * 40,
        ]
        self.assertEqual(ime_hotwords.filter_phrase_terms(keep + drop), keep)

    def test_length_boundary_is_sixteen(self):
        self.assertTrue(ime_hotwords.acceptable_phrase("字" * 16))
        self.assertFalse(ime_hotwords.acceptable_phrase("字" * 17))

    def test_quoted_book_titles_are_skipped(self):
        self.assertFalse(ime_hotwords.acceptable_phrase("《兰香如故》"))

    def test_plain_and_space_separated_terms_are_kept(self):
        self.assertTrue(ime_hotwords.acceptable_phrase("流浪地球"))
        self.assertTrue(ime_hotwords.acceptable_phrase("山河永念 我们从未忘记"))

    def test_supplement_is_capped_and_deduped(self):
        values = ["热搜%d" % i for i in range(100)] + ["热搜0", "热搜0"]
        out = ime_hotwords.filter_phrase_terms(values)
        self.assertEqual(len(out), ime_hotwords.MAX_GENERAL_WORDS)
        self.assertEqual(len(out), len(set(out)))


class BaiduParseTest(unittest.TestCase):
    def test_parses_embedded_sdata_html(self):
        payload = {"data": {"cards": [{"content": [
            {"word": "庆余年", "hotScore": "1"}, {"word": "流浪地球"}]}]}}
        html = "<html><!--s-data:%s--></html>" % json.dumps(payload, ensure_ascii=False)
        self.assertEqual(ime_hotwords.parse_baidu_board(html), ["庆余年", "流浪地球"])

    def test_parses_nested_content_shape(self):
        payload = {"data": {"cards": [{"content": [
            {"content": [{"word": "甲"}, {"word": "乙"}]}]}]}}
        html = "<!--s-data:%s-->" % json.dumps(payload, ensure_ascii=False)
        self.assertEqual(ime_hotwords.parse_baidu_board(html), ["甲", "乙"])

    def test_garbage_input_yields_no_titles(self):
        self.assertEqual(ime_hotwords.parse_baidu_board("no marker here"), [])
        self.assertEqual(ime_hotwords.parse_baidu_board("<!--s-data:{broken-->"), [])
        self.assertEqual(ime_hotwords.parse_baidu_board({"data": {"cards": []}}), [])

    def test_supplement_url_is_the_official_baidu_board(self):
        self.assertTrue(ime_hotwords.SUPPLEMENT_URL.startswith("https://top.baidu.com/board"))


class BuildWordsTest(unittest.TestCase):
    def setUp(self):
        self.pinyin = ime_hotwords.load_pinyin_map()

    def test_film_names_come_first_and_contract_shape_holds(self):
        words = ime_hotwords.build_words([{"text": "庆余年", "kind": "tv", "weight": 90}],
                                         ["流浪地球"], self.pinyin)
        self.assertEqual(words[0]["text"], "庆余年")
        self.assertEqual(words[0]["pinyin"], "qingyunian")
        self.assertEqual(words[0]["initials"], "qyn")
        self.assertEqual(words[-1]["kind"], "general")
        for word in words:
            self.assertEqual(set(word), {"text", "pinyin", "initials", "kind", "weight"})
            self.assertIsInstance(word["weight"], int)

    def test_duplicate_titles_collapse_and_film_kind_wins(self):
        films = [{"text": "猫和老鼠", "kind": "anime", "weight": 50}]
        words = ime_hotwords.build_words(films, ["猫和老鼠"], self.pinyin)
        self.assertEqual([w["text"] for w in words].count("猫和老鼠"), 1)
        self.assertEqual(words[0]["kind"], "anime")
        self.assertEqual(words[0]["weight"], 50)

    def test_unmappable_title_is_dropped_entirely(self):
        words = ime_hotwords.build_words(
            [{"text": "庆" + UNMAPPED_CJK, "kind": "movie", "weight": 10}],
            ["庆" + UNMAPPED_CJK], self.pinyin)
        self.assertEqual(words, [])

    def test_limit_is_respected(self):
        films = [{"text": "词%d" % i, "kind": "movie", "weight": i} for i in range(50)]
        words = ime_hotwords.build_words(films, ["热%d" % i for i in range(50)],
                                         self.pinyin, limit=20)
        self.assertEqual(len(words), 20)

    def test_weight_is_clamped_and_never_raises(self):
        words = ime_hotwords.build_words(
            [{"text": "庆余年", "kind": "tv", "weight": "9" * 20},
             {"text": "大明王朝", "kind": "tv", "weight": object()}], [], self.pinyin)
        self.assertTrue(all(0 <= w["weight"] <= 100000 for w in words))


class CollectTermsTest(unittest.TestCase):
    def test_degraded_homes_still_yield_titles(self):
        homes = {
            "movie": home(("最近热门", [item("大明王朝")]), source="douban", stale=False),
            "tv": home(("最近热门", [item("庆余年")]), source="douban-snapshot", stale=True),
            "anime": home(("最近热门", [item("猫和老鼠")]), upstream_available=False),
        }
        terms = ime_hotwords.collect_film_terms(homes)
        texts = {term["text"] for term in terms}
        self.assertEqual(texts, {"大明王朝", "庆余年", "猫和老鼠"})

    def test_movie_outranks_other_categories_for_same_title(self):
        homes = {
            "movie": home(("最近热门", [item("大赢家")]), stale=False),
            "short": home(("最近热门", [item("大赢家")]), stale=False),
        }
        self.assertEqual(ime_hotwords.collect_film_terms(homes)[0]["kind"], "movie")

    def test_hottest_section_ranks_first(self):
        homes = {"movie": home(("豆瓣高分", [item("冷门片")]),
                               ("最近热门", [item("热门片")]), stale=False)}
        self.assertEqual(ime_hotwords.collect_film_terms(homes)[0]["text"], "热门片")

    def test_usable_home_rejects_snapshot_and_unavailable_payloads(self):
        self.assertFalse(ime_hotwords._usable_home(home(("最近热门", [item("甲")]),
                                                       source="douban-snapshot")))
        self.assertFalse(ime_hotwords._usable_home(home(("最近热门", [item("甲")]), stale=True)))
        self.assertFalse(ime_hotwords._usable_home(home(("最近热门", [item("甲")]),
                                                       upstream_available=False)))
        self.assertFalse(ime_hotwords._usable_home(home()))
        self.assertFalse(ime_hotwords._usable_home(None))
        self.assertTrue(ime_hotwords._usable_home(home(("最近热门", [item("甲")]), stale=False)))


class SnapshotFallbackTest(unittest.TestCase):
    SNAPSHOT = {"generatedAt": "2026-09-29T10:24:16+00:00",
                "home": {"movie": home(("最近热门", [item("年会不能停！2", "36850814")]))}}

    def test_builtin_snapshot_is_served_and_not_claimed_fresh(self):
        service = ime_hotwords.HotwordService(
            FakeCatalog(snapshot=self.SNAPSHOT), cache_path=temp_cache(),
            fetch_json=dead_network, clock=FakeClock())
        payload = service.get()
        self.assertEqual(payload["words"][0]["text"], "年会不能停！2")
        self.assertEqual(payload["source"], "catalog-snapshot")
        self.assertTrue(payload["stale"])

    def test_updated_at_uses_snapshot_build_time_and_never_drifts(self):
        service = ime_hotwords.HotwordService(
            FakeCatalog(snapshot=self.SNAPSHOT), cache_path=temp_cache(),
            fetch_json=dead_network, clock=FakeClock())
        first = service.get()["updated_at"]
        for _ in range(3):
            self.assertEqual(service.get()["updated_at"], first)
        # The snapshot stamp is real, and clearly not the fake clock's epoch.
        self.assertGreater(first, 1_700_000_000)
        self.assertNotEqual(first, 1000000.0)

    def test_missing_generated_at_falls_back_to_zero(self):
        catalog = FakeCatalog(snapshot={"home": self.SNAPSHOT["home"]})
        service = ime_hotwords.HotwordService(catalog, cache_path=temp_cache(),
                                             fetch_json=dead_network, clock=FakeClock())
        self.assertEqual(service.get()["updated_at"], 0.0)

    def test_refresh_failure_keeps_snapshot_words(self):
        service = ime_hotwords.HotwordService(FakeCatalog(snapshot=self.SNAPSHOT),
                                             cache_path=temp_cache(),
                                             fetch_json=dead_network, clock=FakeClock())
        service.get()
        self.assertTrue(wait_for(lambda: service.stats()["failures"] >= 1))
        payload = service.get()
        self.assertEqual(payload["words"][0]["text"], "年会不能停！2")
        self.assertTrue(payload["stale"])


class FreshRefreshTest(unittest.TestCase):
    def live_catalog(self, title="大明王朝"):
        return FakeCatalog(
            homes=all_live({"movie": home(("最近热门", [item(title, "1")]),
                                           ("豆瓣高分", [item("猫和老鼠", "2")]), stale=False)}),
            snapshot={"home": {}})

    def test_refreshed_names_carry_full_pinyin_and_initials(self):
        clock = FakeClock()
        service = ime_hotwords.HotwordService(self.live_catalog(), cache_path=temp_cache(),
                                             fetch_json=dead_network, clock=clock)
        service.get()
        self.assertTrue(wait_for(lambda: service.stats()["refreshes"] >= 1))
        payload = service.get()
        words = {w["text"]: w for w in payload["words"]}
        self.assertEqual(words["大明王朝"]["pinyin"], "damingwangchao")
        self.assertEqual(words["大明王朝"]["initials"], "dmwc")
        self.assertEqual(words["大明王朝"]["kind"], "movie")
        self.assertEqual(words["猫和老鼠"]["kind"], "movie")
        self.assertFalse(payload["stale"])
        self.assertEqual(payload["source"], "catalog-live")
        self.assertAlmostEqual(payload["updated_at"], clock.now, delta=1.0)

    def test_fresh_cache_is_served_without_upstream_calls(self):
        service = ime_hotwords.HotwordService(self.live_catalog(), cache_path=temp_cache(),
                                             fetch_json=dead_network, clock=FakeClock())
        service.get()
        self.assertTrue(wait_for(lambda: service.stats()["refreshes"] >= 1))
        before = len(service.catalog.calls)
        service.get()
        service.get()
        self.assertEqual(len(service.catalog.calls), before)

    def test_expired_cache_serves_immediately_and_refreshes(self):
        clock = FakeClock()
        service = ime_hotwords.HotwordService(self.live_catalog(), cache_path=temp_cache(),
                                             fetch_json=dead_network, clock=clock)
        service.get()
        self.assertTrue(wait_for(lambda: service.stats()["refreshes"] >= 1))
        clock.advance(ime_hotwords.CACHE_MAX_AGE + 1)
        payload = service.get()
        self.assertEqual(payload["words"][0]["text"], "大明王朝")
        self.assertTrue(payload["stale"])
        self.assertTrue(wait_for(lambda: service.stats()["refreshes"] >= 2))

    def test_supplement_failure_never_discards_fresh_films(self):
        service = ime_hotwords.HotwordService(self.live_catalog(), cache_path=temp_cache(),
                                             fetch_json=dead_network, clock=FakeClock())
        service.get()
        self.assertTrue(wait_for(lambda: service.stats()["refreshes"] >= 1))
        payload = service.get()
        self.assertEqual(payload["words"][0]["text"], "大明王朝")
        self.assertFalse(payload["stale"])

    def test_supplement_words_are_appended_bounded_and_typed_general(self):
        def supplement(url, timeout=None):
            self.assertEqual(url, ime_hotwords.SUPPLEMENT_URL)
            return ["全网热搜第二", "另一个热搜词", "带。句号的不该要", "x" * 40]

        service = ime_hotwords.HotwordService(self.live_catalog(), cache_path=temp_cache(),
                                             fetch_json=supplement, clock=FakeClock())
        service.get()
        self.assertTrue(wait_for(lambda: service.stats()["refreshes"] >= 1))
        payload = service.get()
        general = [w for w in payload["words"] if w["kind"] == "general"]
        self.assertEqual([w["text"] for w in general], ["全网热搜第二", "另一个热搜词"])
        self.assertEqual(payload["words"][0]["kind"], "movie")

    def test_previous_general_words_survive_a_dead_supplement(self):
        state = {"fail": False}

        def supplement(url, timeout=None):
            if state["fail"]:
                raise OSError("supplement down")
            return ["热搜保留词"]

        catalog = self.live_catalog()
        clock = FakeClock()
        service = ime_hotwords.HotwordService(catalog, cache_path=temp_cache(),
                                             fetch_json=supplement, clock=clock)
        service.get()
        self.assertTrue(wait_for(lambda: service.stats()["refreshes"] >= 1))
        self.assertIn("热搜保留词", [w["text"] for w in service.get()["words"]])
        state["fail"] = True
        catalog.homes["movie"] = home(("最近热门", [item("新片上映", "9")]), stale=False)
        # Expire the clean round-1 cache so a second round actually runs.
        clock.advance(ime_hotwords.CACHE_MAX_AGE + 1)
        service.get()
        self.assertTrue(wait_for(lambda: service.stats()["refreshes"] >= 2))
        words = [w["text"] for w in service.get()["words"]]
        self.assertIn("新片上映", words)       # films still refreshed
        self.assertIn("热搜保留词", words)      # general words kept as fallback


class DegradedRefreshTest(unittest.TestCase):
    def build(self):
        # Five live categories, so the first round is clean; individual tests
        # then knock specific categories out to simulate a partial refresh.
        catalog = FakeCatalog(homes=all_live({
            "movie": home(("最近热门", [item("大明王朝", "1")]), stale=False),
            "tv": home(("最近热门", [item("庆余年", "2")]), stale=False),
        }), snapshot={"home": {}})
        return catalog, ime_hotwords.HotwordService(catalog, cache_path=temp_cache(),
                                                    fetch_json=dead_network, clock=FakeClock())

    def test_degraded_categories_stay_stale_and_retry(self):
        catalog, service = self.build()
        for gone in ("anime", "variety", "short"):
            catalog.homes.pop(gone)
        service.get()
        self.assertTrue(wait_for(lambda: service.stats()["refreshes"] >= 1))
        payload = service.get()
        # Missing categories count as degraded, so the payload stays stale and
        # keeps retrying on the short interval instead of sitting for 6h.
        self.assertTrue(payload["stale"])
        texts = [w["text"] for w in payload["words"]]
        self.assertIn("大明王朝", texts)
        self.assertIn("庆余年", texts)
        self.assertGreaterEqual([c for c, _ in catalog.calls].count("anime"), 1)

    def test_failed_category_keeps_its_previous_titles(self):
        catalog, service = self.build()
        clock = service._clock
        service.get()
        self.assertTrue(wait_for(lambda: service.stats()["refreshes"] >= 1))
        original_home = catalog.home

        def flaky(category, force_refresh=False):
            if category == "tv":
                raise OSError("tv upstream down")
            return original_home(category, force_refresh=force_refresh)

        catalog.home = flaky
        # Expire round 1, then let the partial round's short backoff expire too.
        clock.advance(ime_hotwords.CACHE_MAX_AGE + 1)
        service.get()
        self.assertTrue(wait_for(lambda: service.stats()["refreshes"] >= 2, timeout=10))
        self.assertIn("庆余年", [w["text"] for w in service.get()["words"]])

    def test_stale_home_response_is_not_treated_as_live(self):
        catalog, service = self.build()
        # A snapshot-sourced / stale answer must not be written as catalog-live,
        # because that would suppress the next real refresh for 6 hours.
        catalog.homes["movie"] = home(("最近热门", [item("旧片", "3")]), stale=True)
        catalog.homes["tv"] = home(("最近热门", [item("旧剧", "4")]), source="douban-snapshot")
        service.get()
        self.assertTrue(wait_for(lambda: service.stats()["refreshes"] >= 1))
        record = json.loads(service.cache_path.read_text(encoding="utf-8"))
        self.assertIn("movie", record["degraded"])
        self.assertIn("tv", record["degraded"])
        self.assertTrue(service.get()["stale"])

    def test_partial_degraded_uses_backoff_not_immediate_repull(self):
        catalog, service = self.build()
        for gone in ("anime", "variety", "short"):
            catalog.homes.pop(gone)
        service.get()
        self.assertTrue(wait_for(lambda: service.stats()["refreshes"] >= 1))
        # Only movie/tv are live, so this round is partial and must defer.
        record = json.loads(service.cache_path.read_text(encoding="utf-8"))
        self.assertTrue(record["degraded"])
        self.assertTrue(service.get()["stale"])
        before = len(catalog.calls)
        for _ in range(5):
            service.get()
        time.sleep(0.1)
        # Without backoff each stale GET would re-pull all five categories.
        self.assertEqual(len(catalog.calls), before)

    def test_first_round_failure_falls_back_to_snapshot_shelves(self):
        # Cold instance: one category works, the rest fail. The snapshot must
        # still supply those shelves rather than shrinking the word list.
        snapshot = {"home": {category: home(("最近热门",
                                             [item("快照_%s" % category, "s")] ))
                              for category in ime_hotwords.CATEGORIES}}
        catalog = FakeCatalog(homes=all_live(
            {"movie": home(("最近热门", [item("新片", "1")]), stale=False)},
            empty=("tv", "anime", "variety", "short")),
                              snapshot=snapshot)
        service = ime_hotwords.HotwordService(catalog, cache_path=temp_cache(),
                                             fetch_json=dead_network, clock=FakeClock())
        service.get()
        self.assertTrue(wait_for(lambda: service.stats()["refreshes"] >= 1))
        texts = [w["text"] for w in service.get()["words"]]
        self.assertIn("新片", texts)
        self.assertIn("快照_tv", texts)
        self.assertIn("快照_anime", texts)
        self.assertIn("快照_variety", texts)
        self.assertIn("快照_short", texts)


class SingleflightTest(unittest.TestCase):
    def test_concurrent_gets_coalesce_into_one_refresh(self):
        catalog = FakeCatalog(homes=all_live({"movie": home(("最近热门", [item("庆余年", "1")]),
                                                                stale=False)}),
                              snapshot={"home": {}}, delay=0.2)
        service = ime_hotwords.HotwordService(catalog, cache_path=temp_cache(),
                                             fetch_json=dead_network, clock=FakeClock())
        threads = [threading.Thread(target=service.get) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertTrue(wait_for(lambda: service.stats()["refreshes"] >= 1))
        self.assertEqual(len([call for call in catalog.calls if call[0] == "movie"]), 1)
        self.assertLessEqual(catalog.max_active, 1)

    def test_refresh_uses_force_refresh_for_exactly_the_five_categories(self):
        catalog = FakeCatalog(homes={c: home(("最近热门", [item("片_%s" % c, c)]), stale=False)
                                     for c in ime_hotwords.CATEGORIES},
                              snapshot={"home": {}})
        service = ime_hotwords.HotwordService(catalog, cache_path=temp_cache(),
                                             fetch_json=dead_network, clock=FakeClock())
        service.get()
        self.assertTrue(wait_for(lambda: service.stats()["refreshes"] >= 1))
        self.assertEqual({call[0] for call in catalog.calls}, set(ime_hotwords.CATEGORIES))
        self.assertTrue(all(call[1] is True for call in catalog.calls))


class BackoffTest(unittest.TestCase):
    def test_failure_preserves_words_and_suppresses_immediate_retry(self):
        clock = FakeClock()
        catalog = FakeCatalog(homes={"movie": home(("最近热门", [item("庆余年", "1")]),
                                                        stale=False)},
                              snapshot={"home": {}})
        service = ime_hotwords.HotwordService(catalog, cache_path=temp_cache(),
                                             fetch_json=dead_network, clock=clock)
        service.get()
        self.assertTrue(wait_for(lambda: service.stats()["refreshes"] >= 1))
        clock.advance(ime_hotwords.CACHE_MAX_AGE + 1)
        catalog.homes.clear()          # total outage: no category is usable
        payload = service.get()
        self.assertEqual(payload["words"][0]["text"], "庆余年")
        self.assertTrue(payload["stale"])
        self.assertTrue(wait_for(lambda: service.stats()["failures"] >= 1))
        before = len(catalog.calls)
        service.get()
        time.sleep(0.1)
        self.assertEqual(len(catalog.calls), before)

    def test_backoff_grows_is_capped_and_never_overflows(self):
        service = ime_hotwords.HotwordService(FakeCatalog(), cache_path=temp_cache(),
                                             fetch_json=dead_network, clock=FakeClock())
        first = service._next_retry_delay()
        second = service._next_retry_delay()
        self.assertGreater(second, first)
        for _ in range(5000):
            delay = service._next_retry_delay()
            self.assertLessEqual(delay, ime_hotwords.RETRY_MAX)
        self.assertEqual(delay, ime_hotwords.RETRY_MAX)

    def test_backoff_resets_after_a_successful_refresh(self):
        catalog = FakeCatalog(homes=all_live({"movie": home(("最近热门", [item("庆余年", "1")]),
                                                                stale=False)}),
                              snapshot={"home": {}})
        service = ime_hotwords.HotwordService(catalog, cache_path=temp_cache(),
                                             fetch_json=dead_network, clock=FakeClock())
        service._next_retry_delay()
        service._retry_not_before = 0.0
        service.get()
        self.assertTrue(wait_for(lambda: service.stats()["refreshes"] >= 1))
        self.assertEqual(service.stats()["retry_attempts"], 0)

    def test_total_outage_keeps_old_record_and_timestamp(self):
        clock = FakeClock()
        catalog = FakeCatalog(homes=all_live({"movie": home(("最近热门", [item("庆余年", "1")]),
                                                                stale=False)}),
                              snapshot={"home": {}})
        service = ime_hotwords.HotwordService(catalog, cache_path=temp_cache(),
                                             fetch_json=dead_network, clock=clock)
        service.get()
        self.assertTrue(wait_for(lambda: service.stats()["refreshes"] >= 1))
        good_record = json.loads(service.cache_path.read_text(encoding="utf-8"))
        good_time = good_record["updated_at"]
        self.assertEqual(good_record["degraded"], [])

        clock.advance(ime_hotwords.CACHE_MAX_AGE + 1)
        catalog.homes.clear()          # every category dead
        payload = service.get()
        self.assertTrue(wait_for(lambda: service.stats()["failures"] >= 1))
        # The old record survives with its own timestamp, not a refreshed one.
        after = json.loads(service.cache_path.read_text(encoding="utf-8"))
        self.assertEqual(after["updated_at"], good_time)
        self.assertEqual(after["words"][0]["text"], "庆余年")
        self.assertTrue(payload["stale"])


class PersistenceTest(unittest.TestCase):
    def test_write_is_atomic_and_reloads_across_instances(self):
        tmp = pathlib.Path(tempfile.mkdtemp())
        cache = tmp / "ime.json"
        catalog = FakeCatalog(homes=all_live({"movie": home(("最近热门", [item("大明王朝", "1")]),
                                                                stale=False)}),
                              snapshot={"home": {}})
        clock = FakeClock()
        first = ime_hotwords.HotwordService(catalog, cache_path=cache,
                                            fetch_json=dead_network, clock=clock)
        first.get()
        self.assertTrue(wait_for(lambda: first.stats()["refreshes"] >= 1))
        self.assertEqual(list(tmp.glob("*.tmp")), [])
        self.assertTrue(cache.exists())
        second = ime_hotwords.HotwordService(catalog, cache_path=cache,
                                             fetch_json=dead_network, clock=clock)
        payload = second.get()
        self.assertEqual(payload["words"][0]["text"], "大明王朝")
        self.assertFalse(payload["stale"])
        self.assertEqual(payload["source"], "catalog-live")

    def test_corrupt_cache_falls_back_to_snapshot(self):
        cache = pathlib.Path(tempfile.mkdtemp()) / "ime.json"
        cache.write_text("{not json", encoding="utf-8")
        catalog = FakeCatalog(snapshot={"home": {
            "movie": home(("最近热门", [item("庆余年", "1")]))}})
        service = ime_hotwords.HotwordService(catalog, cache_path=cache,
                                             fetch_json=dead_network, clock=FakeClock())
        payload = service.get()
        self.assertEqual(payload["words"][0]["text"], "庆余年")
        self.assertTrue(payload["stale"])

    def test_malformed_cache_metadata_never_raises(self):
        catalog = FakeCatalog(snapshot={"home": {
            "movie": home(("最近热门", [item("庆余年", "1")]))}})
        broken_records = [
            {"schema": 1, "updated_at": "not-a-number", "words": [{"text": "a"}]},
            {"schema": 1, "updated_at": None, "words": [{"text": "a", "pinyin": "a",
                                                        "initials": "a", "kind": "movie",
                                                        "weight": "heavy"}]},
            {"schema": 1, "updated_at": float("inf"), "words": []},
            {"schema": 1, "updated_at": 1.0, "words": "not-a-list"},
            {"schema": 2, "updated_at": 1.0, "words": [{"text": "a", "pinyin": "a",
                                                        "initials": "a", "kind": "movie",
                                                        "weight": 1}]},
            {"schema": 1, "updated_at": 1.0, "words": [{"text": "a", "pinyin": "a",
                                                       "initials": "a", "kind": "bogus",
                                                       "weight": 1}]},
            [1, 2, 3],
        ]
        for broken in broken_records:
            cache = pathlib.Path(tempfile.mkdtemp()) / "ime.json"
            cache.write_text(json.dumps(broken), encoding="utf-8")
            service = ime_hotwords.HotwordService(catalog, cache_path=cache,
                                                 fetch_json=dead_network, clock=FakeClock())
            payload = service.get()          # must not raise
            self.assertEqual(payload["words"][0]["text"], "庆余年")
            self.assertTrue(payload["stale"])

    def test_missing_cache_directory_is_created(self):
        cache = pathlib.Path(tempfile.mkdtemp()) / "nested" / "dir" / "ime.json"
        ime_hotwords.HotwordService(FakeCatalog(), cache_path=cache,
                                    fetch_json=dead_network, clock=FakeClock())
        self.assertTrue(cache.parent.is_dir())


class BoundsTest(unittest.TestCase):
    def test_response_is_bounded_across_all_categories(self):
        homes = {}
        for category in ime_hotwords.CATEGORIES:
            homes[category] = home(*[
                ("最近热门", [item("%s-%d-%d" % (category, s, i), "%s%d%d" % (category, s, i))
                              for i in range(20)]) for s in range(3)], stale=False)
        service = ime_hotwords.HotwordService(FakeCatalog(homes=homes, snapshot=homes),
                                             cache_path=temp_cache(),
                                             fetch_json=dead_network, clock=FakeClock())
        payload = service.get()
        self.assertLessEqual(len(payload["words"]), ime_hotwords.MAX_WORDS)
        for word in payload["words"]:
            self.assertIn(word["kind"], set(ime_hotwords.CATEGORIES) | {"general"})
            self.assertEqual(set(word), {"text", "pinyin", "initials", "kind", "weight"})

    def test_only_the_fixed_supplement_url_is_requested(self):
        seen = []

        def record(url, timeout=None):
            seen.append((url, timeout))
            return ["热搜词"]

        catalog = FakeCatalog(homes={"movie": home(("最近热门", [item("庆余年", "1")]),
                                                        stale=False)},
                              snapshot={"home": {}})
        service = ime_hotwords.HotwordService(catalog, cache_path=temp_cache(),
                                             fetch_json=record, clock=FakeClock())
        service.get()
        self.assertTrue(wait_for(lambda: service.stats()["refreshes"] >= 1))
        self.assertTrue(seen)
        for url, timeout in seen:
            self.assertEqual(url, ime_hotwords.SUPPLEMENT_URL)
            self.assertIsNotNone(timeout)

    def test_oversized_upstream_payload_is_bounded(self):
        service = ime_hotwords.HotwordService(
            FakeCatalog(homes={"movie": home(("最近热门", [item("庆余年", "1")]), stale=False)},
                        snapshot={"home": {}}),
            cache_path=temp_cache(),
            fetch_json=lambda url, timeout=None: ["热搜%d" % i for i in range(200000)],
            clock=FakeClock())
        service.get()
        self.assertTrue(wait_for(lambda: service.stats()["refreshes"] >= 1))
        payload = service.get()
        self.assertEqual(payload["words"][0]["text"], "庆余年")
        self.assertLessEqual(len(payload["words"]), ime_hotwords.MAX_WORDS)
        self.assertLessEqual(len([w for w in payload["words"] if w["kind"] == "general"]),
                             ime_hotwords.MAX_GENERAL_WORDS)


if __name__ == "__main__":
    unittest.main()
