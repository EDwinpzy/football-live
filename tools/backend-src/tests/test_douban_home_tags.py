import pathlib
import sys
import unittest
import urllib.parse
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import douban_catalog as d


class HomeTagsTest(unittest.TestCase):
    def test_movie_home_uses_three_homepage_tags_in_upstream_order(self):
        calls = []
        def fetch(url):
            calls.append(url)
            return {'subjects': [{'id': '2', 'title': 'second'}, {'id': '1', 'title': 'first'}]}
        c = d.DoubanCatalog(fetch=fetch)
        for sort, tag in [('U', '热门'), ('R', '最新'), ('S', '豆瓣高分')]:
            self.assertEqual([x['douban_id'] for x in c._home_list('movie', sort)], ['2', '1'])
            url = urllib.parse.urlparse(calls[-1]); q = urllib.parse.parse_qs(url.query)
            self.assertEqual(url.path, '/j/search_subjects')
            self.assertEqual(q['type'], ['movie']); self.assertEqual(q['tag'], [tag])
            self.assertEqual(q['sort'], ['recommend'])

    def test_tv_home_has_recommend_time_and_rank(self):
        calls = []
        c = d.DoubanCatalog(fetch=lambda u: calls.append(u) or {'subjects': []})
        for sort, order in [('U', 'recommend'), ('R', 'time'), ('S', 'rank')]:
            c._home_list('tv', sort, page=2)
            q = urllib.parse.parse_qs(urllib.parse.urlparse(calls[-1]).query)
            self.assertEqual(q['type'], ['tv']); self.assertEqual(q['tag'], ['热门'])
            self.assertEqual(q['sort'], [order]); self.assertEqual(q['page_start'], ['20'])

    def test_anime_home_queries_mainland_animation_and_uses_deeper_pages(self):
        calls = []
        c = d.DoubanCatalog(fetch=lambda u: calls.append(u) or {'data': []})
        c._home_list('anime', 'R', page=4)
        q = urllib.parse.parse_qs(urllib.parse.urlparse(calls[-1]).query)
        self.assertEqual(q['genres'], ['动画'])
        self.assertEqual(q['countries'], ['中国大陆'])
        self.assertEqual(q['start'], ['60'])
        self.assertGreaterEqual(d.CN3D_MAX_HOME_PAGES, 8)

    def test_cn3d_requires_domestic_animation(self):
        base = {'title': '凡人修仙传 年番4', 'regions': ['中国大陆'], 'genres': ['动画']}
        self.assertTrue(d.is_cn3d_anime(base))
        self.assertFalse(d.is_cn3d_anime(dict(base, regions=['日本'])))
        self.assertFalse(d.is_cn3d_anime(dict(base, genres=['剧情'])))
        self.assertFalse(d.is_cn3d_anime(dict(base, title='伍六七')))
        self.assertFalse(d.is_cn3d_anime(dict(base, title='未知国漫')))

    def test_cn3d_series_catalog_covers_major_franchises(self):
        self.assertGreaterEqual(len(d.CN3D_SERIES), 75)
        base = {'regions': ['中国大陆'], 'genres': ['动画']}
        for title in ('秦时明月之龙腾万里', '天行九歌', '元尊', '镜·双城',
                      '哪吒之魔童闹海', '超神学院之雄兵连'):
            with self.subTest(title=title):
                self.assertTrue(d.is_cn3d_anime(dict(base, title=title)))

    def test_home_anime_scans_past_a_sequel_heavy_first_page(self):
        c = d.DoubanCatalog(fetch=lambda _url: {})
        first_page = [{'id': str(i), 'title': '凡人修仙传 年番%d' % i,
                       'regions': ['中国大陆'], 'genres': ['动画']}
                      for i in range(20)]
        other_series = [name for name in d.CN3D_SERIES if name != '凡人修仙传'][:20]
        second_page = [{'id': 's%d' % i, 'title': name + ' 第1季',
                        'regions': ['中国大陆'], 'genres': ['动画']}
                       for i, name in enumerate(other_series)]
        page_calls = []

        def home_list(_category, _sort, page=1, strict=False):
            page_calls.append(page)
            return first_page if page == 1 else second_page if page == 2 else []

        with patch.object(c, '_home_list', side_effect=home_list), \
                patch.object(c, '_enrich', side_effect=lambda rows, *_args: rows):
            items = c._section_items('anime', 'U')

        self.assertEqual(page_calls, [1, 2])
        self.assertEqual(len(items), 20)
        self.assertEqual(len({d.cn3d_series_key(item) for item in items}), 20)

    def test_sparse_home_sections_fill_from_the_full_cn3d_snapshot_index(self):
        titles = d.CN3D_SERIES[:20]
        sections = [{'title': title, 'items': [
            {'douban_id': 'seed-0', 'category': 'anime', 'title': titles[0],
             'regions': ['中国大陆'], 'genres': ['动画']}]} for title in d.HOME_SECTION_NAMES]
        snapshot = {
            'home': {'anime': {'sections': sections}},
            'index': [{'douban_id': 'seed-%d' % i, 'category': 'anime', 'title': name + ' 第1季',
                       'regions': ['中国大陆'], 'genres': ['动画'], 'year': str(2026 - i % 5),
                       'rating': 9 - i / 10, 'rating_count': 1000 - i}
                      for i, name in enumerate(titles)],
        }
        c = d.DoubanCatalog(fetch=lambda _url: {'data': []}, snapshot=snapshot)

        with patch.object(d.time, 'sleep'):
            result = c.home('anime')

        self.assertTrue(result['stale'])
        self.assertEqual(result['source'], 'douban-snapshot')
        self.assertEqual([len(section['items']) for section in result['sections']], [20, 20, 20])
        for section in result['sections']:
            self.assertEqual(len({d.cn3d_series_key(item) for item in section['items']}), 20)

    def test_anime_show_scans_and_paginates_filtered_douban_results(self):
        c = d.DoubanCatalog(fetch=lambda _url: {})
        rows_by_page = {
            1: [{'douban_id': 'old%d' % i, 'title': '非3D动画%d' % i,
                 'regions': ['中国大陆'], 'genres': ['动画']} for i in range(20)],
            2: [{'douban_id': 'new%d' % i, 'title': name + ' 第1季',
                 'regions': ['中国大陆'], 'genres': ['动画']}
                for i, name in enumerate(d.CN3D_SERIES[:20])],
        }
        calls = []

        def explore(_category, _sort, page, genre, region, _year, _rating, send=()):
            calls.append((page, genre, region, set(send)))
            return rows_by_page.get(page, [])

        with patch.object(c, '_explore', side_effect=explore):
            result = c.show('anime', page=1)

        self.assertEqual([call[0] for call in calls], [1, 2])
        self.assertTrue(all(call[1:3] == ('动画', '中国大陆') for call in calls))
        self.assertTrue(all({'genre', 'region'}.issubset(call[3]) for call in calls))
        self.assertEqual(len(result['items']), 20)
        self.assertEqual(len({d.cn3d_series_key(item) for item in result['items']}), 20)

    def test_explicit_style_can_admit_a_new_series(self):
        item = {'title': '新作品', 'regions': ['中国大陆'], 'genres': ['动画'], 'tags': ['3D动画']}
        self.assertTrue(d.is_cn3d_anime(item))
        self.assertFalse(d.is_cn3d_anime(dict(item, tags=['2D动画'])))

    def test_snapshot_cannot_leak_japanese_or_2d_animation(self):
        value = {'sections': [{'title': '最近热门', 'items': [
            {'title': '凡人修仙传', 'regions': ['中国大陆'], 'genres': ['动画']},
            {'title': '药屋少女', 'regions': ['日本'], 'genres': ['动画']},
            {'title': '伍六七', 'regions': ['中国大陆'], 'genres': ['动画']}]}]}
        out = d._apply_section_policy('anime', value)
        self.assertEqual([x['title'] for x in out['sections'][0]['items']], ['凡人修仙传'])

    def test_home_cache_generation_invalidates_old_explore_cache(self):
        self.assertEqual(d.HOME_CACHE_PREFIX, 'home:cn3d-v2:')


if __name__ == '__main__': unittest.main()
