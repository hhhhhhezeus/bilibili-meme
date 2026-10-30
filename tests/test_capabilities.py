import ast
import json
from unittest.mock import patch, MagicMock
import importlib.util
import pathlib
import sys
import unittest

from maibot_sdk.context import PluginContext

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('bilibili_meme', ROOT / '__init__.py', submodule_search_locations=[str(ROOT)])
package = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = package
spec.loader.exec_module(package)
from bilibili_meme.plugin import BilibiliMemePlugin, BilibiliMemeConfig

class CapabilityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.calls = []
        async def rpc(method, plugin_id, payload, **kwargs):
            self.calls.append((method, payload, kwargs))
            if payload['capability'] == 'database.query':
                return {'data': []}
            return {'success': True}
        self.plugin = BilibiliMemePlugin()
        self.plugin._set_context(PluginContext('test', rpc_call=rpc))

    async def test_database_and_send_reach_sdk_as_named_arguments(self):
        await self.plugin._library('test', BilibiliMemeConfig())
        await self.plugin._upsert_jargon({'content': 'test'})
        result = await self.plugin._finish({'stream_id': 'group'}, 'hello')
        self.assertTrue(result[2])
        self.assertEqual([call[1]['capability'] for call in self.calls],
                         ['database.query', 'database.query', 'database.query', 'send.text'])
        self.assertEqual(self.calls[0][1]['args']['filters'], {'content': 'test'})
        self.assertEqual(self.calls[-1][1]['args']['text'], 'hello')
        self.assertTrue(all(not extra for _, _, extra in self.calls))

    async def test_sdk_unwraps_database_rows_and_failed_send(self):
        async def rpc(method, plugin_id, payload, **kwargs):
            if payload['capability'] == 'database.query':
                return {'success': True, 'result': [{'id': 7, 'content': 'test', 'meaning': '解释'}]}
            return {'success': False, 'error': 'offline'}
        self.plugin._set_context(PluginContext('test', rpc_call=rpc))
        self.assertIn('解释', await self.plugin._library('test', BilibiliMemeConfig()))
        self.assertFalse((await self.plugin._finish({'stream_id': 'group'}, 'hello'))[2])

    async def test_learn_and_summary_with_real_sdk(self):
        calls = []
        existing = False
        async def rpc(method, plugin_id, payload, **kwargs):
            calls.append(payload)
            self.assertFalse(kwargs)
            args = payload['args']
            if payload['capability'] == 'llm.generate':
                self.assertIn('prompt', args)
                self.assertEqual(args['task_name'], 'utils')
                return {'success': True, 'response': json.dumps({
                    'is_meme': True, 'content': '测试梗', 'confidence': 0.9,
                    'meaning': '测试解释', 'one_line': '测试总结', 'reliability': 0.9})}
            if args['query_type'] == 'get':
                return {'success': True, 'result': [{'id': 42, 'created_by': 'AI', 'evidence_messages': '"bilibili"', 'count': 8}] if existing else []}
            return {'success': True, 'result': 1}
        self.plugin._set_context(PluginContext('test', rpc_call=rpc))
        client = MagicMock()
        client.video_info.return_value = {'bvid': 'BV11kn86mEjg', 'cid': 1, 'aid': 2,
            'title': '测试视频', 'owner': '作者', 'view': 100, 'duration': 30, 'danmaku_total': 0}
        client.comments.return_value = []
        client.subtitle.return_value = {'ok': True, 'lines': [(0, '测试字幕')], 'lang': 'zh'}
        client.tags.return_value = []
        client.danmaku.return_value = []
        with patch('bilibili_meme.plugin.BiliClient', return_value=client) as factory:
            factory.bursts.return_value = []
            factory.segments_for.return_value = 1
            cfg = BilibiliMemeConfig()
            self.assertIn('已写入', await self.plugin._learn('BV11kn86mEjg', cfg))
            existing = True
            self.assertIn('已更新', await self.plugin._learn('BV11kn86mEjg', cfg))
            self.assertIn('测试总结', await self.plugin._summarize('BV11kn86mEjg', cfg.plugin))
        queries = [c['args'] for c in calls if c['capability'] == 'database.query']
        self.assertEqual([q['query_type'] for q in queries], ['get', 'create', 'get', 'update'])
        self.assertEqual(queries[-1]['filters'], {'id': 42})
        self.assertEqual(set(queries[-1]['data']), {'meaning', 'evidence_messages'})

    async def test_manual_jargon_and_failed_query_are_not_overwritten(self):
        for response in ({'success': True, 'result': [{'id': 7, 'created_by': 'USER'}]},
                         {'success': False, 'error': 'database unavailable'}):
            calls = []
            async def rpc(method, plugin_id, payload, **kwargs):
                calls.append(payload)
                return response
            self.plugin._set_context(PluginContext('test', rpc_call=rpc))
            result = await self.plugin._upsert_jargon({'content': 'test'})
            self.assertEqual(len(calls), 1)
            self.assertNotIn('已写入', result)
            self.assertNotIn('已更新', result)

    async def test_create_and_update_failure_are_reported(self):
        for existing in (False, True):
            async def rpc(method, plugin_id, payload, **kwargs):
                if payload['args']['query_type'] == 'get':
                    rows = [{'id': 7, 'created_by': 'AI', 'evidence_messages': '"bilibili"'}] if existing else []
                    return {'success': True, 'result': rows}
                return {'success': False, 'error': 'write denied', 'result': None}
            self.plugin._set_context(PluginContext('test', rpc_call=rpc))
            result = await self.plugin._upsert_jargon({'content': 'test', 'meaning': 'x', 'evidence_messages': '"bilibili"'})
            self.assertIn('write denied', result)
            self.assertNotIn('已写入', result)
            self.assertNotIn('已更新', result)

    async def test_none_create_and_library_failure(self):
        async def rpc(method, plugin_id, payload, **kwargs):
            if payload['args']['query_type'] == 'get':
                return {'success': True, 'result': []}
            return {'success': True, 'result': None}
        self.plugin._set_context(PluginContext('test', rpc_call=rpc))
        result = await self.plugin._upsert_jargon({'content': 'test'})
        self.assertIn('未确认', result)
        async def failed(method, plugin_id, payload, **kwargs):
            return {'success': False, 'result': None, 'error': 'offline'}
        self.plugin._set_context(PluginContext('test', rpc_call=failed))
        with self.assertRaisesRegex(ValueError, 'offline'):
            await self.plugin._library('test', BilibiliMemeConfig())

    def test_component_timeouts(self):
        components = {x['name']: x for x in self.plugin.get_components()}
        for name, expected in [('学梗', 200000), ('总结视频', 260000), ('bili_video_summary', 260000)]:
            self.assertEqual(components[name]['metadata']['timeout_ms'], expected)

    def test_all_capability_calls_use_one_positional_argument(self):
        tree = ast.parse((ROOT / 'plugin.py').read_text())
        calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Attribute) and n.func.attr == 'call_capability']
        self.assertEqual(len(calls), 3)
        self.assertTrue(all(len(call.args) == 1 for call in calls))

if __name__ == '__main__':
    unittest.main()
