"""海龟汤插件的网页管理接口。

只提供 GET / POST：AstrBot 的插件页面通过 iframe 里的 postMessage 桥接调用后端，
而那座桥只实现了 api:get 和 api:post，PUT/DELETE 到不了这里。

汤底默认不随列表下发，必须单独请求 story/answer 才会返回——管理员在网页上
翻题库时不应该被剧透。
"""

from __future__ import annotations

import asyncio
from typing import Any

from astrbot.api import logger
from astrbot.api.web import error_response, json_response, request

PLUGIN_NAME = "astrbot_plugin_soupai"

SOURCE_LABELS = {
    "network": "网络题库",
    "local": "本地存储库",
    "custom": "自定义题库",
}

# 网络题库是跟着仓库走的静态资源，在网页上增删会污染 git 工作区，
# 也会在同步上游时冲突。这里只允许屏蔽，不允许改文件。
READONLY_SOURCES = {"network"}


def _ok(data: Any = None):
    return json_response({"status": "ok", "data": data})


class SoupaiWebApi:
    """把题库管理能力暴露给 AstrBot 面板。"""

    def __init__(self, plugin):
        self.plugin = plugin
        self.annotation_task: asyncio.Task | None = None
        self.annotation_job = {
            "status": "idle",
            "total": 0,
            "processed": 0,
            "succeeded": 0,
            "failed": 0,
            "skipped": 0,
            "errors": [],
        }

    # ------------------------------------------------------------ 注册

    def register(self) -> None:
        routes = [
            ("/overview", self.overview, ["GET"], "题库与会话总览"),
            ("/stories", self.stories, ["GET"], "题目列表（不含汤底）"),
            ("/story/answer", self.story_answer, ["GET"], "查看单题汤底"),
            ("/story/create", self.story_create, ["POST"], "新增题目"),
            ("/story/update", self.story_update, ["POST"], "修改题目"),
            ("/story/delete", self.story_delete, ["POST"], "删除题目"),
            ("/story/hide", self.story_hide, ["POST"], "屏蔽或恢复题目"),
            ("/story/generate", self.story_generate, ["POST"], "让 LLM 生成新题"),
            (
                "/story/annotation",
                self.story_annotation,
                ["GET"],
                "查看单题标注（含汤底信息）",
            ),
            ("/story/annotate", self.story_annotate, ["POST"], "标注单题"),
            (
                "/annotation/preview",
                self.annotation_preview,
                ["GET"],
                "预览批量标注范围",
            ),
            ("/annotation/start", self.annotation_start, ["POST"], "启动批量标注"),
            ("/annotation/status", self.annotation_status, ["GET"], "批量标注进度"),
            ("/annotation/cancel", self.annotation_cancel, ["POST"], "停止批量标注"),
            ("/usage/mark", self.usage_mark, ["POST"], "标记单题在某会话的出题状态"),
            ("/usage/reset", self.usage_reset, ["POST"], "重置使用记录"),
            ("/games", self.games, ["GET"], "进行中的对局"),
            ("/games/end", self.games_end, ["POST"], "强制结束某局"),
            ("/history", self.history, ["GET"], "结束对局的存档（不含汤底）"),
            (
                "/history/detail",
                self.history_detail,
                ["GET"],
                "单局复盘（含汤底）",
            ),
            ("/history/clear", self.history_clear, ["POST"], "清理对局存档"),
            ("/config", self.config_get, ["GET"], "插件配置（含服务商下拉项）"),
            ("/config/save", self.config_save, ["POST"], "保存插件配置"),
        ]
        for route, handler, methods, desc in routes:
            self.plugin.context.register_web_api(
                f"/{PLUGIN_NAME}{route}", handler, methods, desc
            )
        logger.info(f"海龟汤网页管理接口已注册 {len(routes)} 个路由")

    # ------------------------------------------------------------ 工具

    def _storage(self, source: str):
        self.plugin._ensure_story_storages()
        return {
            "network": self.plugin.online_story_storage,
            "local": self.plugin.local_story_storage,
            "custom": self.plugin.custom_story_storage,
        }.get(source)

    async def _payload(self) -> dict:
        data = await request.json(default={})
        return data if isinstance(data, dict) else {}

    def _resolve(self, source: str, story_id: str):
        """定位一道题，返回 (storage, index, story)。"""
        storage = self._storage(source)
        if storage is None:
            return None, -1, None
        index = storage.find_index(str(story_id))
        if index < 0:
            return storage, -1, None
        return storage, index, storage.stories[index]

    # ------------------------------------------------------------ 读接口

    async def overview(self):
        """三个题库的概况，以及出过题的会话清单。"""
        self.plugin._ensure_story_storages()
        session = request.query.get("session") or None

        sources = []
        all_sessions: set[str] = set()
        for key in ("network", "local", "custom"):
            storage = self._storage(key)
            info = storage.get_storage_info(session)
            all_sessions.update(storage.sessions())
            sources.append(
                {
                    "source": key,
                    "label": SOURCE_LABELS[key],
                    "readonly": key in READONLY_SOURCES,
                    "total": info["total"],
                    "hidden": info.get("hidden", 0),
                    "used": info["used"],
                    "used_all_sessions": storage.get_usage_info()["used"],
                }
            )

        # 对局以 group_id 为键，使用记录以 unified_msg_origin 为键，
        # 靠开局时写进对局数据的 session 字段对上
        playing = {
            game.get("session")
            for game in self.plugin.game_state.active_games.values()
            if game.get("session")
        }
        # 正在玩但还没有出题记录的会话（刚重置过，或这局是 LLM 现场生成的）
        # 也要列出来，否则会话页会漏掉它
        all_sessions.update(playing)

        sessions = []
        for umo in sorted(all_sessions):
            used = sum(
                len(self._storage(k).used_ids(umo))
                for k in ("network", "local", "custom")
            )
            sessions.append(
                {
                    "session": umo,
                    "label": self._session_label(umo),
                    "used": used,
                    "playing": umo in playing,
                }
            )

        return _ok(
            {
                "sources": sources,
                "sessions": sessions,
                "session": session,
                "active_games": len(self.plugin.game_state.active_games),
                "judge_engine": self.plugin.judge_engine,
                "generating": self.plugin.auto_generating,
            }
        )

    def _session_label(self, umo: str) -> str:
        """把 unified_msg_origin 显示成人话。"""
        if umo == "__default__":
            return "后台任务"
        parts = umo.split(":")
        if len(parts) >= 3:
            platform, msg_type, sid = parts[0], parts[1], ":".join(parts[2:])
            kind = "私聊" if "Friend" in msg_type or "Private" in msg_type else "群"
            return f"{platform} {kind} {sid}"
        return umo

    async def stories(self):
        """分页列出题目。默认只给汤面，汤底要单独请求。"""
        source = request.query.get("source") or "network"
        storage = self._storage(source)
        if storage is None:
            return error_response(f"未知题库: {source}")

        keyword = (request.query.get("q") or "").strip()
        session = request.query.get("session") or None
        # 搜索汤底会在列表页泄底，所以默认只搜汤面；要搜汤底得显式打开
        search_answer = (request.query.get("search_answer") or "") == "1"
        try:
            page = max(int(request.query.get("page") or 1), 1)
            page_size = min(max(int(request.query.get("page_size") or 20), 1), 100)
        except ValueError:
            return error_response("分页参数不合法")

        used = storage.used_ids(session) if session else set()
        items = []
        for index, story in enumerate(storage.stories):
            puzzle = story.get("puzzle", "")
            answer = story.get("answer", "")
            if keyword:
                haystack = puzzle + (answer if search_answer else "")
                if keyword.lower() not in haystack.lower():
                    continue
            sid = storage.story_id(story, index)
            items.append(
                {
                    "id": sid,
                    "index": index,
                    "puzzle": puzzle,
                    "answer_preview_len": len(answer),
                    "hidden": sid in storage.hidden_ids,
                    "used": sid in used,
                    "created_at": story.get("created_at"),
                    "annotation_status": self.plugin.story_catalog.annotation_status(
                        source, sid, puzzle, answer
                    ),
                }
            )

        total = len(items)
        start = (page - 1) * page_size
        return _ok(
            {
                "source": source,
                "label": SOURCE_LABELS[source],
                "readonly": source in READONLY_SOURCES,
                "total": total,
                "page": page,
                "page_size": page_size,
                "items": items[start : start + page_size],
            }
        )

    async def story_answer(self):
        """单独取一道题的汤底。前端必须二次确认后才调这里。"""
        source = request.query.get("source") or ""
        story_id = request.query.get("id") or ""
        _, _, story = self._resolve(source, story_id)
        if story is None:
            return error_response("题目不存在")
        logger.info(f"网页端查看汤底: {source}/{story_id} by {request.username}")
        return _ok({"id": story_id, "answer": story.get("answer", "")})

    async def games(self):
        """进行中的对局。汤底同样不下发。"""
        games = []
        for key, game in self.plugin.game_state.active_games.items():
            umo = game.get("session") or key
            games.append(
                {
                    # key 是对局的内部键（群号），结束对局要用它
                    "key": key,
                    "session": umo,
                    "label": self._session_label(umo),
                    "started_at": game.get("started_at"),
                    "puzzle": game.get("puzzle", ""),
                    "difficulty": game.get("difficulty", "普通"),
                    "question_count": game.get("question_count", 0),
                    "question_limit": game.get("question_limit"),
                    "hint_count": game.get("hint_count", 0),
                    "hint_limit": game.get("hint_limit"),
                    "verification_attempts": game.get("verification_attempts", 0),
                    "pass_score": self.plugin._pass_score_for(game),
                    "best_score": int(game.get("best_score") or 0),
                    "passed": bool(game.get("passed")),
                    "qa_history": game.get("qa_history", []),
                }
            )
        return _ok(
            {"games": games, "verification_limit": self.plugin.verification_limit}
        )

    # ------------------------------------------------------------ 写接口

    async def story_create(self):
        data = await self._payload()
        source = data.get("source") or "custom"
        puzzle = (data.get("puzzle") or "").strip()
        answer = (data.get("answer") or "").strip()
        if not puzzle or not answer:
            return error_response("汤面和汤底都不能为空")
        if source in READONLY_SOURCES:
            return error_response(f"{SOURCE_LABELS[source]}是只读的，不能新增")
        storage = self._storage(source)
        if storage is None:
            return error_response(f"未知题库: {source}")
        try:
            saved = await self.plugin.save_story_checked(source, puzzle, answer)
        except ValueError as exc:
            return error_response(str(exc))
        logger.info(f"网页端新增题目到 {source} by {request.username}")
        return _ok(
            {
                "total": len(storage.stories),
                "id": saved["id"],
                "warnings": saved["check"].get("warnings", []),
            }
        )

    async def story_update(self):
        data = await self._payload()
        source = data.get("source") or ""
        if source in READONLY_SOURCES:
            return error_response(f"{SOURCE_LABELS[source]}是只读的，不能修改")
        storage, index, story = self._resolve(source, data.get("id") or "")
        if story is None:
            return error_response("题目不存在")
        puzzle = (data.get("puzzle") or "").strip()
        answer = (data.get("answer") or "").strip()
        if not puzzle or not answer:
            return error_response("汤面和汤底都不能为空")
        try:
            saved = await self.plugin.save_story_checked(
                source, puzzle, answer, story_id=str(data.get("id"))
            )
        except ValueError as exc:
            return error_response(str(exc))
        logger.info(f"网页端修改题目 {source}/{data.get('id')} by {request.username}")
        return _ok(
            {"id": str(data.get("id")), "warnings": saved["check"].get("warnings", [])}
        )

    async def story_delete(self):
        data = await self._payload()
        source = data.get("source") or ""
        if source in READONLY_SOURCES:
            return error_response(
                f"{SOURCE_LABELS[source]}是只读的。想让某道题不再出现，请用屏蔽。"
            )
        storage, index, story = self._resolve(source, data.get("id") or "")
        if story is None:
            return error_response("题目不存在")
        story_id = storage.story_id(story, index)
        warnings = []
        async with self.plugin._story_write_lock:
            with storage.lock:
                index = storage.find_index(story_id)
                if index < 0:
                    return error_response("题目不存在")
                removed = storage.stories.pop(index)
                try:
                    storage.save_stories()
                except ValueError as exc:
                    storage.stories.insert(index, removed)
                    return error_response(str(exc))
                for ids in storage.usage.values():
                    ids.discard(story_id)
                storage.hidden_ids.discard(story_id)
                storage.save_usage_record()
                storage.save_hidden_record()
            try:
                self.plugin.story_catalog.forget(source, story_id)
            except ValueError as exc:
                logger.warning(f"Story deleted but annotation cleanup failed: {exc}")
                warnings.append(
                    "题目已删除，但旧标注缓存清理失败；它不会用于其他题目。"
                )
        logger.info(f"网页端删除题目 {source}/{story_id} by {request.username}")
        return _ok({"total": len(storage.stories), "warnings": warnings})

    async def story_hide(self):
        data = await self._payload()
        storage, index, story = self._resolve(
            data.get("source") or "", data.get("id") or ""
        )
        if story is None:
            return error_response("题目不存在")
        hidden = bool(data.get("hidden", True))
        storage.set_hidden(storage.story_id(story, index), hidden)
        return _ok({"hidden": hidden})

    async def story_generate(self):
        """让 LLM 现场生成题目，写进本地存储库。"""
        data = await self._payload()
        try:
            count = min(max(int(data.get("count") or 1), 1), 5)
        except (TypeError, ValueError):
            return error_response("数量不合法")
        theme, ideas = data.get("theme"), data.get("ideas")
        if (theme is not None and (not isinstance(theme, str) or len(theme) > 120)) or (
            ideas is not None and (not isinstance(ideas, str) or len(ideas) > 2000)
        ):
            return error_response("题材最多 120 字，创作参考最多 2000 字")

        self.plugin._ensure_story_storages()
        created, failed = [], []
        for _ in range(count):
            try:
                story = await self.plugin.generate_and_store_story(theme, ideas)
            except ValueError as exc:
                failed.append(str(exc))
                continue
            created.append(
                {
                    "id": story["id"],
                    "puzzle": story["puzzle"],
                    "check_method": story["check"].get("method"),
                    "warnings": story["check"].get("warnings", []),
                }
            )
        logger.info(
            f"网页端生成题目: 成功 {len(created)} 失败 {len(failed)} by {request.username}"
        )
        if not created:
            return error_response(
                failed[0] if failed else "生成失败，请检查 LLM 配置",
                data={"created": [], "failed": failed},
            )
        return _ok({"created": created, "failed": failed})

    async def story_annotation(self):
        """Return spoiler-bearing metadata only on an explicit detail request."""
        source, sid = request.query.get("source") or "", request.query.get("id") or ""
        _, _, story = self._resolve(source, sid)
        if story is None:
            return error_response("题目不存在")
        catalog = self.plugin.story_catalog
        return _ok(
            {
                "status": catalog.annotation_status(
                    source, sid, story["puzzle"], story["answer"]
                ),
                "annotation": catalog.get_annotation(
                    source, sid, story["puzzle"], story["answer"]
                ),
            }
        )

    async def story_annotate(self):
        """Annotate one explicitly selected story without editing its bank."""
        data = await self._payload()
        source, sid = str(data.get("source") or ""), str(data.get("id") or "")
        if not isinstance(data.get("force", False), bool):
            return error_response("force 必须为布尔值")
        try:
            annotation = await self.plugin.story_catalog.annotate(
                source, sid, force=data.get("force", False)
            )
        except ValueError as exc:
            return error_response(str(exc))
        return _ok({"status": "ready", "annotation": annotation})

    async def annotation_preview(self):
        """Count the selected stories without invoking any model."""
        source = request.query.get("source") or "all"
        if source not in ("all", "network", "local", "custom"):
            return error_response("未知题库")
        force = request.query.get("force") == "1"
        entries = [
            e
            for e in self.plugin.story_catalog.entries()
            if source == "all" or e["source"] == source
        ]
        ready = sum(
            self.plugin.story_catalog.annotation_status(
                e["source"], e["id"], e["puzzle"], e["answer"]
            )
            == "ready"
            for e in entries
        )
        return _ok(
            {
                "total": len(entries),
                "ready": ready,
                "pending": len(entries) if force else len(entries) - ready,
            }
        )

    async def annotation_start(self):
        """Start a bounded background annotation job after an explicit request."""
        if self.annotation_task is not None and not self.annotation_task.done():
            return error_response("已有标注任务正在运行，请先等待或取消")
        data = await self._payload()
        if self.annotation_task is not None and not self.annotation_task.done():
            return error_response("已有标注任务正在运行，请先等待或取消")
        source, force = data.get("source") or "all", data.get("force", False)
        if source not in ("all", "network", "local", "custom") or not isinstance(
            force, bool
        ):
            return error_response("标注范围或 force 参数不合法")
        provider_id = next(
            (
                value
                for key in (
                    "annotation_llm_provider",
                    "verify_llm_provider",
                    "judge_llm_provider",
                )
                if (value := str(self.plugin.config.get(key) or "").strip())
            ),
            "",
        )
        provider = self.plugin._resolve_provider(provider_id)
        if provider is None or not callable(getattr(provider, "text_chat", None)):
            return error_response("请先配置可用的题库标注或验证模型")
        entries = [
            e
            for e in self.plugin.story_catalog.entries()
            if source == "all" or e["source"] == source
        ]
        pending = [
            e
            for e in entries
            if force
            or self.plugin.story_catalog.annotation_status(
                e["source"], e["id"], e["puzzle"], e["answer"]
            )
            != "ready"
        ]
        skipped = len(entries) - len(pending)
        self.annotation_job = {
            "status": "running" if pending else "completed",
            "total": len(entries),
            "processed": skipped,
            "succeeded": 0,
            "failed": 0,
            "skipped": skipped,
            "errors": [],
        }
        if pending:
            self.annotation_task = asyncio.create_task(
                self._run_annotations(pending, force)
            )
        return _ok({"job": self.annotation_job})

    async def _run_annotations(self, entries: list[dict], force: bool) -> None:
        """Process one annotation at a time and retain completed work on cancel.

        Args:
            entries: Snapshot of selected stories.
            force: Whether to refresh existing annotations.
        """
        job = self.annotation_job
        try:
            for entry in entries:
                try:
                    await self.plugin.story_catalog.annotate(
                        entry["source"], entry["id"], force=force
                    )
                    job["succeeded"] += 1
                except Exception as exc:
                    logger.warning(f"Story annotation failed: {exc}")
                    job["failed"] += 1
                    if len(job["errors"]) < 20:
                        job["errors"].append(
                            {
                                "source": entry["source"],
                                "id": entry["id"],
                                "message": str(exc)
                                if isinstance(exc, ValueError)
                                else "标注失败，请查看日志",
                            }
                        )
                job["processed"] += 1
            job["status"] = "completed"
        except asyncio.CancelledError:
            job["status"] = "cancelled"
            raise
        except Exception:
            job["status"] = "failed"
            logger.exception("Annotation job failed")

    async def annotation_status(self):
        """Return job progress without annotation content."""
        return _ok({"job": self.annotation_job})

    async def annotation_cancel(self):
        """Cancel only the active job, preserving already saved annotations."""
        task, job = self.annotation_task, self.annotation_job
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            job["status"] = "cancelled"
        return _ok({"job": self.annotation_job})

    async def usage_mark(self):
        """把某道题在某个会话标成已出或未出。"""
        data = await self._payload()
        storage, index, story = self._resolve(
            data.get("source") or "", data.get("id") or ""
        )
        if story is None:
            return error_response("题目不存在")
        session = (data.get("session") or "").strip()
        if not session:
            return error_response("需要指定会话")
        story_id = storage.story_id(story, index)
        if data.get("used", True):
            storage.mark_used(session, story_id)
        else:
            storage.unmark_used(session, story_id)
        return _ok({"id": story_id, "used": bool(data.get("used", True))})

    async def usage_reset(self):
        """重置使用记录。不给 source 就三个库一起，不给 session 就所有会话。"""
        data = await self._payload()
        source = data.get("source")
        session = data.get("session") or None
        targets = [source] if source else ["network", "local", "custom"]
        for key in targets:
            storage = self._storage(key)
            if storage is None:
                return error_response(f"未知题库: {key}")
            storage.reset_usage(session)
        logger.info(
            f"网页端重置使用记录 source={source or '全部'} session={session or '全部'} "
            f"by {request.username}"
        )
        return _ok({"source": source, "session": session})

    async def games_end(self):
        data = await self._payload()
        key = (data.get("key") or "").strip()
        if not key:
            return error_response("需要指定对局")
        game = self.plugin.game_state.get_game(key)
        ended = self.plugin.game_state.end_game(key, "web_end")
        task = game.get("_session_task") if game else None
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        if ended:
            logger.info(f"网页端强制结束对局 {key} by {request.username}")
        return _ok({"ended": ended})

    async def history(self):
        """结束对局的存档列表。和题库列表一样，这里不带汤底。"""
        archive = getattr(self.plugin, "game_archive", None)
        if archive is None:
            return _ok({"total": 0, "games": [], "sessions": [], "offset": 0})
        session = (request.query.get("session") or "").strip()
        try:
            limit = min(max(int(request.query.get("limit") or 20), 1), 100)
            offset = max(int(request.query.get("offset") or 0), 0)
        except ValueError:
            return error_response("分页参数不合法")
        payload = archive.page(session=session, limit=limit, offset=offset)
        payload["sessions"] = [
            {**item, "label": self._session_label(item["session"])}
            for item in archive.sessions()
        ]
        for game in payload["games"]:
            game["label"] = self._session_label(game.get("session") or "")
        return _ok(payload)

    async def history_detail(self):
        """整局复盘，含汤底：这一局已经结束，对照真相才看得懂判定。"""
        archive = getattr(self.plugin, "game_archive", None)
        entry_id = (request.query.get("id") or "").strip()
        if archive is None or not entry_id:
            return error_response("需要指定存档 id")
        entry = archive.detail(entry_id)
        if entry is None:
            return error_response("找不到这局存档，可能已被新的对局挤出")
        entry["label"] = self._session_label(entry.get("session") or "")
        return _ok(entry)

    async def history_clear(self):
        """清空存档，或只清某个会话。"""
        archive = getattr(self.plugin, "game_archive", None)
        if archive is None:
            return error_response("存档不可用")
        data = await self._payload()
        session = (data.get("session") or "").strip()
        removed = archive.clear(session)
        logger.info(
            f"网页端清理对局存档 session={session or 'all'} 共 {removed} 局 "
            f"by {request.username}"
        )
        return _ok({"removed": removed})

    # ------------------------------------------------------------ 配置

    # 数值配置项的合法区间，超出则拒绝保存（面板上的 schema 有同样的 hint）
    _INT_RANGES = {
        "game_timeout": (30, 86400),
        "storage_max_size": (5, 500),
        "auto_generate_min": (0, 500),
        "verification_limit": (0, 100),
        "verification_pass_score": (0, 100),
        # 和 main.py 的 _config_number 上下界保持一致：那边越界会静默回落到
        # 默认值，在这里拦住才能告诉用户填错了
        "reward_pool": (1, 100000),
        "reward_daily_cap": (0, 1000000),
        "reward_min_questions": (0, 1000),
    }
    _FLOAT_RANGES = {
        "jev_judge_min_confidence": (0, 1),
        "reward_hint_penalty": (0, 100),
        "reward_verify_penalty": (0, 100),
        "reward_verify_weight": (0, 100),
    }

    def _providers_payload(self, kind: str = "chat") -> list[dict]:
        """List configured providers for one kind of settings dropdown.

        Args:
            kind: Chat, embedding, or rerank provider category.

        Returns:
            Provider IDs, model labels, and enabled states without credentials.
        """
        providers = []
        try:
            if kind == "embedding":
                instances = self.plugin.context.get_all_embedding_providers()
            elif kind == "rerank":
                instances = getattr(
                    self.plugin.context.provider_manager, "rerank_provider_insts", []
                )
            else:
                instances = self.plugin.context.get_all_providers()
            for provider in instances:
                meta = provider.meta()
                providers.append(
                    {
                        "id": meta.id,
                        "model": meta.model,
                        "enable": getattr(provider, "enable", True) is not False,
                    }
                )
        except Exception as e:
            logger.warning(f"获取 LLM 服务商列表失败: {e}")
        return providers

    async def config_get(self):
        """下发当前配置值 + 配置 schema + 服务商列表。

        schema 原样下发（options/labels/condition/secret 等元数据），前端
        和面板的 ConfigItemRenderer 用同一套规则渲染，新增配置项不用改前端。
        jev_api_key 是密文：永远下发空串，前端空输入框表示「不修改」。
        """
        config = self.plugin.config
        values: dict[str, Any] = {}
        for key in config.schema or {}:
            value = config.get(key)
            if key == "jev_api_key":
                value = ""
            values[key] = value
        return _ok(
            {
                "schema": config.schema or {},
                "values": values,
                "providers": self._providers_payload(),
                "embedding_providers": self._providers_payload("embedding"),
                "rerank_providers": self._providers_payload("rerank"),
                # 密文不回显，但前端需要知道有没有设过，好显示占位提示
                "has_jev_api_key": bool(str(config.get("jev_api_key") or "").strip()),
            }
        )

    async def config_save(self):
        """网页端保存配置。

        只合并合法的键；jev_api_key 传空串表示「未修改」而跳过（它本身
        允许为空=禁用 Jev，所以用单独的 clear_jev_api_key 字段显式清空）。
        保存后调 plugin._load_config() 让新值立即生效——config.save_config
        只写文件不重建插件实例，而这里不走面板那套热重载。
        """
        data = await self._payload()
        config = self.plugin.config
        schema = config.schema or {}

        # 控制字段，不是配置项
        clear_jev_key = bool(data.pop("clear_jev_api_key", False))

        updates: dict[str, Any] = {}
        for key, value in data.items():
            if key not in schema:
                return error_response(f"未知的配置项: {key}")
            meta = schema[key]
            ftype = meta.get("type")

            if ftype == "int":
                try:
                    value = int(value)
                except (TypeError, ValueError):
                    return error_response(f"{key} 需要是整数")
                lo, hi = self._INT_RANGES.get(key, (-(2**31), 2**31))
                if not lo <= value <= hi:
                    return error_response(f"{key} 需要在 {lo} ~ {hi} 之间")
            elif ftype == "float":
                try:
                    value = float(value)
                except (TypeError, ValueError):
                    return error_response(f"{key} 需要是数字")
                lo, hi = self._FLOAT_RANGES.get(key, (float("-inf"), float("inf")))
                if not lo <= value <= hi:
                    return error_response(f"{key} 需要在 {lo} ~ {hi} 之间")
            elif ftype == "bool":
                value = bool(value)
            elif ftype == "string":
                value = str(value)
                if key in ("generation_theme", "generation_ideas"):
                    limit = 120 if key == "generation_theme" else 2000
                    if len(value) > limit:
                        return error_response(f"{key} 最多 {limit} 字")
                if meta.get("options") and value not in meta["options"]:
                    return error_response(f"{key} 的值不合法: {value}")
            else:
                return error_response(f"配置项 {key} 的类型不受支持: {ftype}")

            if key == "jev_api_key" and value == "":
                # 空串=前端没改过这个输入框，不能把它当成「清空」
                continue
            updates[key] = value

        if clear_jev_key:
            updates["jev_api_key"] = ""

        if not updates:
            return _ok({"saved": 0})

        config.save_config(updates)
        self.plugin._load_config()
        logger.info(
            f"网页端保存配置: {', '.join(sorted(updates))} by {request.username}"
        )
        return _ok({"saved": len(updates)})
