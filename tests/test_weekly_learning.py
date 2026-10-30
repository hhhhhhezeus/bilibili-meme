import importlib.util
from pathlib import Path
import sys
import unittest
from unittest.mock import MagicMock, AsyncMock
from maibot_sdk.context import PluginContext

ROOT = Path(__file__).resolve().parents[1]
if 'bilibili_meme' not in sys.modules:
    spec=importlib.util.spec_from_file_location('bilibili_meme', ROOT/'__init__.py', submodule_search_locations=[str(ROOT)])
    package=importlib.util.module_from_spec(spec);sys.modules[spec.name]=package;spec.loader.exec_module(package)
from bilibili_meme.plugin import BilibiliMemePlugin, BilibiliMemeConfig

class WeeklyLearningTests(unittest.IsolatedAsyncioTestCase):
    async def test_multiple_memes_and_sensitive_filter(self):
        async def rpc(method, plugin_id, payload, **kwargs):
            self.assertEqual(payload['capability'], 'llm.generate')
            return {'success': True, 'response': '{"memes":[{"content":"梗A","meaning":"解释A","confidence":0.9,"taboo":{"level":"none"}},{"content":"梗B","meaning":"解释B","confidence":0.8,"taboo":{"level":"mild"}},{"content":"敏感","meaning":"解释","confidence":0.9,"taboo":{"level":"strong"}}]}'}
        plugin=BilibiliMemePlugin();plugin._set_context(PluginContext('test', rpc_call=rpc))
        plugin._upsert_jargon=AsyncMock(return_value='（已写入黑话库）')
        client=MagicMock();client.video_info.return_value={'cid':1,'aid':2,'title':'断网补全计划150'}
        client.subtitle.return_value={'lines':[(1,'梗A'),(2,'梗B')]};client.comments.return_value=[]
        await plugin._weekly_learn_video(client, {'bvid':'BV1234567890'}, BilibiliMemeConfig().plugin)
        self.assertEqual([c.args[0]['content'] for c in plugin._upsert_jargon.await_args_list], ['梗A','梗B'])

    async def test_missing_material_and_write_failure_retry(self):
        async def rpc(method, plugin_id, payload, **kwargs):
            return {'success': True, 'response':'{"memes":[{"content":"梗","meaning":"含义","confidence":0.9,"taboo":{"level":"none"}}]}'}
        plugin=BilibiliMemePlugin();plugin._set_context(PluginContext('test', rpc_call=rpc));plugin._upsert_jargon=AsyncMock(return_value='（⚠️ 写库失败）')
        client=MagicMock();client.video_info.return_value={'cid':1,'aid':2,'title':'x'};client.comments.return_value=[];client.subtitle.return_value={'lines':[]}
        with self.assertRaisesRegex(ValueError, '材料不足'):
            await plugin._weekly_learn_video(client, {'bvid':'BV1234567890'}, BilibiliMemeConfig().plugin)
        client.subtitle.return_value={'lines':[(1,'内容')]}
        with self.assertRaisesRegex(ValueError, '写入梗失败'):
            await plugin._weekly_learn_video(client, {'bvid':'BV1234567890'}, BilibiliMemeConfig().plugin)

    async def test_enabled_background_job_processes_both_sources_once(self):
        import tempfile
        from datetime import datetime
        from zoneinfo import ZoneInfo
        from unittest.mock import patch
        from maibot_sdk.context import PluginPaths
        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime(2026, 11, 2, 10, tzinfo=ZoneInfo('Asia/Shanghai'))
        with tempfile.TemporaryDirectory() as d:
            plugin=BilibiliMemePlugin()
            plugin._set_context(PluginContext('test', paths=PluginPaths(data_dir=Path(d), runtime_dir=Path(d)/'runtime')))
            plugin.set_plugin_config({'plugin': {'config_version':'1.0.0', 'weekly_enabled':True}})
            plugin._weekly_learn_video=AsyncMock()
            client=MagicMock()
            def discover(uid, since):
                return [{'bvid': 'BV1234567890' if uid == 646732586 else 'BV1234567891',
                         'title': '断网补全计划150' if uid == 646732586 else '梗指南新视频',
                         'author_uid':uid, 'published_at':since+60}]
            client.recent_uploads.side_effect=discover
            async def stop_after_run(_):
                raise __import__('asyncio').CancelledError()
            with patch('bilibili_meme.plugin.datetime', Clock), patch('bilibili_meme.plugin.BiliClient', return_value=client), patch('bilibili_meme.plugin.asyncio.sleep', stop_after_run):
                await plugin.on_load()
                await __import__('asyncio').gather(plugin._weekly_task, return_exceptions=True)
                self.assertEqual(plugin._weekly_learn_video.await_count, 2)
                self.assertEqual(plugin._active_requests, 0)
                self.assertIn('last_completed_end', plugin._weekly_runner._load())
                await plugin.on_unload()
