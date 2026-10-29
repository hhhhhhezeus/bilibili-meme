"""B站学梗插件 —— 从「梗指南」类视频自动学习网络梗，写入麦麦的黑话库。

命令
    /学梗 <关键词或BV号>   搜索/解析视频 → LLM 结构化 → 写 jargons 表
    /梗库 [名称]           查看已学梗（无参数=概览，有参数=详情）

能力依赖（见 _manifest.json）
    llm.generate     本地 GLM 抽取
    database.query   Jargon 表读写
    send.text        回包
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from typing import Any

from maibot_sdk import Command, Field, MaiBotPlugin, PluginConfigBase, Tool

from .bili import _BV_RE, BiliClient, BiliError
from .extract import (
    build_prompt,
    build_video_summary_prompt,
    parse_json,
    pick_comments,
    score_candidate,
)

logger = logging.getLogger("bilibili_meme")

class BilibiliMemePluginSection(PluginConfigBase):
    """[plugin] 段：插件配置（WebUI 可改）。"""

    config_version: str = Field(
        default="1.0.0",
        description="配置结构版本，升级插件时用于迁移，一般不要改。",
        json_schema_extra={"disabled": True, "hidden": True},
    )
    enabled: bool = Field(
        default=True,
        description="总开关。关闭后命令不响应。",
        json_schema_extra={"x-widget": "switch"},
    )
    write_to_db: bool = Field(
        default=True,
        description="学习结果是否写入麦麦的黑话库。关闭则只回复预览，不污染库。",
        json_schema_extra={"x-widget": "switch"},
    )
    reject_taboo_strong: bool = Field(
        default=True,
        description="拒绝入库敏感度 strong 的梗（地域黑/地狱笑话等），只回复提示。",
        json_schema_extra={"x-widget": "switch"},
    )
    max_candidates: int = Field(default=5, description="同名词搜到多个视频时，最多比较几个候选。")
    comment_pages: int = Field(default=2, description="每个视频抓几页热门评论（每页 20 条）。")
    danmaku_segments: int = Field(default=2, description="抓几个弹幕分片（每片约 6 分钟）。")
    rate_limit_seconds: float = Field(default=1.2, description="B站请求间隔秒数，太小会被风控。")
    sessdata: str = Field(
        default="",
        description="B站 SESSDATA（建议用小号）。有它才能拿字幕；留空则视频理解只能靠弹幕/评论。",
    )
    llm_task: str = Field(default="utils", description="用哪个模型任务做抽取（utils/learner 都行）。")
    min_confidence: float = Field(default=0.35, description="低于该置信度的梗不入库。")


class BilibiliMemeConfig(PluginConfigBase):
    """插件配置根。宿主约定必须有 plugin 段（含 config_version）。"""

    plugin: BilibiliMemePluginSection = Field(default_factory=BilibiliMemePluginSection)


class BilibiliMemePlugin(MaiBotPlugin):
    """B站学梗。"""

    @classmethod
    def get_config_model(cls) -> type[PluginConfigBase] | None:
        return BilibiliMemeConfig

    # ---------------- 生命周期 ----------------

    async def on_load(self) -> None:
        logger.info("B站学梗插件已加载")

    async def on_unload(self) -> None:
        logger.info("B站学梗插件已卸载")

    async def on_config_update(
        self, scope: str, config_data: dict[str, Any], version: str
    ) -> None:
        """配置热重载：配置对象由基类自动刷新，这里只记日志。"""
        logger.info("B站学梗配置已更新: scope=%s version=%s", scope, version)

    # ---------------- 命令 ----------------

    @Command("学梗", description="从B站视频学习一个梗", pattern=r"^/学梗\s*(?P<term>.*)$")
    async def handle_learn(self, term: str = "", **kwargs: Any) -> Any:
        cfg: BilibiliMemeConfig = self.config  # type: ignore[assignment]
        if not cfg.plugin.enabled:
            return None
        logger.debug("学梗 kwargs keys=%s", sorted(kwargs.keys()))

        raw = self._arg(kwargs, "term", fallback=term)
        if not raw:
            return await self._finish(kwargs, "用法：/学梗 大肥鱼   或   /学梗 BV11kn86mEjg")

        await self._finish(kwargs, f"🔍 正在学「{raw}」…（要抓 B站 数据 + 本地模型整理，稍等十几秒）")
        try:
            text = await asyncio.wait_for(self._learn(raw, cfg), timeout=180)
        except asyncio.TimeoutError:
            text = "❌ 超时了（180 秒）。可能是 B站 接口慢或模型排队，稍后再试。"
        except BiliError as exc:
            text = f"❌ B站 接口出错：{exc}"
        except Exception as exc:  # noqa: BLE001 - 命令层兜底，避免插件崩
            logger.exception("学梗失败")
            text = f"❌ 出错了：{type(exc).__name__}: {exc}"
        return await self._finish(kwargs, text)

    @Command("梗库", description="查看已学的梗", pattern=r"^/梗库\s*(?P<term>.*)$")
    async def handle_library(self, term: str = "", **kwargs: Any) -> Any:
        cfg: BilibiliMemeConfig = self.config  # type: ignore[assignment]
        if not cfg.plugin.enabled:
            return None
        name = self._arg(kwargs, "term", fallback=term)
        try:
            text = await self._library(name, cfg)
        except Exception as exc:  # noqa: BLE001
            logger.exception("梗库查询失败")
            text = f"❌ 查库出错：{type(exc).__name__}: {exc}"
        return await self._finish(kwargs, text)

    # ---------------- 核心流程 ----------------

    async def _learn(self, raw: str, cfg: BilibiliMemeConfig) -> str:
        client = BiliClient(rate_limit=cfg.plugin.rate_limit_seconds, sessdata=self._sessdata(cfg.plugin))

        # 1) 定位视频
        m = _BV_RE.search(raw)
        if m:
            bvid = m.group(1)
            video = await asyncio.to_thread(client.video_info, bvid)
            cands = [
                {
                    "bvid": bvid,
                    "title": video["title"],
                    "danmaku": video["danmaku_total"],
                    "view": video["view"],
                }
            ]
            term_hint = raw.replace(bvid, "").strip() or video["title"]
        else:
            term_hint = raw
            # 多查询合并：长查询经常 0 命中，短查询更稳，去重后再打分
            seen: dict[str, dict[str, Any]] = {}
            for query in (f"{raw}是什么梗", f"{raw} 梗指南", raw):
                try:
                    for item in await asyncio.to_thread(
                        client.search_videos, query, cfg.plugin.max_candidates
                    ):
                        if item.get("bvid"):
                            seen.setdefault(item["bvid"], item)
                except BiliError as exc:
                    logger.debug("查询 %r 失败(继续): %s", query, exc)
                if len(seen) >= cfg.plugin.max_candidates * 2:
                    break
            if not seen:
                return f"❌ 没搜到「{raw}」相关的视频。换个说法，或直接给 BV 号。"
            cands = list(seen.values())

        # 2) 选源：拉每个候选的评论打分；再看有没有字幕（有字幕=能拿到"来历"，加权）
        best: tuple[float, dict[str, Any], list[dict[str, Any]], dict[str, Any], dict[str, Any]] | None = None
        scored: list[tuple[float, dict[str, Any], list[dict[str, Any]], dict[str, Any]]] = []
        for cand in cands:
            try:
                info = await asyncio.to_thread(client.video_info, cand["bvid"])
                cs = await asyncio.to_thread(client.comments, info["aid"], cfg.plugin.comment_pages)
            except BiliError as exc:
                logger.warning("候选 %s 跳过: %s", cand.get("bvid"), exc)
                continue
            cand["view"] = info["view"]
            cand["danmaku"] = cand.get("danmaku") or info["danmaku_total"]
            scored.append((score_candidate(cand, cs), cand, cs, info))

        # 只对前 3 名探测字幕，避免请求过多
        scored.sort(key=lambda x: x[0], reverse=True)
        for score, cand, cs, info in scored[:3]:
            try:
                sub = await asyncio.to_thread(client.subtitle, info["bvid"], info["cid"])
            except BiliError:
                sub = {"ok": False, "lines": [], "lang": ""}
            bonus = 3.0 if sub.get("ok") and len(sub.get("lines") or []) >= 20 else 0.0
            if bonus:
                logger.info("候选 %s 有字幕(%d条)，选源加权 +%.1f", info["bvid"], len(sub.get("lines") or []), bonus)
            if best is None or (score + bonus) > best[0]:
                best = (score + bonus, cand, cs, info, sub)

        # 其余候选不探测字幕，直接参与比较（sub 为空）
        for score, cand, cs, info in scored[3:]:
            if best is None or score > best[0]:
                best = (score, cand, cs, info, {"ok": False, "lines": [], "lang": ""})

        if best is None:
            return "❌ 所有候选视频都取不到数据（可能被风控）。稍后再试。"

        score, cand, cs, info, sub = best

        # 3) 补充信号：标签 + 弹幕时间轴
        tags = await asyncio.to_thread(client.tags, info["bvid"])
        elems = await asyncio.to_thread(client.danmaku, info["cid"], cfg.plugin.danmaku_segments)
        bursts = BiliClient.bursts(elems)

        # 4) LLM 抽取
        prompt = build_prompt(
            term_hint=term_hint,
            video=info,
            tags=tags,
            bursts=bursts,
            comments=pick_comments(cs),
            subtitle=sub,
        )
        payload = await self.ctx.call_capability(
            "llm.generate",
            **{"prompt": prompt, "task_name": cfg.plugin.llm_task, "max_tokens": 1200, "temperature": 0.3},
        )
        if not (payload or {}).get("success"):
            return f"❌ 模型抽取失败：{(payload or {}).get('error') or '未知错误'}"
        data = parse_json(str(payload.get("response") or ""))
        if not data:
            return "❌ 模型输出不是合法 JSON，这次没学成（可重试一次）。"
        if not data.get("is_meme"):
            return f"❌ 判断这不是一个可用的梗（或视频讲的是别的东西）。视频：{info['title'][:40]}"

        # 5) 入库判定
        taboo = data.get("taboo") or {}
        level = str(taboo.get("level") or "none").lower()
        conf = float(data.get("confidence") or 0)
        content = str(data.get("content") or "").strip()

        if not content:
            return "❌ 模型没给出梗名，这次没学成。"
        if cfg.plugin.reject_taboo_strong and level == "strong":
            return (
                f"🚫 「{content}」被判定为高敏感梗，拒绝入库。\n"
                f"原因：{taboo.get('reason') or '未说明'}\n"
                f"（涉及地域/灾难/人身攻击类的梗，麦麦不该在群聊里用）"
            )
        if conf < cfg.plugin.min_confidence:
            return (
                f"⚠️ 「{content}」置信度只有 {conf:.2f}（低于 {cfg.plugin.min_confidence}），"
                f"材料不足以说清含义/用法，未入库。\n视频：{info['title'][:40]}"
            )

        record = {
            "content": content,
            "meaning": self._meaning_text(data),
            "evidence_messages": json.dumps(
                [[{"platform": "bilibili", "message_id": info["bvid"]}]], ensure_ascii=False
            ),
            "session_id_dict": "{}",
            "count": 0,
            "is_jargon": True,
            "is_complete": True,
            "is_global": True,
            "last_inference_count": 0,
            "created_by": "AI",
        }

        saved = "（未写入，仅预览）"
        if cfg.plugin.write_to_db:
            saved = await self._upsert_jargon(record)

        head = "✅ 学到了" if cfg.plugin.write_to_db else "👀 预览（未入库）"
        lines = [
            f"{head}：**{content}**",
            f"含义：{record['meaning'][:200]}",
        ]
        usage = str(data.get("usage") or "").strip()
        if usage:
            lines.append(f"用法：{usage[:160]}")
        exs = [str(e).strip() for e in (data.get("examples") or []) if str(e).strip()]
        if exs:
            lines.append("例句：" + " ｜ ".join(exs[:3])[:200])
        aliases = [str(a).strip() for a in (data.get("aliases") or []) if str(a).strip()]
        if aliases:
            lines.append("别名：" + "、".join(aliases[:5]))
        lines.append(
            f"来源：{info['title'][:34]}（{info['owner'][:12]}）｜"
            f"选源分 {score} ｜ 置信 {conf:.2f} ｜ 时效 {data.get('freshness', '?')}"
        )
        if level != "none":
            lines.append(f"⚠️ 敏感度 {level}：{taboo.get('reason') or '未说明'}")
        lines.append(saved)
        return "\n".join(lines)

    async def _library(self, name: str, cfg: BilibiliMemeConfig) -> str:
        if name:
            res = await self.ctx.call_capability(
                "database.query",
                **{
                    "model_name": "Jargon",
                    "query_type": "get",
                    "filters": {"content": name},
                    "limit": 3,
                },
            )
            rows = self._rows_of(res)
            if not rows:
                return f"库里没有「{name}」。可以用 /学梗 {name} 学一个。"
            lines = [f"📚 「{name}」共 {len(rows)} 条记录"]
            for r in rows:
                lines.append(
                    f"• {r.get('content')}｜用过 {r.get('count', 0)} 次｜"
                    f"{'全局' if r.get('is_global') else '局部'}\n  {str(r.get('meaning') or '')[:160]}"
                )
            return "\n".join(lines)

        res = await self.ctx.call_capability(
            "database.query", **{"model_name": "Jargon", "query_type": "get", "limit": 200}
        )
        rows = self._rows_of(res)
        mine = [r for r in rows if str(r.get("evidence_messages") or "").find('"bilibili"') >= 0]
        if not mine:
            return "梗库还是空的。用 /学梗 <关键词> 学第一个。"
        ordered = sorted(mine, key=lambda r: r.get("id") or 0, reverse=True)
        top_used = sorted(mine, key=lambda r: r.get("count") or 0, reverse=True)[:5]
        lines = [f"📚 已学会 {len(mine)} 个 B站 梗（库里共 {len(rows)} 条黑话）", "", "最近学的："]
        for r in ordered[:5]:
            lines.append(f"• {r.get('content')}（用过 {r.get('count', 0)} 次）")
        if any((r.get("count") or 0) > 0 for r in top_used):
            lines.append("")
            lines.append("最常用的：")
            for r in top_used:
                if (r.get("count") or 0) > 0:
                    lines.append(f"• {r.get('content')} × {r.get('count')}")
        return "\n".join(lines)

    # ---------------- 辅助 ----------------

    @staticmethod
    def _meaning_text(data: dict[str, Any]) -> str:
        """把含义 + 用法 + 时效揉进 meaning 字段（jargons 只有一个自由文本位）。"""
        parts = [str(data.get("meaning") or "").strip()]
        if origin := str(data.get("origin") or "").strip():
            parts.append(f"来历：{origin}")
        if usage := str(data.get("usage") or "").strip():
            parts.append(f"用法：{usage}")
        if exs := [str(e).strip() for e in (data.get("examples") or []) if str(e).strip()]:
            parts.append("例句：" + " / ".join(exs[:3]))
        if aliases := [str(a).strip() for a in (data.get("aliases") or []) if str(a).strip()]:
            parts.append("别名：" + "、".join(aliases[:5]))
        if fresh := str(data.get("freshness") or "").strip():
            parts.append(f"时效：{fresh}")
        return " ｜ ".join(p for p in parts if p)[:1500]

    async def _upsert_jargon(self, record: dict[str, Any]) -> str:
        """同名梗做更新，避免库里堆重复。"""
        try:
            res = await self.ctx.call_capability(
                "database.query",
                **{
                    "model_name": "Jargon",
                    "query_type": "get",
                    "filters": {"content": record["content"]},
                    "limit": 1,
                },
            )
            self._check_db_result(res)
            rows = self._rows_of(res)
            if rows:
                existing = rows[0]
                if existing.get("created_by") != "AI" or '"bilibili"' not in str(existing.get("evidence_messages", "")):
                    return "（⚠️ 存在非本插件创建的同名黑话，已保留原记录，未覆盖）"
                old_id = rows[0].get("id")
                if old_id is None:
                    raise ValueError("已有记录缺少 ID，未执行更新")
                result = await self.ctx.call_capability(
                    "database.query",
                    **{
                        "model_name": "Jargon",
                        "query_type": "update",
                        "filters": {"id": old_id},
                        "data": {key: record[key] for key in ("meaning", "evidence_messages")},
                    },
                )
                self._check_db_result(result)
                if result == 0:
                    raise ValueError("数据库未更新任何记录")
                return "（库里已有同名梗，已更新释义并保留使用统计）"
            result = await self.ctx.call_capability(
                "database.query", **{"model_name": "Jargon", "query_type": "create", "data": record}
            )
            self._check_db_result(result)
            return "（已写入黑话库，下次聊天命中会自然用上）"
        except Exception as exc:  # noqa: BLE001
            logger.exception("写库失败")
            return f"（⚠️ 写库失败：{type(exc).__name__}: {exc}）"

    @staticmethod
    def _check_db_result(result):
        if result is False or (isinstance(result, dict) and result.get("success") is False):
            raise ValueError("数据库操作失败，请检查宿主日志")

    @staticmethod
    def _rows_of(res: Any) -> list[dict[str, Any]]:
        """兼容能力层的各种返回形态。"""
        if isinstance(res, list):
            return [r for r in res if isinstance(r, dict)]
        if not isinstance(res, dict):
            return []
        for key in ("data", "result", "rows", "items"):
            val = res.get(key)
            if isinstance(val, list):
                return [r for r in val if isinstance(r, dict)]
            if isinstance(val, dict):
                for k2 in ("data", "rows", "items"):
                    v2 = val.get(k2)
                    if isinstance(v2, list):
                        return [r for r in v2 if isinstance(r, dict)]
                return [val]
        return []

    @staticmethod
    def _text_of(kwargs: dict[str, Any]) -> str:
        for key in ("text", "message", "content", "raw_text", "plain_text"):
            val = kwargs.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
        return ""

    @Command("总结视频", description="总结一个B站视频讲了什么", pattern=r"^/总结视频\s*(?P<term>.*)$")
    async def handle_summary(self, term: str = "", **kwargs: Any) -> Any:
        cfg: BilibiliMemeConfig = self.config  # type: ignore[assignment]
        if not cfg.plugin.enabled:
            return None
        raw = self._arg(kwargs, "term", fallback=term)
        if not raw:
            return await self._finish(kwargs, "用法：/总结视频 BV号   或   /总结视频 https://b23.tv/xxxx")
        await self._finish(kwargs, f"📺 正在看「{raw[:40]}」…（要下载字幕/弹幕 + 本地模型总结）")
        try:
            text = await asyncio.wait_for(self._summarize(raw, cfg.plugin), timeout=240)
        except asyncio.TimeoutError:
            text = "❌ 超时了（240 秒）。视频太长或接口慢，稍后再试。"
        except BiliError as exc:
            text = f"❌ B站 接口出错：{exc}"
        except Exception as exc:  # noqa: BLE001
            logger.exception("总结视频失败")
            text = f"❌ 出错了：{type(exc).__name__}: {exc}"
        return await self._finish(kwargs, text)

    @Tool(
        "bili_video_summary",
        description="读取并总结一个 B站 视频的内容。群友分享了 B站 链接、BV号或 b23.tv 短链时可以调用。",
        parameters={
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "B站视频链接、b23.tv 短链或 BV 号",
                }
            },
            "required": ["url"],
        },
    )
    async def tool_video_summary(self, url: str = "", **kwargs: Any) -> str:
        cfg: BilibiliMemeConfig = self.config  # type: ignore[assignment]
        if not cfg.plugin.enabled:
            return "插件已禁用"
        target = url or str(kwargs.get("url") or "")
        if not target:
            return "缺少 url 参数"
        try:
            return await asyncio.wait_for(self._summarize(target, cfg.plugin), timeout=240)
        except asyncio.TimeoutError:
            return "总结超时（240 秒）"
        except Exception as exc:  # noqa: BLE001
            logger.exception("tool 总结视频失败")
            return f"总结失败：{type(exc).__name__}: {exc}"

    async def _summarize(self, raw: str, conf: Any) -> str:
        """视频理解主流程：定位视频 → 字幕(优先) → 弹幕时间轴 → 评论 → LLM 总结。"""
        client = BiliClient(rate_limit=conf.rate_limit_seconds, sessdata=self._sessdata(conf))

        bvid = ""
        m = _BV_RE.search(raw)
        if m:
            bvid = m.group(1)
        else:
            bvid = await asyncio.to_thread(client.expand_short_link, raw)
        if not bvid:
            found = await asyncio.to_thread(client.search_videos, raw, 3)
            if not found:
                return "❌ 没找到这个视频。给我 BV 号或直接贴链接。"
            bvid = found[0]["bvid"]

        info = await asyncio.to_thread(client.video_info, bvid)
        sub = await asyncio.to_thread(client.subtitle, bvid, info["cid"])
        segs = max(conf.danmaku_segments, BiliClient.segments_for(info["duration"]))
        elems = await asyncio.to_thread(client.danmaku, info["cid"], segs)
        bursts = BiliClient.bursts(elems)
        cs = await asyncio.to_thread(client.comments, info["aid"], conf.comment_pages)

        prompt = build_video_summary_prompt(
            video=info, subtitle=sub, bursts=bursts, comments=pick_comments(cs)
        )
        payload = await self.ctx.call_capability(
            "llm.generate",
            **{"prompt": prompt, "task_name": conf.llm_task, "max_tokens": 1200, "temperature": 0.3},
        )
        if not (payload or {}).get("success"):
            return f"❌ 模型总结失败：{(payload or {}).get('error') or '未知错误'}"
        data = parse_json(str(payload.get("response") or ""))
        if not data:
            return "❌ 模型输出不是合法 JSON，这次没总结成。"

        out = [f"📺 {info['title'][:50]}", str(data.get("one_line") or "").strip()]
        points = data.get("key_points") or []
        if points:
            out.append("")
            for i, pt in enumerate(points[:5], 1):
                t = str((pt or {}).get("time") or "").strip()
                txt = str((pt or {}).get("point") or "").strip()
                out.append(f"{i}. {('[' + t + '] ') if t else ''}{txt}")
        meta = []
        if aud := str(data.get("audience") or "").strip():
            meta.append(f"适合：{aud}")
        if url_verdict := str(data.get("verdict") or "").strip():
            meta.append(f"评价：{url_verdict}")
        if meta:
            out.append("")
            out.append(" ｜ ".join(meta))
        try:
            rel = float(data.get("reliability") or 0)
        except (TypeError, ValueError):
            rel = 0.0
        src = f"字幕：{sub.get('lang') or '无'}" if sub.get("ok") else (
            "字幕：需登录 cookie" if sub.get("need_login") else "字幕：该视频无字幕"
        )
        out.append(f"（{src}｜弹幕 {len(elems)} 条｜可信度 {rel:.2f}）")
        if note := str(data.get("note") or "").strip():
            out.append(f"⚠️ {note}")
        return "\n".join(x for x in out if x is not None)

    @staticmethod
    def _arg(kwargs: dict[str, Any], name: str, fallback: str = "") -> str:
        """取命令参数：优先命名捕获组，其次从完整消息文本里剥掉命令前缀。"""
        groups = kwargs.get("matched_groups")
        if isinstance(groups, dict):
            val = groups.get(name)
            if isinstance(val, str) and val.strip():
                return val.strip()
        if fallback and fallback.strip():
            return fallback.strip()
        text = str(kwargs.get("text") or "").strip()
        return re.sub(r"^/\S+\s*", "", text).strip()

    @staticmethod
    def _sessdata(conf: Any) -> str:
        """SESSDATA 优先级：环境变量 BILI_SESSDATA > config.toml。

        环境变量不会被"覆盖插件目录"这种操作冲掉，适合长期部署。
        """
        return (os.environ.get("BILI_SESSDATA") or getattr(conf, "sessdata", "") or "").strip()

    async def _finish(self, kwargs: dict[str, Any], text: str) -> tuple[bool, str, bool]:
        """发送结果并决定是否拦截主链。

        宿主不会自动发送命令返回值（只记日志），所以必须自己 send.text。
        返回值三元组 (success, 文本, 是否拦截)：拦截=True 可避免麦麦再当普通聊天回一遍。
        """
        stream_id = str(kwargs.get("stream_id") or "")
        if not stream_id:
            return True, text, False
        try:
            res = await self.ctx.call_capability(
                "send.text",
                **{"text": text, "stream_id": stream_id, "sync_to_maisaka_history": False},
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("send.text 异常: %s", exc)
            return True, text, False
        if res is False or (isinstance(res, dict) and res.get("success") is False):
            logger.warning("send.text 失败: %s", res.get("error") if isinstance(res, dict) else res)
            return True, text, False
        return True, text, True


def create_plugin() -> BilibiliMemePlugin:
    """插件工厂（宿主通过它实例化）。"""
    return BilibiliMemePlugin()
