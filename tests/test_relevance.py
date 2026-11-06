"""回归测试：搜索到的无关视频不能再被当成用户要学的梗。"""
import importlib.util
import json
import pathlib
import sys
import unittest
from unittest.mock import MagicMock, patch

from maibot_sdk.context import PluginContext

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('bilibili_meme', ROOT / '__init__.py',
                                              submodule_search_locations=[str(ROOT)])
package = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = package
spec.loader.exec_module(package)

from bilibili_meme.extract import is_relevant, score_candidate
from bilibili_meme.plugin import BilibiliMemeConfig, BilibiliMemePlugin


class RelevanceTests(unittest.TestCase):
    def test_term_must_appear_in_title_or_desc(self):
        self.assertTrue(is_relevant({'title': '吃了不烧心是什么梗【梗指南】'}, '吃了不烧心'))
        self.assertTrue(is_relevant({'title': '随便的标题', 'desc': '这里讲了 不烧心 的来历'}, '不烧心'))
        self.assertFalse(is_relevant({'title': '倪海厦-人纪-黄帝内经-合集（字幕版）'}, '回头是岸中正兄'))
        self.assertFalse(is_relevant({'title': '结过婚我也认是什么梗【梗指南】'}, '吃了不烧心'))

    def test_short_terms_are_not_dropped(self):
        self.assertTrue(is_relevant({'title': '任意视频'}, 'a'))
        self.assertTrue(is_relevant({'title': '任意视频'}, ''))

    def test_irrelevant_candidate_scores_negative_infinity(self):
        self.assertEqual(score_candidate({'title': '黄帝内经合集', 'danmaku': 99999, 'view': 10 ** 8},
                                         [], '吃了不烧心'), float('-inf'))
        self.assertGreater(score_candidate({'title': '吃了不烧心是什么梗【梗指南】', 'danmaku': 100,
                                            'view': 10 ** 5}, [], '吃了不烧心'), 0)


class LearnGuardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.calls = []
        self.rows = []

        async def rpc(method, plugin_id, payload, **kwargs):
            self.calls.append(payload)
            if payload['capability'] == 'database.query':
                return {'success': True, 'result': list(self.rows)}
            return {'success': True}

        self.plugin = BilibiliMemePlugin()
        self.plugin._set_context(PluginContext('test', rpc_call=rpc))
        self.plugin.set_plugin_config({'plugin': {'config_version': '1.0.0'}})

    def _client(self, videos):
        client = MagicMock()
        client.search_videos.return_value = videos
        client.video_info.side_effect = lambda bv: {
            'bvid': bv, 'cid': 1, 'aid': 2, 'owner': 'up',
            'title': videos[0]['title'], 'desc': '', 'view': 10 ** 7,
            'duration': 60, 'danmaku_total': 1000,
        }
        client.comments.return_value = []
        client.tags.return_value = []
        client.danmaku.return_value = []
        client.subtitle.return_value = {'ok': False, 'lines': [], 'lang': ''}
        return client

    async def test_all_candidates_irrelevant_is_refused_before_llm(self):
        client = self._client([{'bvid': 'BV1aaaaaaaaa', 'title': '倪海厦-人纪-黄帝内经-合集（字幕版）'}])
        with patch('bilibili_meme.plugin.BiliClient', return_value=client):
            text = await self.plugin._learn('回头是岸中正兄', BilibiliMemeConfig())
        self.assertIn('没有真正讲', text)
        self.assertFalse([c for c in self.calls if c['capability'] == 'llm.generate'])
        self.assertFalse([c for c in self.calls if c['args'].get('query_type') in ('create', 'update')])

    async def test_model_saying_matches_query_false_is_refused(self):
        client = self._client([{'bvid': 'BV1bbbbbbbbb', 'title': '吃了不烧心是什么梗【梗指南】'}])

        async def rpc(method, plugin_id, payload, **kwargs):
            self.calls.append(payload)
            if payload['capability'] == 'llm.generate':
                return {'success': True, 'response': json.dumps({'is_meme': True, 'matches_query': False,
                                                                 'content': '别的梗', 'confidence': 0.9})}
            return {'success': True, 'result': []}

        self.plugin._set_context(PluginContext('test', rpc_call=rpc))
        with patch('bilibili_meme.plugin.BiliClient', return_value=client):
            text = await self.plugin._learn('吃了不烧心', BilibiliMemeConfig())
        self.assertIn('对不上', text)
        self.assertFalse([c for c in self.calls if c['args'].get('query_type') in ('create', 'update')])

    async def test_known_term_returns_cache_without_learning(self):
        self.rows = [{'id': 1, 'content': '吃了不烧心', 'meaning': '旧释义',
                      'evidence_messages': '[[{"platform": "bilibili", "message_id": "BV1"}]],'}]
        with patch('bilibili_meme.plugin.BiliClient') as factory:
            text = await self.plugin._learn('吃了不烧心', BilibiliMemeConfig())
        self.assertIn('已经学过', text)
        self.assertIn('旧释义', text)
        factory.assert_not_called()


if __name__ == '__main__':
    unittest.main()
