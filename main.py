import asyncio
import json
import os
import random
import threading
import uuid
from datetime import datetime
from pathlib import Path

import httpx

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageEventResult, filter
from astrbot.api.message_components import At, Reply
from astrbot.api.provider import LLMResponse
from astrbot.api.star import Context, Star, StarTools

# SessionFilter 尚未在 astrbot.api.util 中导出，只能从 core 导入
from astrbot.core.utils.session_waiter import (
    SessionController,
    SessionFilter,
    session_waiter,
)

from .webui import SoupaiWebApi

# 未提供会话标识时归入的记录桶，例如后台任务取题
DEFAULT_SESSION = "__default__"


# 线程安全的题库管理基类
class ThreadSafeStoryStorage:
    """线程安全的题库管理基类，按会话分别记录哪些题已经出过。

    使用记录保存的是故事 id 而非下标：下标会因为增删题目而整体偏移，
    本地库存满后的 pop(0) 和网页端的删除都会让记录张冠李戴。
    """

    def __init__(self, storage_name: str, data_path=None):
        self.storage_name = storage_name
        self.data_path = data_path
        self.stories: list[dict] = []
        # 会话标识 -> 该会话已出过的故事 id
        self.usage: dict[str, set[str]] = {}
        # 被隐藏的故事 id，用于在不改动只读题库文件的前提下屏蔽某些题
        self.hidden_ids: set[str] = set()
        self.lock = threading.RLock()
        self.usage_file = (
            self.data_path / f"{storage_name}_usage.json" if self.data_path else None
        )
        self.hidden_file = (
            self.data_path / f"{storage_name}_hidden.json" if self.data_path else None
        )
        self.load_usage_record()
        self.load_hidden_record()

    # ------------------------------------------------------------------ id

    def story_id(self, story: dict, index: int) -> str:
        """故事的稳定标识。没有 id 字段的（只读的网络题库）退化为下标。"""
        sid = story.get("id") if isinstance(story, dict) else None
        return str(sid) if sid else str(index)

    def _assign_missing_ids(self) -> bool:
        """给缺少 id 的故事补一个，返回是否产生了改动。"""
        changed = False
        for story in self.stories:
            if isinstance(story, dict) and not story.get("id"):
                story["id"] = uuid.uuid4().hex[:12]
                changed = True
        return changed

    def find_index(self, story_id: str) -> int:
        """按 id 查下标，找不到返回 -1。"""
        for i, story in enumerate(self.stories):
            if self.story_id(story, i) == str(story_id):
                return i
        return -1

    # --------------------------------------------------------------- usage

    def load_usage_record(self):
        """加载使用记录。旧版的扁平列表格式会被备份后丢弃。"""
        self.usage = {}
        if not self.usage_file or not self.usage_file.exists():
            return
        try:
            with open(self.usage_file, encoding="utf-8") as f:
                raw = json.load(f)
        except Exception as e:
            logger.error(f"加载使用记录失败: {e}")
            return

        if isinstance(raw, list):
            # v1 的全局记录按下标存储，换成按会话+id 之后无法对应，
            # 备份原文件后从空记录重新开始
            backup = self.usage_file.with_suffix(".v1.json")
            try:
                self.usage_file.replace(backup)
                logger.warning(
                    f"{self.storage_name} 的使用记录是旧版全局格式，已备份到 {backup.name} "
                    f"并重置为按会话记录"
                )
            except Exception as e:
                logger.error(f"备份旧版使用记录失败: {e}")
            return

        if isinstance(raw, dict):
            self.usage = {
                str(session): {str(i) for i in ids}
                for session, ids in raw.items()
                if isinstance(ids, list)
            }
            total = sum(len(v) for v in self.usage.values())
            logger.info(
                f"从 {self.usage_file.name} 加载了 {len(self.usage)} 个会话、共 {total} 条使用记录"
            )

    def save_usage_record(self):
        """保存使用记录到文件"""
        if not self.usage_file:
            return
        try:
            self.usage_file.parent.mkdir(parents=True, exist_ok=True)
            payload = {s: sorted(ids) for s, ids in self.usage.items() if ids}
            with open(self.usage_file, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"保存使用记录失败: {e}")

    def used_ids(self, session: str) -> set[str]:
        """某个会话已出过的故事 id"""
        with self.lock:
            return set(self.usage.get(session or DEFAULT_SESSION, set()))

    def mark_used(self, session: str, story_id: str) -> None:
        with self.lock:
            self.usage.setdefault(session or DEFAULT_SESSION, set()).add(str(story_id))
            self.save_usage_record()

    def unmark_used(self, session: str, story_id: str) -> None:
        with self.lock:
            self.usage.get(session or DEFAULT_SESSION, set()).discard(str(story_id))
            self.save_usage_record()

    def reset_usage(self, session: str | None = None):
        """重置使用记录。不指定会话则清空所有会话。"""
        with self.lock:
            if session is None:
                self.usage.clear()
                logger.info(f"{self.storage_name} 全部会话的使用记录已重置")
            else:
                self.usage.pop(session, None)
                logger.info(f"{self.storage_name} 会话 {session} 的使用记录已重置")
            self.save_usage_record()

    def sessions(self) -> list[str]:
        with self.lock:
            return sorted(s for s, ids in self.usage.items() if ids)

    def get_usage_info(self, session: str | None = None) -> dict:
        """获取使用记录信息。不指定会话则统计所有会话去重后的总量。"""
        with self.lock:
            if session is None:
                merged: set[str] = set()
                for ids in self.usage.values():
                    merged |= ids
            else:
                merged = set(self.usage.get(session, set()))
            return {"used": len(merged), "used_ids": sorted(merged)}

    # -------------------------------------------------------------- hidden

    def load_hidden_record(self):
        self.hidden_ids = set()
        if not self.hidden_file or not self.hidden_file.exists():
            return
        try:
            with open(self.hidden_file, encoding="utf-8") as f:
                self.hidden_ids = {str(i) for i in json.load(f)}
            if self.hidden_ids:
                logger.info(f"{self.storage_name} 有 {len(self.hidden_ids)} 道题被屏蔽")
        except Exception as e:
            logger.error(f"加载屏蔽名单失败: {e}")

    def save_hidden_record(self):
        if not self.hidden_file:
            return
        try:
            self.hidden_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.hidden_file, "w", encoding="utf-8") as f:
                json.dump(sorted(self.hidden_ids), f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"保存屏蔽名单失败: {e}")

    def set_hidden(self, story_id: str, hidden: bool) -> None:
        with self.lock:
            if hidden:
                self.hidden_ids.add(str(story_id))
            else:
                self.hidden_ids.discard(str(story_id))
            self.save_hidden_record()

    # ---------------------------------------------------------------- pick

    def pick_story(self, session: str) -> tuple[str, str] | None:
        """为某个会话取一道没出过、也没被屏蔽的题。

        该会话把可用的题出完后，只重置这个会话的记录，不影响其他会话。
        """
        with self.lock:
            if not self.stories:
                return None

            used = self.usage.setdefault(session or DEFAULT_SESSION, set())
            candidates = [
                (i, s)
                for i, s in enumerate(self.stories)
                if self.story_id(s, i) not in self.hidden_ids
            ]
            if not candidates:
                return None

            available = [
                (i, s) for i, s in candidates if self.story_id(s, i) not in used
            ]
            if not available:
                logger.info(
                    f"{self.storage_name} 对会话 {session} 已出完，清空该会话记录重新开始"
                )
                used.clear()
                available = candidates

            index, story = random.choice(available)
            used.add(self.story_id(story, index))
            self.save_usage_record()
            logger.info(
                f"{self.storage_name} 取题 id={self.story_id(story, index)}，"
                f"该会话已出 {len(used)}/{len(candidates)}"
            )
            return story["puzzle"], story["answer"]


# 游戏状态管理
class GameState:
    def __init__(self):
        self.active_games: dict[str, dict] = {}  # 群聊ID -> 游戏状态

    def start_game(self, group_id: str, puzzle: str, answer: str, **extra) -> bool:
        """开始游戏，返回是否成功"""
        if group_id in self.active_games:
            return False
        game_data = {
            "puzzle": puzzle,
            "answer": answer,
            "is_active": True,
            "qa_history": [],
            "hint_history": [],
        }
        game_data.update(extra)
        self.active_games[group_id] = game_data
        return True

    def end_game(self, group_id: str) -> bool:
        """结束游戏"""
        if group_id in self.active_games:
            del self.active_games[group_id]
            return True
        return False

    def get_game(self, group_id: str) -> dict | None:
        """获取游戏状态"""
        return self.active_games.get(group_id)

    def is_game_active(self, group_id: str) -> bool:
        """检查是否有活跃游戏"""
        return group_id in self.active_games


# 网络海龟汤管理
class NetworkSoupaiStorage(ThreadSafeStoryStorage):
    def __init__(self, network_file: str, data_path=None):
        # 初始化基类
        super().__init__("network_soupai", data_path)
        self.network_file = network_file
        self.stories: list[dict] = []
        self.load_stories()
        self._drop_index_era_records()

    def _drop_index_era_records(self) -> None:
        """丢掉按下标记录的使用/屏蔽数据。

        网络题库过去没有 id 字段，``story_id`` 退化成下标，于是记录里存的
        是 "0"、"17" 这样的位置。题库去重后顺序整体变了，这些下标指向的
        已经是另一道题——留着比清掉更糟，它会让没出过的题被当成出过。
        对不上号，也就没法精确迁移，只能备份原文件后重来。
        """

        def is_index(sid: str) -> bool:
            # 现在的 id 是 12 位十六进制，旧下标最多 3 位，不会误判
            return sid.isdigit() and len(sid) < 12

        with self.lock:
            stale_usage = {
                session: {sid for sid in ids if is_index(sid)}
                for session, ids in self.usage.items()
            }
            stale_usage = {s: ids for s, ids in stale_usage.items() if ids}
            stale_hidden = {sid for sid in self.hidden_ids if is_index(sid)}
            if not stale_usage and not stale_hidden:
                return

            for path, drop in (
                (self.usage_file, bool(stale_usage)),
                (self.hidden_file, bool(stale_hidden)),
            ):
                if not drop or not path or not path.exists():
                    continue
                try:
                    path.replace(path.with_suffix(".byindex.json"))
                except Exception as e:
                    logger.error(f"备份下标格式的 {path.name} 失败: {e}")

            dropped = 0
            for session, ids in stale_usage.items():
                self.usage[session] -= ids
                dropped += len(ids)
            self.hidden_ids -= stale_hidden
            self.save_usage_record()
            self.save_hidden_record()
            logger.warning(
                f"{self.storage_name} 有 {dropped} 条使用记录、{len(stale_hidden)} 条屏蔽记录"
                f"是按下标存的，题库加上 id 后已对不上号，已备份原文件并丢弃"
            )

    def load_stories(self):
        """从文件加载网络海龟汤故事"""
        try:
            if os.path.exists(self.network_file):
                with open(self.network_file, encoding="utf-8") as f:
                    self.stories = json.load(f)
                logger.info(
                    f"从 {self.network_file} 加载了 {len(self.stories)} 个网络海龟汤故事"
                )
            else:
                self.stories = []
                logger.warning(f"网络海龟汤文件不存在: {self.network_file}")
        except Exception as e:
            logger.error(f"加载网络海龟汤失败: {e}")
            self.stories = []

    def get_story(self, session: str = DEFAULT_SESSION) -> tuple[str, str] | None:
        """为指定会话取一道网络题"""
        return self.pick_story(session)

    def get_storage_info(self, session: str | None = None) -> dict:
        """获取网络题库信息"""
        usage_info = self.get_usage_info(session)
        total = len(self.stories)
        return {
            "total": total,
            "hidden": len(self.hidden_ids),
            "available": total - len(self.hidden_ids) - usage_info["used"],
            "used": usage_info["used"],
        }


# 存储库管理
class LocalSoupaiStorage(ThreadSafeStoryStorage):
    def __init__(self, storage_file: str, max_size: int = 50, data_path=None):
        # 初始化基类
        super().__init__("storage_soupai", data_path)
        self.storage_file = storage_file
        self.max_size = max_size
        self.stories: list[dict] = []
        self.load_stories()

    def load_stories(self):
        """从文件加载故事"""
        try:
            storage_path = (
                self.storage_file
                if isinstance(self.storage_file, str)
                else str(self.storage_file)
            )
            if os.path.exists(storage_path):
                with open(storage_path, encoding="utf-8") as f:
                    self.stories = json.load(f)
                logger.info(f"从 {storage_path} 加载了 {len(self.stories)} 个故事")
                if self._assign_missing_ids():
                    self.save_stories()
            else:
                self.stories = []
                logger.info("存储库文件不存在，创建新的存储库")
        except Exception as e:
            logger.error(f"加载故事失败: {e}")
            self.stories = []

    def save_stories(self):
        """保存故事到文件"""
        try:
            storage_path = (
                self.storage_file
                if isinstance(self.storage_file, str)
                else str(self.storage_file)
            )
            # 确保目录存在
            os.makedirs(os.path.dirname(storage_path), exist_ok=True)
            with open(storage_path, "w", encoding="utf-8") as f:
                json.dump(self.stories, f, ensure_ascii=False, indent=2)
            logger.info(f"保存了 {len(self.stories)} 个故事到 {storage_path}")
        except Exception as e:
            logger.error(f"保存故事失败: {e}")

    def add_story(self, puzzle: str, answer: str) -> bool:
        """添加故事到存储库"""
        with self.lock:
            if len(self.stories) >= self.max_size:
                # 移除最旧的故事。使用记录按 id 保存，不会因此错位
                dropped = self.stories.pop(0)
                logger.info("存储库已满，移除最旧的故事")
                dropped_id = dropped.get("id")
                if dropped_id:
                    for ids in self.usage.values():
                        ids.discard(str(dropped_id))

            story = {
                "id": uuid.uuid4().hex[:12],
                "puzzle": puzzle,
                "answer": answer,
                "created_at": datetime.now().isoformat(),
            }
            self.stories.append(story)
            self.save_stories()
            self.save_usage_record()
            logger.info(f"添加新故事到存储库，当前存储库大小: {len(self.stories)}")
            return True

    def get_story(self, session: str = DEFAULT_SESSION) -> tuple[str, str] | None:
        """为指定会话取一道本地题"""
        return self.pick_story(session)

    def get_storage_info(self, session: str | None = None) -> dict:
        """获取存储库信息"""
        usage_info = self.get_usage_info(session)
        return {
            "total": len(self.stories),
            "max_size": self.max_size,
            "hidden": len(self.hidden_ids),
            "available": self.max_size - len(self.stories),
            "used": usage_info["used"],
            "remaining": len(self.stories) - usage_info["used"],
        }


# 自定义海龟汤存储
class CustomSoupaiStorage(ThreadSafeStoryStorage):
    def __init__(self, storage_file: str, data_path=None):
        # 初始化基类
        super().__init__("custom_soupai", data_path)
        self.storage_file = storage_file
        self.stories: list[dict] = []
        self.load_stories()

    def load_stories(self):
        """从文件加载自定义故事"""
        try:
            storage_path = (
                self.storage_file
                if isinstance(self.storage_file, str)
                else str(self.storage_file)
            )
            if os.path.exists(storage_path):
                with open(storage_path, encoding="utf-8") as f:
                    self.stories = json.load(f)
                logger.info(
                    f"从 {storage_path} 加载了 {len(self.stories)} 个自定义海龟汤故事"
                )
                if self._assign_missing_ids():
                    self.save_stories()
            else:
                self.stories = []
                logger.info("自定义海龟汤文件不存在，创建新的存储库")
        except Exception as e:
            logger.error(f"加载自定义海龟汤失败: {e}")
            self.stories = []

    def save_stories(self):
        """保存自定义故事到文件"""
        try:
            storage_path = (
                self.storage_file
                if isinstance(self.storage_file, str)
                else str(self.storage_file)
            )
            # 确保目录存在
            os.makedirs(os.path.dirname(storage_path), exist_ok=True)
            with open(storage_path, "w", encoding="utf-8") as f:
                json.dump(self.stories, f, ensure_ascii=False, indent=2)
            logger.info(
                f"保存了 {len(self.stories)} 个自定义海龟汤故事到 {storage_path}"
            )
        except Exception as e:
            logger.error(f"保存自定义海龟汤失败: {e}")

    def add_story(self, puzzle: str, answer: str) -> bool:
        """添加自定义故事到存储库"""
        with self.lock:
            story = {
                "id": uuid.uuid4().hex[:12],
                "puzzle": puzzle,
                "answer": answer,
                "created_at": datetime.now().isoformat(),
            }
            self.stories.append(story)
            self.save_stories()
            logger.info(f"添加新自定义海龟汤故事，当前存储库大小: {len(self.stories)}")
            return True

    def get_story(self, session: str = DEFAULT_SESSION) -> tuple[str, str] | None:
        """为指定会话取一道自定义题"""
        return self.pick_story(session)

    def get_storage_info(self, session: str | None = None) -> dict:
        """获取自定义存储库信息"""
        usage_info = self.get_usage_info(session)
        return {
            "total": len(self.stories),
            "hidden": len(self.hidden_ids),
            "used": usage_info["used"],
            "remaining": len(self.stories) - usage_info["used"],
        }


# 验证结果类
class VerificationResult:
    """验证结果类"""

    def __init__(self, level: str, comment: str, is_correct: bool = False):
        self.level = level
        self.comment = comment
        self.is_correct = is_correct

    def to_dict(self) -> dict:
        return {
            "level": self.level,
            "comment": self.comment,
            "is_correct": self.is_correct,
        }


# 自定义会话过滤器 - 以群为单位进行会话控制
class GroupSessionFilter(SessionFilter):
    """会话过滤器，确保每个群的会话独立"""

    def __init__(self, group_id: str):
        # 为每个会话保存其所属群 ID
        self.group_id = group_id

    def filter(self, event: AstrMessageEvent) -> str:
        current_group_id = (
            event.get_group_id() if event.get_group_id() else event.unified_msg_origin
        )
        # 仅当事件来自该群时才返回有效的会话 ID，否则返回空串避免误触发
        return self.group_id if current_group_id == self.group_id else ""


class SoupaiPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.game_state = GameState()

        # Jev 的 httpx 客户端，首次判定时惰性创建
        self._jev_client: httpx.AsyncClient | None = None

        self._load_config()

        # 难度设置
        self.difficulty_settings = {
            "简单": {
                "limit": None,
                "accept_levels": ["完全还原", "核心推理正确"],
                "hint_limit": 10,
            },
            "普通": {
                "limit": 35,
                "accept_levels": ["完全还原"],
                "hint_limit": 5,
            },
            "困难": {
                "limit": 15,
                "accept_levels": ["完全还原"],
                "hint_limit": 1,
            },
            "666开挂了": {
                "limit": 5,
                "accept_levels": ["完全还原"],
                "hint_limit": 0,
            },
        }
        self.group_difficulty: dict[str, str] = {}

        # 数据存储路径: 使用框架提供的工具获取插件数据目录
        self.data_path = StarTools.get_data_dir()
        self.data_path.mkdir(parents=True, exist_ok=True)

        # 存储库初始化延迟到 init 方法中
        self.local_story_storage = None
        self.online_story_storage = None
        self.custom_story_storage = None

        # 防止重复调用的状态
        self.generating_games = set()  # 正在生成谜题的群聊ID集合

        # 自动生成状态
        self.auto_generating = False
        self.auto_generate_task = None

    def _load_config(self) -> None:
        """从 self.config 读取全部配置项到实例属性。

        配置有两个入口：面板插件配置页，以及插件网页管理页的设置标签。
        面板保存会热重载插件、实例整个重建，走这里没问题；网页端保存
        用 config.save_config(replace) 只写文件不重建实例，所以保存后
        也要调一次这里，让新配置立即生效，不用手动重载。
        """
        self.generate_llm_provider_id = self.config.get("generate_llm_provider", "")
        self.judge_llm_provider_id = self.config.get("judge_llm_provider", "")
        self.game_timeout = self.config.get("game_timeout", 300)
        self.storage_max_size = self.config.get("storage_max_size", 50)
        self.auto_generate_start = self.config.get("auto_generate_start", 3)
        self.auto_generate_end = self.config.get("auto_generate_end", 6)
        self.puzzle_source_strategy = self.config.get(
            "puzzle_source_strategy", "network_first"
        )
        # TODO: 别名兼容处理，建议若干版本后删除
        if self.puzzle_source_strategy == "ai_first":
            self.puzzle_source_strategy = "local_first"

        # 回复方式：quote=引用提问者原消息，direct=直接回复
        self.reply_mode = self.config.get("reply_mode", "quote")
        if self.reply_mode not in ("quote", "direct"):
            logger.warning(
                f"未知的 reply_mode 配置值 {self.reply_mode!r}，回退为 quote"
            )
            self.reply_mode = "quote"

        # 每局可用的验证次数
        self.verification_limit = self.config.get("verification_limit", 2)

        # 提问判定引擎。Jev 只做「是/否/不重要/是也不是」这类封闭选项的快速判别；
        # 出题、提示、验证一律由 LLM 负责
        self.judge_engine = str(self.config.get("judge_engine", "llm")).strip().lower()
        if self.judge_engine not in ("llm", "jev"):
            logger.warning(
                f"未知的 judge_engine 配置值 {self.judge_engine!r}，回退为 llm"
            )
            self.judge_engine = "llm"
        self.judge_fallback_to_llm = bool(
            self.config.get("judge_fallback_to_llm", True)
        )

        self.jev_api_key = str(self.config.get("jev_api_key", "")).strip()
        # 面板上清空输入框存进来的是空串而不是缺键，默认值得靠 or 兜住，
        # 不能只依赖 .get 的第二参数——否则 model="" 的请求必然报错
        self.jev_base_url = (
            str(self.config.get("jev_base_url", "")).strip().rstrip("/")
            or "https://api.typesafe.ai"
        )
        self.jev_model = str(self.config.get("jev_model", "")).strip() or "jev-latest"
        self.jev_judge_min_confidence = float(
            self.config.get("jev_judge_min_confidence", 0.5)
        )

        if self.judge_engine == "jev" and not self.jev_api_key:
            logger.warning("判定引擎选了 Jev 但未填写 API Key，将继续使用 LLM 判定")
            self.judge_engine = "llm"

        # 客户端持有旧的 base_url 和 key，配置变了必须丢弃重建
        if self._jev_client is not None:
            self._jev_client = None

    def _ensure_story_storages(self) -> None:
        """确保题库存储被初始化。

        在某些环境下, 插件的 ``init`` 方法可能未被调用或异常退出,
        导致存储对象仍为 ``None``。为避免后续调用出现
        ``'NoneType' object has no attribute 'get_story'`` 的错误, 这里
        提供一次性惰性初始化。
        """

        if self.local_story_storage is None:
            storage_file = self.data_path / "storage_soupai.json"
            self.local_story_storage = LocalSoupaiStorage(
                storage_file, self.storage_max_size, self.data_path
            )

        if self.online_story_storage is None:
            plugin_dir = Path(__file__).resolve().parent
            network_file = plugin_dir / "network_soupai.json"
            self.online_story_storage = NetworkSoupaiStorage(
                str(network_file), self.data_path
            )

        if self.custom_story_storage is None:
            custom_file = self.data_path / "custom_soupai.json"
            self.custom_story_storage = CustomSoupaiStorage(custom_file, self.data_path)

    async def initialize(self):
        """插件加载完成后由 AstrBot 调用。

        必须叫 initialize：star_manager 只会 `await star_cls.initialize()`，
        Star 基类上也只有 initialize / terminate 这一对。叫成别的名字不会
        报错，只是永远不执行——网页接口注册不上（打开页面报「未找到该路由」），
        自动出题也不会启动。
        """
        await super().initialize()

        # 初始化存储对象
        self._ensure_story_storages()

        # 注册网页管理接口，对应 pages/dashboard 那个页面
        try:
            self.web_api = SoupaiWebApi(self)
            self.web_api.register()
        except Exception as e:
            logger.error(f"注册网页管理接口失败，网页端将不可用: {e}")

        # 启动自动生成任务
        asyncio.create_task(self._start_auto_generate())

        online_info = self.online_story_storage.get_storage_info()
        logger.info(
            f"海龟汤插件已加载，配置: 生成LLM提供商={self.generate_llm_provider_id}, 判断LLM提供商={self.judge_llm_provider_id}, 超时时间={self.game_timeout}秒, 网络题库={online_info['total']}个谜题, 本地存储库大小={self.storage_max_size}, 谜题来源策略={self.puzzle_source_strategy}"
        )

    async def terminate(self):
        """插件卸载时清理资源"""
        # 停止自动生成
        self.auto_generating = False
        if self.auto_generate_task:
            self.auto_generate_task.cancel()
        if self._jev_client is not None:
            await self._jev_client.aclose()
            self._jev_client = None
        logger.info("海龟汤插件已卸载呜呜呜呜呜")

    async def _start_auto_generate(self):
        """启动自动生成任务"""
        while True:
            try:
                now = datetime.now()
                current_hour = now.hour

                # 检查是否在自动生成时间范围内
                if self.auto_generate_start <= current_hour < self.auto_generate_end:
                    if not self.auto_generating:
                        # 检查存储库是否已满，如果已满则不启动自动生成
                        self._ensure_story_storages()
                        storage_info = self.local_story_storage.get_storage_info()
                        if storage_info["available"] <= 0:
                            logger.info(
                                f"本地存储库已满，跳过自动生成，时间: {current_hour}:00"
                            )
                            # 等待1小时后再次检查
                            await asyncio.sleep(3600)  # 1小时
                            continue

                        logger.info(f"开始自动生成故事，时间: {current_hour}:00")
                        self.auto_generating = True
                        asyncio.create_task(self._auto_generate_loop())
                else:
                    if self.auto_generating:
                        logger.info(f"停止自动生成故事，时间: {current_hour}:00")
                        self.auto_generating = False

                # 等待1小时后再次检查
                await asyncio.sleep(3600)  # 1小时
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"自动生成任务错误: {e}")
                await asyncio.sleep(3600)  # 出错后等待1小时再试

    async def _auto_generate_loop(self):
        """自动生成循环"""
        # 确保在运行循环前题库已初始化
        self._ensure_story_storages()
        while self.auto_generating:
            try:
                # 检查本地存储库是否已满
                storage_info = self.local_story_storage.get_storage_info()
                if storage_info["available"] <= 0:
                    logger.info("本地存储库已满，停止自动生成")
                    self.auto_generating = False
                    break

                # 生成一个故事
                puzzle, answer = await self.generate_story_with_llm()
                if puzzle and answer and not puzzle.startswith("（"):
                    self.local_story_storage.add_story(puzzle, answer)
                    logger.info("自动生成故事成功")
                else:
                    logger.warning("自动生成故事失败")

                # 等待5分钟再生成下一个
                await asyncio.sleep(300)  # 5分钟
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"自动生成故事错误: {e}")
                await asyncio.sleep(300)  # 出错后等待5分钟再试

    async def _jev_choice(
        self,
        state: dict,
        instructions: str,
        criteria: dict[str, str],
    ) -> tuple[str, float] | None:
        """用 Jev 做一次单选判定。

        返回 (选中的选项, 置信度)；未启用 Jev、置信度不足或请求失败时返回
        None，调用方据此改用 LLM。Jev 只会返回 criteria 里的键，不会跑题。

        关闭兜底时置信度门槛不生效（始终采信 Jev），但请求真的失败时
        仍然返回 None —— 否则玩家等不到任何回复。
        """
        if self.judge_engine != "jev":
            return None

        min_confidence = (
            self.jev_judge_min_confidence if self.judge_fallback_to_llm else 0.0
        )

        if self._jev_client is None:
            self._jev_client = httpx.AsyncClient(
                base_url=self.jev_base_url,
                headers={"Authorization": f"Bearer {self.jev_api_key}"},
                timeout=httpx.Timeout(15.0),
            )

        payload = {
            "model": self.jev_model,
            "state": state,
            "questions": {
                "verdict": {
                    "type": "choice",
                    "instructions": instructions,
                    "criteria": criteria,
                }
            },
        }
        try:
            resp = await self._jev_client.post("/v1/systemone", json=payload)
            resp.raise_for_status()
            answer = resp.json()["answers"]["verdict"]
            choice = answer["choice"]
            confidence = answer.get("confidence", 0.0)
        except Exception as e:
            logger.warning(f"Jev 判定失败，改用 LLM: {e}")
            return None

        if choice not in criteria:
            logger.warning(f"Jev 返回了未知选项 {choice!r}，改用 LLM")
            return None
        if confidence < min_confidence:
            logger.info(
                f"Jev 判定 {choice!r} 置信度 {confidence:.2f} 低于 {min_confidence}，改用 LLM"
            )
            return None

        logger.info(f"Jev 判定: {choice}（置信度 {confidence:.2f}）")
        return choice, confidence

    def _resolve_provider(self, provider_id: str, umo: str | None = None):
        """解析要使用的 LLM 提供商，未找到时返回 None 并记录日志。

        Args:
            provider_id: 插件配置中指定的提供商 ID，留空表示跟随 AstrBot 当前设置。
            umo: 会话标识。启用了「提供商会话隔离」时，据此取该会话偏好的模型。
        """
        if provider_id:
            provider = self.context.get_provider_by_id(provider_id)
            if provider is None:
                logger.error(f"未找到指定的 LLM 提供商: {provider_id}")
            return provider

        provider = self.context.get_using_provider(umo=umo)
        if provider is None:
            logger.error("未配置 LLM 服务商")
        return provider

    # ✅ 生成谜题和答案
    async def generate_story_with_llm(self, umo: str | None = None) -> tuple[str, str]:
        """使用 LLM 生成海龟汤谜题"""

        provider = self._resolve_provider(self.generate_llm_provider_id, umo)
        if provider is None:
            if self.generate_llm_provider_id:
                return "（无法生成题面，指定的生成 LLM 提供商不存在）", "（无）"
            return "（无法生成题面，请先配置大语言模型）", "（无）"

        prompt = self._build_puzzle_prompt()

        try:
            logger.info("开始调用 LLM 生成谜题...")
            llm_resp: LLMResponse = await provider.text_chat(
                prompt=prompt,
                contexts=[],
                func_tool=None,
                image_urls=[],
                system_prompt="你是一个专业的反转推理谜题创作者，专门为海龟汤游戏设计谜题。你需要创作简洁、具象、有逻辑反转的谜题，让玩家能够通过是/否提问逐步还原真相。每次创作都必须全新、原创，不能重复已有故事。",
            )

            text = llm_resp.completion_text.strip()
            logger.info(f"LLM 返回内容: {text}")

            # 尝试多种格式解析
            puzzle = None
            answer = None

            # 格式1: "题面：xxx 答案：xxx"
            if "题面：" in text and "答案：" in text:
                puzzle = text.split("题面：")[1].split("答案：")[0].strip()
                answer = text.split("答案：")[1].strip()

            # 格式2: "**题面**：xxx **答案**：xxx" (Markdown格式)
            elif "**题面**" in text and "**答案**" in text:
                puzzle = text.split("**题面**")[1].split("**答案**")[0].strip()
                if puzzle.startswith("：") or puzzle.startswith(":"):
                    puzzle = puzzle[1:].strip()
                answer = text.split("**答案**")[1].strip()
                if answer.startswith("：") or answer.startswith(":"):
                    answer = answer[1:].strip()

            # 格式3: "题面：xxx\n答案：xxx"
            elif "题面：" in text and "\n答案：" in text:
                puzzle = text.split("题面：")[1].split("\n答案：")[0].strip()
                answer = text.split("\n答案：")[1].strip()

            # 格式4: 尝试从文本中提取题面和答案
            else:
                lines = text.split("\n")
                for line in lines:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue

                    # 寻找题面
                    if not puzzle and ("题面" in line or "**题面**" in line):
                        puzzle = line
                        if "：" in line:
                            puzzle = line.split("：", 1)[1].strip()
                        elif ":" in line:
                            puzzle = line.split(":", 1)[1].strip()
                        # 移除可能的Markdown标记
                        puzzle = puzzle.replace("**", "").replace("*", "").strip()

                    # 寻找答案
                    elif not answer and ("答案" in line or "**答案**" in line):
                        answer = line
                        if "：" in line:
                            answer = line.split("：", 1)[1].strip()
                        elif ":" in line:
                            answer = line.split(":", 1)[1].strip()
                        # 移除可能的Markdown标记
                        answer = answer.replace("**", "").replace("*", "").strip()

                    # 如果找到了题面但还没找到答案，继续寻找
                    elif puzzle and not answer and len(line) > 20:
                        # 可能是答案的开始
                        answer = line

            if puzzle and answer:
                # 清理答案中的多余内容
                if "----" in answer:
                    answer = answer.split("----")[0].strip()
                if "---" in answer:
                    answer = answer.split("---")[0].strip()

                logger.info(f"成功解析谜题: 题面='{puzzle}', 答案='{answer}'")
                return puzzle, answer

            logger.error(f"LLM 返回内容格式错误: {text}")
            return "生成失败", "无法解析 LLM 返回的内容"
        except Exception as e:
            logger.error(f"生成谜题失败: {e}")
            return "生成失败", f"LLM 调用出错: {e}"

    def _build_puzzle_prompt(self) -> str:
        """构建谜题生成的提示词"""
        import random

        # 丰富的主题列表，增加多样性
        themes = [
            # 🔍 人类行为与误导
            "误解他人行为的代价",
            "看似反常实则合理的选择",
            "主动伪装带来的反转",
            "隐瞒真相与道德困境",
            "他人为主角设下的圈套",
            "故意失败的计划",
            "真实动机被遮蔽",
            "道德与规则的冲突",
            # 🧠 心理博弈与控制
            "陷害与自保之间的抉择",
            "信息不对称引发的误判",
            "操控他人感知的行为",
            "主观偏见导致的误解",
            "冷静外表下的激烈动机",
            "以退为进的心理策略",
            # 🧪 现实逻辑与错觉
            "空间结构引发的错觉",
            "物品使用的误导性",
            "因果顺序的错配",
            "隐藏在日常中的意外用途",
            "非典型证据的误导",
            "时间线的巧妙安排",
            # 📍 社会环境与冲突
            "职场中的暗中博弈",
            "公众场合下的隐秘行为",
            "权力结构下的自我保护",
            "日常制度漏洞的利用",
            "面对规则边缘的选择",
            "技术被滥用的后果",
            "资源争夺下的灰色行为",
            # 🧩 特定身份与角色
            "保安不是最了解监控的人",
            "程序员的删除并非错误",
            "清洁工的观察比谁都细致",
            "老师的行为引发质疑",
            "医生做出的不寻常选择",
            "司机的路线似乎有问题",
            "演员的自毁是否另有用意",
            # 🕯 情感错位与人性
            "好意引发的巨大误会",
            "爱被误解为恶意",
            "习惯性行为暴露了真相",
            "为了他人不得不说谎",
            "逃避责任的精心设计",
            "牺牲某人换取整体安全",
        ]

        selected_theme = random.choice(themes)

        prompt = (
            f"你是一个逻辑推理谜题设计师，正在创作一个用于【海龟汤游戏】的原创谜题。\n\n"
            "【目标】：生成一个结构清晰、信息复杂、具备反差感的逻辑谜题，玩家可以通过是/否提问逐步还原真相。答案中解释的所有行为和结果，必须都在题面中有所体现或留有暗示，禁止引入题面未提及的核心行为或结果。谜题在满足以上要求的前提下，应尽可能风格多样、身份多样、行为设定独特、反转机制不重复，避免模板化创作。\n\n"
            "【题面】要求：\n"
            "1~2句话，控制在30字以内，但不能过短或单一；\n"
            "必须包含具体人物 + 至少两个具体细节或行为（如行为+环境、行为+结果、两个动作等）；\n"
            "行为必须具象明确，严禁使用抽象词、形容词、心理或情绪描述；\n"
            "必须包含异常或矛盾要素，能引发为什么？的思考；\n"
            "允许黑暗元素，如陷害、伤害、诱导、自残、掩盖证据等冷峻现实情节；\n"
            "不得使用幻想、梦境、魔法、精神病等设定；\n"
            "使用陈述句，不得使用疑问句或解释语气。\n\n"
            "【答案】要求：\n"
            "不超过200字；\n"
            "真实可实现，具有完整因果逻辑；\n"
            "至少包含两个推理层次或误导点（例如动机误导+情境误导）；\n"
            "不得出现反转在于、真相是、实际上之类的总结或解释语；\n"
            "不要使用说明性句子或教学语气；\n"
            "整体氛围可偏冷峻，但必须具备可还原性，逻辑自洽。\n"
            "答案仅用于解释题面中已有行为与结果，禁止引入题面未包含的额外关键事件或角色。\n\n"
            "参考例子：\n"
            "题面：女演员在试镜前剪断了自己的裙子，却最终被录取。\n"
            "答案：这名女演员事先得知试镜剧本中有一幕裙子被撕裂的情节。她故意提前剪开裙子并精心处理切口，使在表演时裙子自然裂开看起来逼真震撼。评审认为她的表演最具冲击力，毫不犹豫录取了她。她的破坏行为反而让她脱颖而出。\n\n"
            "【输出格式】：\n"
            "题面：XXX\n"
            "答案：XXX\n\n"
            f"请基于「{selected_theme}」主题生成一个完全原创的反转推理谜题。"
        )

        return prompt

    async def _generate_for_storage(self) -> bool:
        """为存储库生成故事"""
        try:
            puzzle, answer = await self.generate_story_with_llm()
            if puzzle and answer and not puzzle.startswith("（"):
                self.local_story_storage.add_story(puzzle, answer)
                logger.info("为存储库生成故事成功")
                return True
            else:
                logger.warning("为存储库生成故事失败")
                return False
        except Exception as e:
            logger.error(f"为存储库生成故事错误: {e}")
            return False

    # ✅ 验证用户推理
    #
    # 这里刻意只用 LLM，不接 Jev。Jev 把 state 当作可信数据，玩家提交的推理
    # 却是本插件里唯一有作弊动机的输入：实测提交「完全还原」四个字，Jev 就以
    # 0.87 的置信度判定完全还原，直接赢下本局。换 Score 原语也一样被骗，因为
    # 问题不在原语，而在它不设防。判定提问没有这个风险（骗到一个「是」不会赢）。
    async def verify_user_guess(
        self, user_guess: str, true_answer: str, umo: str | None = None
    ) -> VerificationResult:
        """
        验证用户推理

        Args:
            user_guess: 用户的推理内容
            true_answer: 标准答案
            umo: 会话标识，用于按会话选择 LLM 提供商

        Returns:
            VerificationResult: 验证结果
        """
        provider = self._resolve_provider(self.judge_llm_provider_id, umo)
        if provider is None:
            if self.judge_llm_provider_id:
                return VerificationResult("验证失败", "未配置判断 LLM，无法验证")
            return VerificationResult("验证失败", "未配置 LLM，无法验证")

        # 构建验证提示词
        system_prompt = self._build_verification_system_prompt()
        user_prompt = self._build_verification_user_prompt(user_guess, true_answer)

        try:
            logger.info(f"开始验证用户推理: '{user_guess[:50]}...'")

            llm_resp: LLMResponse = await provider.text_chat(
                prompt=user_prompt,
                contexts=[],
                func_tool=None,
                image_urls=[],
                system_prompt=system_prompt,
            )

            text = llm_resp.completion_text.strip()
            logger.info(f"验证 LLM 返回内容: {text}")

            # 解析验证结果
            result = self._parse_verification_result(text)
            return result

        except Exception as e:
            logger.error(f"验证用户推理失败: {e}")
            return VerificationResult("验证失败", f"验证过程中发生错误: {e}")

    def _build_verification_system_prompt(self) -> str:
        """构建验证系统提示词"""
        return """你是一个推理游戏的裁判。玩家需要还原一个隐藏的完整故事，你的任务是根据玩家的陈述与标准答案对比，判断其相似程度。

你的任务是对这两个内容进行比较，判断它们在"核心因果逻辑、关键行为动机、事件结果解释"方面是否一致。

请根据相似程度将玩家推理划分为以下四个等级之一：

1. 完全还原：核心逻辑、动机、因果链、关键行为全部准确复原，无明显偏差；
2. 核心推理正确：主干因果逻辑清晰、关键转折已被识别，但部分细节错误或过程含混；
3. 部分正确：推理中包含部分正确线索或行为判断，但整体逻辑不完整或动机解释偏离；
4. 基本不符：推理内容与真相不符，逻辑错误严重，无法解释题面设定。

请输出以下格式：
等级：{等级}
评价：{一句简评}

注意：
- 当等级为"完全还原"或"核心推理正确"时，表示玩家基本猜中了故事真相。
- 评价限一句话，只描述"完成度"本身，不得出现标准答案里的任何具体名词、人物关系、动机或情节。
- 严禁直接或间接泄露正确答案中的信息，包括行为动机、情节真相、因果反转等。
- 不得使用带有暗示性的语句，如"其实…"、"你忽略了…"、"正确是…"等。
- 严禁指出玩家"哪一处"错了或"漏了什么"，那等同于告诉玩家答案。

错误示范（这类评价一律禁止输出）：
  评价：玩家识别了超重与尸体的核心因果链，但"实际没有超重"和凶手存在等细节有偏差。
  ——它复述了答案里的具体情节，玩家看完就知道谜底了。

正确示范：
  评价：主干因果已接近，细节仍有偏差。
  评价：抓到了部分线索，整体逻辑尚未成立。

- 只输出等级和评价，不要添加其他内容。"""

    def _build_verification_user_prompt(self, user_guess: str, true_answer: str) -> str:
        """构建验证用户提示词"""
        return f"""标准答案是：
{true_answer}

玩家还原的推理是：
{user_guess}

请判断其等级和简评。"""

    def _parse_verification_result(self, text: str) -> VerificationResult:
        """解析验证结果"""
        try:
            # 提取等级和评价
            lines = text.strip().split("\n")
            level = ""
            comment = ""

            for line in lines:
                line = line.strip()
                if line.startswith("等级："):
                    level = line.replace("等级：", "").strip()
                elif line.startswith("评价："):
                    comment = line.replace("评价：", "").strip()

            # 判断是否猜中
            is_correct = level in ["完全还原", "核心推理正确"]

            if not level or not comment:
                # 如果解析失败，尝试从文本中提取信息
                if "完全还原" in text or "核心推理正确" in text:
                    level = "核心推理正确" if "核心推理正确" in text else "完全还原"
                    comment = "推理基本正确，但解析结果格式异常"
                    is_correct = True
                else:
                    level = "验证失败"
                    comment = "无法解析验证结果"
                    is_correct = False

            return VerificationResult(level, comment, is_correct)

        except Exception as e:
            logger.error(f"解析验证结果失败: {e}")
            return VerificationResult("验证失败", f"解析验证结果时发生错误: {e}")

    # ✅ 判断提问的回答方式
    # 四种判定的含义。Jev 的 criteria 与下面 LLM 提示词里的判定标准共用这一套定义，
    # 两条路径的口径必须一致，否则开关切换会改变游戏手感
    _JUDGE_CRITERIA = {
        "是": "玩家命中关键事实或行为，且该信息能直接帮助接近真相。缺少部分细节可以忽略，只要不影响推理方向。",
        "否": "与真相完全不符，或包含明显错误，会使玩家推理走向错误方向。",
        "不重要": "与故事真相无关，或该信息无法推动推理进展。",
        "是也不是": "命中部分事实，但因果关系不完整、或含有可能让玩家推理错误的成分。",
    }

    async def judge_question(
        self,
        question: str,
        true_answer: str,
        umo: str | None = None,
        puzzle: str = "",
    ) -> tuple[str, dict]:
        """判断用户提问的回答方式，优先走 Jev，失败则用 LLM。

        返回 (判定文本, 判定来源)。来源形如 ``{"engine": "jev",
        "confidence": 0.99}`` 或 ``{"engine": "llm", "fallback": True}``，
        网页端的对局页靠它标出每一问到底是谁判的；判不出来时是
        ``{"engine": "unavailable"}``，那条回复不是真正的判定结果。
        """

        jev_choice = await self._jev_choice(
            state={"谜面": puzzle, "真相": true_answer, "玩家提问": question},
            instructions="海龟汤推理游戏。请判断`玩家提问`的说法，相对于`真相`应当如何回答。",
            criteria=self._JUDGE_CRITERIA,
        )
        if jev_choice:
            choice, confidence = jev_choice
            return choice, {"engine": "jev", "confidence": round(confidence, 2)}
        if self.judge_engine == "jev" and not self.judge_fallback_to_llm:
            # 选了 Jev 又关了兜底，走到这里说明请求真的失败了
            return "判定服务暂时不可用，请稍后再问一次", {"engine": "unavailable"}

        # 配置的是 Jev 却走到这里，说明上面那次判定没成，这一问是回退来的
        llm_source = {"engine": "llm", "fallback": self.judge_engine == "jev"}

        provider = self._resolve_provider(self.judge_llm_provider_id, umo)
        if provider is None:
            unavailable = {"engine": "unavailable"}
            if self.judge_llm_provider_id:
                return "（未配置判断 LLM，无法判断）", unavailable
            return "（未配置 LLM，无法判断）", unavailable

        prompt = (
            f"海龟汤游戏规则：\n"
            f"1. 故事的完整真相是：{true_answer}\n"
            f'2. 玩家提问或陈述："{question}"\n'
            f"3. 你的任务是判断玩家的说法是否符合真相。\n"
            f'4. 只能回答："是"、"否"、"不重要"或"是也不是"。\n\n'
            f"判定标准：\n"
            f'- "是"：\n'
            f'  玩家命中关键事实或行为，且该信息能直接帮助接近真相。缺少部分细节可以忽略，只要不影响推理方向，就判"是"。\n'
            f'- "否"：\n'
            f"  与真相完全不符，或包含明显错误，会使玩家推理走向错误方向。\n"
            f'- "不重要"：\n'
            f"  与故事真相无关，或该信息无法推动推理进展。\n"
            f'- "是也不是"：\n'
            f"  玩家命中部分事实，但：\n"
            f"    1) 因果关系不完整或存在偏差；\n"
            f"    2) 表述中包含可能让玩家推理错误的成分；\n"
            f"    3) 忽略了与当前描述直接相关的重要关键点。\n"
            f'  如果只是缺少背景信息，但不影响方向，优先判"是"而不是"是也不是"。\n\n'
            f"额外说明：\n"
            f"- 不要求玩家一次性说出全部真相。\n"
            f'- 允许玩家只描述真相的一部分，只要方向正确且不会误导，就判"是"。\n'
            f'- 对可能误导玩家的陈述要谨慎，宁可判"是也不是"。\n'
            f"- 判定时平衡游戏流畅性和推理挑战性。"
        )

        try:
            llm_resp: LLMResponse = await provider.text_chat(
                prompt=prompt,
                contexts=[],
                func_tool=None,
                image_urls=[],
                system_prompt='你是一个海龟汤推理游戏的助手。你必须严格按照游戏规则回答，只能回答"是"、"否"、"不重要"或"是也不是"，不能添加任何其他内容。',
            )

            valid_responses = {"是", "否", "是也不是", "不重要"}
            reply = llm_resp.completion_text.strip()
            if reply in valid_responses:
                return reply, llm_source
            return (
                "你给ai干宕机了或者有什么其他原因，反正他没好好回复，我也不知道为什么（我努力修过代码了）",
                {"engine": "unavailable"},
            )

        except Exception as e:
            logger.error(f"判断问题失败: {e}")
            return "（判断失败，请重试）", {"engine": "unavailable"}

    # ✅ 生成方向性提示
    def build_allow_list(self, puzzle: str, qa_history: list[dict]) -> list[str]:
        """根据题面和历史问答构建允许在提示中出现的名词列表"""
        import re

        # 汇总文本：题面 + 所有问答
        parts = [puzzle] + [
            f"{item.get('question', '')}{item.get('answer', '')}" for item in qa_history
        ]
        text = "\n".join(parts)

        # 提取连续的中文、字母或数字片段作为候选名词
        tokens = re.findall(r"[A-Za-z0-9\u4e00-\u9fff]+", text)

        allow: list[str] = []
        for token in tokens:
            if not token:
                continue
            # 同义合并：例如“男人A”“嫌疑人A”只保留末尾的大写字母
            m = re.match(r".*([A-Z])$", token)
            if m:
                token = m.group(1)
            if token not in allow:
                allow.append(token)
        return allow

    async def generate_hint(
        self,
        puzzle: str,
        true_answer: str,
        qa_history: list[dict],
        hint_history: list[str],
        allow_list: list[str],
        umo: str | None = None,
    ) -> str:
        """根据本局已记录的问答与提示生成新的方向性提示"""
        provider = self._resolve_provider(self.judge_llm_provider_id, umo)
        if provider is None:
            if self.judge_llm_provider_id:
                return "（未配置判断 LLM，无法提供提示）"
            return "（未配置 LLM，无法提供提示）"

        history_text = "\n".join(
            [f"问：{item['question']}\n答：{item['answer']}" for item in qa_history]
        )
        hint_text = "\n".join(hint_history) if hint_history else "（无）"
        allow_text = ", ".join(allow_list) if allow_list else "（无）"
        prompt = (
            '你是"海龟汤"提示生成器。你知道完整真相（仅供内部推理，严禁外泄）。\n'
            "材料：\n\n"
            f"* 题面：{puzzle}\n"
            f"* 完整真相（不可外泄）：{true_answer}\n"
            f"* 历史问答：{history_text}\n"
            f"* 历史提示：{hint_text}\n"
            f"* 允许名词 allow_list：{allow_text}（只能使用其中名词，不得创造新名词）\n\n"
            "在心中完成：\n\n"
            "1. 从历史问答归纳：已确认/已否定/不重要/部分正确的信息；\n"
            "2. 用以下维度整理：对象/身份、关系、动机、时间、地点、证据、步骤、先后、条件、规则、误解；\n"
            "3. 选择一个“未探索”或“partial 尚缺”的维度，且与历史提示不重复；\n"
            "4. 只使用 allow_list 中的名词与通用词，生成一句**动作化**的下一步提问方向；\n"
            "5. 禁止泄露真相细节，不得同义改写泄露；不得复述已确认内容。\n\n"
            "输出要求（只输出一句）：\n\n"
            "* 格式：关注【<维度>】：<动词 + allow_list名词/通用词>\n"
            "* 字数 ≤ 22（或 ≤ 24），不得添加解释。"
        )

        try:
            llm_resp: LLMResponse = await provider.text_chat(
                prompt=prompt,
                contexts=[],
                func_tool=None,
                image_urls=[],
            )
            text = llm_resp.completion_text.strip()
            if text.startswith("提示："):
                text = text[len("提示：") :]
            return text
        except Exception as e:
            logger.error(f"生成提示失败: {e}")
            return "（生成提示失败，请重试）"

    @filter.command("汤难度")
    async def set_difficulty(self, event: AstrMessageEvent, level: str = ""):
        """设置游戏难度"""
        group_id = event.get_group_id()
        if not group_id:
            yield event.plain_result("此功能只能在群聊中使用")
            return
        if self.game_state.is_game_active(group_id):
            yield event.plain_result("当前有活跃游戏，无法修改难度")
            return
        if level not in self.difficulty_settings:
            options = "/".join(self.difficulty_settings.keys())
            current = self.group_difficulty.get(group_id, "普通")
            yield event.plain_result(f"可选难度：{options}\n当前难度：{current}")
            return
        self.group_difficulty[group_id] = level
        yield event.plain_result(f"难度已设置为 {level}")

    # 🎮 开始游戏指令
    @filter.command("汤")
    async def start_soupai_game(self, event: AstrMessageEvent):
        """开始海龟汤游戏

        使用格式: /汤 [题库类型] [题号]

        参数说明:
        - 题库类型 (可选): network(网络题库), storage(本地存储库), custom(自定义题库)
        - 题号 (可选): 指定题库中的题目索引，从0开始

        示例:
        /汤                    # 使用配置的策略随机获取谜题
        /汤 network           # 从网络题库随机获取谜题
        /汤 storage 5         # 从本地存储库获取第5号谜题
        /汤 custom 2          # 从自定义题库获取第2号谜题
        """
        group_id = event.get_group_id()
        logger.info(f"收到开始游戏指令，群ID: {group_id}")

        if not group_id:
            yield event.plain_result("海龟汤游戏只能在群聊中进行哦~")
            return

        # 检查是否已有活跃游戏
        if self.game_state.is_game_active(group_id):
            logger.info(f"群 {group_id} 已有活跃游戏")
            yield event.plain_result(
                "当前群聊已有活跃的海龟汤游戏，请等待游戏结束或使用 /揭晓 结束当前游戏。"
            )
            return

        # 检查是否正在生成谜题
        if group_id in self.generating_games:
            logger.info(f"群 {group_id} 正在生成谜题，忽略重复请求")
            yield event.plain_result("当前有正在生成的谜题，请稍候...")
            return

        self._ensure_story_storages()

        try:
            # 标记正在生成谜题
            self.generating_games.add(group_id)
            logger.info(f"开始为群 {group_id} 生成谜题")

            # 解析命令参数
            message_content = event.message_str.strip()
            args = message_content.split()[1:]  # 去掉命令本身

            story = None
            source_type = None
            puzzle_index = None

            # 解析参数格式: /汤 <network|storage|custom> <题号>
            # 两个参数都是可选的
            if len(args) >= 1:
                first_arg = args[0].lower()

                # 检查第一个参数是否是题库类型
                if first_arg in ["network", "local", "custom"]:
                    source_type = first_arg

                    # 检查是否有第二个参数（题号）
                    if len(args) >= 2:
                        try:
                            puzzle_index = int(args[1])
                        except ValueError:
                            yield event.plain_result("题号必须是数字")
                            self.generating_games.discard(group_id)
                            return
                else:
                    # 第一个参数不是题库类型，可能是题号
                    try:
                        puzzle_index = int(first_arg)
                        # 使用配置的策略作为默认题库类型
                        strategy = self.puzzle_source_strategy
                        if strategy == "network_first":
                            source_type = "network"
                        elif strategy == "local_first":
                            source_type = "local"
                        elif strategy == "custom_first":
                            source_type = "custom"
                        else:  # random
                            source_type = "current"
                    except ValueError:
                        # 第一个参数既不是题库类型也不是题号，使用默认策略随机获取
                        source_type = "current"
            else:
                # 没有参数，使用配置的策略随机获取
                source_type = "current"

            # 出过的题按会话记录，不同群、私聊各自独立
            session = event.unified_msg_origin

            # 根据解析的参数获取故事
            if puzzle_index is not None:
                # 指定了题号，从特定题库获取
                story = await self.get_story_by_index(
                    source_type, puzzle_index, session
                )
                if not story:
                    yield event.plain_result(
                        f"{source_type}题库中没有第 {puzzle_index} 号题目"
                    )
                    self.generating_games.discard(group_id)
                    return
            else:
                # 没有指定题号，根据策略随机获取
                if source_type == "current":
                    # 使用配置的策略获取随机故事
                    story = await self.get_story_by_strategy(
                        self.puzzle_source_strategy, session
                    )
                else:
                    # 从指定题库获取随机故事
                    storage = self._storage_of(source_type)
                    if storage is None:
                        yield event.plain_result(
                            "题库类型参数错误，请使用 network/local/custom"
                        )
                        self.generating_games.discard(group_id)
                        return
                    story = storage.get_story(session)

            if not story:
                yield event.plain_result("获取谜题失败，请重试")
                self.generating_games.discard(group_id)
                return

            puzzle, answer = story

            # 检查LLM生成是否失败
            if puzzle == "（无法生成题面，请先配置大语言模型）":
                yield event.plain_result(f"生成谜题失败：{answer}")
                self.generating_games.discard(group_id)
                return

            difficulty = self.group_difficulty.get(group_id, "普通")
            diff_conf = self.difficulty_settings.get(
                difficulty, self.difficulty_settings["普通"]
            )

            if self.game_state.start_game(
                group_id,
                puzzle,
                answer,
                difficulty=difficulty,
                question_limit=diff_conf["limit"],
                question_count=0,
                verification_attempts=0,
                accept_levels=diff_conf["accept_levels"],
                hint_limit=diff_conf.get("hint_limit"),
                hint_count=0,
                # 对局以 group_id 为键，但使用记录按 unified_msg_origin 归档，
                # 网页端靠这个字段把两者对上
                session=session,
                started_at=datetime.now().isoformat(),
            ):
                extra = ""
                if diff_conf["limit"] is not None:
                    extra = f"\n模式：{difficulty}（{diff_conf['limit']} 次提问"
                else:
                    extra = f"\n模式：{difficulty}（无限提问"

                hint_limit = diff_conf.get("hint_limit")
                if hint_limit == 0:
                    extra += "，无提示）"
                elif hint_limit is not None:
                    extra += f"，{hint_limit} 次提示）"
                else:
                    extra += "）"

                yield event.plain_result(
                    f"🎮 海龟汤游戏开始！{extra}\n\n📖 题面：{puzzle}\n\n💡 请直接提问或陈述，我会回答：是、否、是也不是\n💡 输入 /提示 可以获取方向性提示\n💡 输入 /验证 <答案> 可以验证答案是否正确\n💡 输入 /揭晓 可以查看完整故事"
                )

                # 启动会话控制
                await self._start_game_session(event, group_id)
            else:
                yield event.plain_result("游戏启动失败，请重试")

            # 移除生成状态，因为故事已经准备完成
            self.generating_games.discard(group_id)
            logger.info(f"群 {group_id} 故事准备完成，移除生成状态")

        except Exception as e:
            logger.error(f"启动游戏失败: {e}")
            # 发生异常时也要移除生成状态
            self.generating_games.discard(group_id)
            logger.info(f"群 {group_id} 启动游戏异常，移除生成状态")
            yield event.plain_result(f"启动游戏时发生错误：{e}")

    # 🔍 揭晓指令
    @filter.command("揭晓")
    async def reveal_answer(self, event: AstrMessageEvent):
        """揭晓答案"""
        group_id = event.get_group_id()

        if not group_id:
            yield event.plain_result("海龟汤游戏只能在群聊中进行哦~")
            return

        # 检查是否有活跃游戏，如果有活跃游戏，说明在会话控制中，不在这里处理
        if self.game_state.is_game_active(group_id):
            # 阻止事件继续传播，避免被会话控制系统重复处理
            event.stop_event()
            return
        game = self.game_state.get_game(group_id)
        if not game:
            yield event.plain_result(
                "当前没有活跃的海龟汤游戏，请使用 /汤 开始新游戏。"
            )
            return

        answer = game["answer"]
        puzzle = game["puzzle"]

        # 发送完整的揭晓信息
        yield event.plain_result(
            f"🎯 海龟汤游戏结束！\n\n📖 题面：{puzzle}\n📖 完整故事：{answer}\n\n感谢参与游戏！"
        )

        # 结束游戏
        self.game_state.end_game(group_id)
        logger.info(f"游戏已结束，群ID: {group_id}")

    # 🎯 游戏会话控制
    async def _start_game_session(self, event: AstrMessageEvent, group_id: str):
        """启动游戏会话控制。答案每次从 game_state 现取，不在这里缓存"""
        try:

            @session_waiter(timeout=self.game_timeout, record_history_chains=False)
            async def game_session_waiter(
                controller: SessionController, event: AstrMessageEvent
            ):
                try:
                    # 从游戏状态获取答案，确保变量可用
                    game = self.game_state.get_game(group_id)
                    if not game:
                        return
                    current_answer = game["answer"]
                    user_input = event.message_str.strip()
                    logger.info(f"会话控制收到消息: '{user_input}'")

                    # 允许在会话中使用 /汤状态 和 /强制结束 指令
                    if user_input in ("/汤状态", "汤状态"):
                        await self._handle_game_status_in_session(event, group_id)
                        return

                    if user_input in ("/强制结束", "强制结束"):
                        await self._handle_force_end_in_session(event, group_id)
                        if not self.game_state.is_game_active(group_id):
                            controller.stop()
                        return

                    normalized_input = user_input.lstrip("/").strip()
                    if normalized_input == "查看":
                        await self._handle_view_history_in_session(event, group_id)
                        controller.keep(timeout=self.game_timeout, reset_timeout=True)
                        return
                    if user_input in ("/提示", "提示"):
                        async for result in self.hint_command(event):
                            await event.send(result)
                        controller.keep(timeout=self.game_timeout, reset_timeout=True)
                        return
                    # 特殊处理 /验证 指令
                    if user_input.startswith("/验证"):
                        import re

                        match = re.match(r"^/验证\s*(.+)$", user_input)
                        if match:
                            user_guess = match.group(1).strip()
                            # 手动调用验证函数
                            await self._handle_verification_in_session(
                                event, user_guess, current_answer
                            )
                            # 检查游戏是否已结束（用户可能猜中了）
                            if not self.game_state.is_game_active(group_id):
                                controller.stop()
                                return
                        else:
                            await event.send(
                                event.plain_result(
                                    "请输入要验证的内容，例如：/验证 他是她的父亲"
                                )
                            )
                        return
                    elif user_input.startswith("验证"):
                        import re

                        match = re.match(r"^验证\s*(.+)$", user_input)
                        if match:
                            user_guess = match.group(1).strip()
                            # 手动调用验证函数
                            await self._handle_verification_in_session(
                                event, user_guess, current_answer
                            )
                            # 检查游戏是否已结束（用户可能猜中了）
                            if not self.game_state.is_game_active(group_id):
                                controller.stop()
                                return
                        else:
                            await event.send(
                                event.plain_result(
                                    "请输入要验证的内容，例如：验证 他是她的父亲"
                                )
                            )
                        return
                    # 特殊处理 /揭晓 指令
                    if user_input == "揭晓":
                        # 获取游戏信息并发送答案
                        game = self.game_state.get_game(group_id)
                        if game:
                            answer = game["answer"]
                            puzzle = game["puzzle"]
                            self.game_state.end_game(group_id)
                            await self._safe_send(
                                event,
                                f"🎯 海龟汤游戏结束！\n\n📖 题面：{puzzle}\n📖 完整故事：{answer}\n\n感谢参与游戏！",
                            )
                        controller.stop()
                        return
                    # Step 1: 检查是否是 /开头的命令，如果是则忽略，让指令处理器处理
                    if user_input.startswith("/"):
                        # 不处理指令，让事件继续传播到指令处理器
                        return
                    # Step 2: 检查是否 @了 bot，只有@bot的消息才触发问答判断
                    if not self._is_at_bot(event):
                        return
                    # Step 3: 是@bot的自然语言提问，触发 LLM 判断
                    game = self.game_state.get_game(group_id)
                    question_limit = game.get("question_limit") if game else None
                    question_count = game.get("question_count", 0) if game else 0
                    if question_limit is not None and question_count >= question_limit:
                        await self._send_reply(
                            event,
                            "❗️提问次数已用完，请使用 /验证 进行猜测。"
                            f"{self._verification_quota_text(game)}",
                        )
                        return

                    # 处理游戏问答消息
                    command_part = user_input.strip()  # 直接使用 plain_text
                    logger.info(f"处理游戏问答消息: '{command_part}'")

                    # 使用 LLM 判断回答（是否问答）
                    logger.info(f"使用 LLM 判断游戏问答: '{command_part}'")
                    reply, judged_by = await self.judge_question(
                        command_part,
                        current_answer,
                        event.unified_msg_origin,
                        game.get("puzzle", "") if game else "",
                    )

                    # 记录提问和回答。judged_by 只给网页端的对局页看，
                    # 群里不显示——玩家没必要知道这一问是谁判的
                    if game is not None:
                        history = game.setdefault("qa_history", [])
                        history.append(
                            {
                                "question": command_part,
                                "answer": reply,
                                "judged_by": judged_by,
                            }
                        )

                    # 更新问题计数
                    if question_limit is not None and game is not None:
                        game["question_count"] = game.get("question_count", 0) + 1
                        # 将判断结果和使用次数合并到一条消息中
                        combined_reply = (
                            f"{reply}（{game['question_count']}/{question_limit}）"
                        )
                        await self._send_reply(event, combined_reply)

                        if game["question_count"] >= question_limit:
                            await self._safe_send(
                                event,
                                "❗️提问次数已用完，将进入验证环节。"
                                f"{self._verification_quota_text(game)}"
                                "请使用 /验证 <推理内容>。",
                            )
                    else:
                        # 如果没有问题限制，只发送判断结果
                        await self._send_reply(event, reply)

                    # 重置超时时间
                    controller.keep(timeout=self.game_timeout, reset_timeout=True)

                except Exception as e:
                    logger.error(f"会话控制内部错误: {e}")
                    # 先结束游戏再发消息，确保发送失败也不会残留状态
                    self.game_state.end_game(group_id)
                    controller.stop()
                    await self._safe_send(event, f"游戏处理过程中发生错误：{e}")

            try:
                # 使用群 ID 限制会话范围，避免多个群并发时互相触发
                await game_session_waiter(
                    event, session_filter=GroupSessionFilter(group_id)
                )
            except TimeoutError:
                game = self.game_state.get_game(group_id)
                if game:
                    true_answer = game["answer"]
                    # 先清状态再发消息：平台掉线时 send 会抛异常，
                    # 若先发后清，本局将永久卡在「进行中」而无法重开
                    self.game_state.end_game(group_id)
                    await self._safe_send(
                        event,
                        f"⏰ 游戏超时！\n\n📖 完整故事：{true_answer}\n\n游戏结束！",
                    )
            except Exception as e:
                logger.error(f"游戏会话错误: {e}")
                self.game_state.end_game(group_id)
                await self._safe_send(event, f"游戏过程中发生错误：{e}")
            finally:
                # 会话监听器已退出，此后没有任何消息会被处理。
                # 无论因何种原因退出，都必须清掉残留状态，否则 /汤 无法重开
                if self.game_state.is_game_active(group_id):
                    logger.warning(f"会话已结束但游戏状态残留，强制清理: {group_id}")
                    self.game_state.end_game(group_id)
        except Exception as e:
            logger.error(f"启动游戏会话失败: {e}")
            self.game_state.end_game(group_id)
            await self._safe_send(event, f"启动游戏会话失败：{e}")

    def _reply_result(self, event: AstrMessageEvent, text: str) -> MessageEventResult:
        """构造回复结果，按 reply_mode 决定是否引用提问者的原消息。

        群里多人同时提问时，裸的「是/不是」分不清在回答谁，引用原消息可消除歧义。
        取不到消息 ID 的平台自动降级为纯文本。
        """
        result = event.plain_result(text)
        if self.reply_mode != "quote":
            return result
        try:
            message_id = getattr(event.message_obj, "message_id", None)
            if message_id:
                result.chain.insert(0, Reply(id=message_id))
        except Exception as e:
            logger.debug(f"构造引用回复失败，降级为纯文本: {e}")
        return result

    async def _send_reply(self, event: AstrMessageEvent, text: str) -> None:
        """在会话中发送一条引用提问者的回复。"""
        await event.send(self._reply_result(event, text))

    def _format_game_status(self, game: dict) -> str:
        """构建游戏状态文本，供 /汤状态 指令与会话内查询共用。"""
        question_count = game.get("question_count", 0)
        question_limit = game.get("question_limit")
        hint_count = game.get("hint_count", 0)
        hint_limit = game.get("hint_limit")

        # 不限次数时不显示 ∞，直接说明（issue #27）
        question_info = (
            f"{question_count}/{question_limit}"
            if question_limit
            else f"{question_count}（不限次数）"
        )
        hint_info = f"{hint_count}/{hint_limit}" if hint_limit else "不可用"

        lines = [
            "🎮 当前有活跃的海龟汤游戏",
            f"📖 题面：{game['puzzle']}",
            f"🎯 难度：{game.get('difficulty', '普通')}",
            f"❓ 提问：{question_info}",
            f"💡 提示：{hint_info}",
        ]
        if self.verification_limit > 0:
            used = game.get("verification_attempts", 0)
            lines.append(f"🔍 验证：{used}/{self.verification_limit}")
        return "\n".join(lines)

    def _verification_quota_text(self, game: dict | None) -> str:
        """描述本局还剩多少次验证机会。"""
        if self.verification_limit <= 0:
            return "验证次数不限。"
        used = game.get("verification_attempts", 0) if game else 0
        remaining = max(self.verification_limit - used, 0)
        return f"你还有 {remaining} 次验证机会。"

    async def _safe_send(self, event: AstrMessageEvent, text: str) -> bool:
        """发送消息并吞掉发送异常，返回是否成功。

        用于结束游戏一类的收尾消息：平台掉线时发送会抛异常，
        绝不能让它阻断后续的状态清理（否则本局会永久卡在「进行中」）。
        """
        try:
            await event.send(event.plain_result(text))
            return True
        except Exception as e:
            logger.error(f"发送消息失败（已忽略，不影响游戏状态清理）: {e}")
            return False

    def _is_at_bot(self, event: AstrMessageEvent) -> bool:
        """检查消息是否@了bot"""

        bot_id = str(event.get_self_id())
        for comp in event.message_obj.message:
            if isinstance(comp, At) and str(comp.qq) == bot_id:
                return True
        return False

    # 每个策略对应的题库尝试顺序，取不到再往后退一档
    _SOURCE_ORDER = {
        "network_first": ("network", "local", "custom"),
        "local_first": ("local", "network", "custom"),
        "custom_first": ("custom", "local", "network"),
    }

    def _storage_of(self, source: str):
        """按名字取题库对象"""
        self._ensure_story_storages()
        return {
            "network": self.online_story_storage,
            "local": self.local_story_storage,
            "custom": self.custom_story_storage,
        }.get(source)

    async def get_story_by_strategy(
        self, strategy: str, session: str = DEFAULT_SESSION
    ) -> tuple[str, str] | None:
        """按策略为指定会话取题，三个题库都取不到时让 LLM 现场生成。"""
        self._ensure_story_storages()

        if strategy == "random":
            strategy = random.choice(list(self._SOURCE_ORDER))
        order = self._SOURCE_ORDER.get(strategy)
        if order is None:
            return None

        for source in order:
            story = self._storage_of(source).get_story(session)
            if story:
                return story
        return await self.generate_story_with_llm()

    async def get_story_by_index(
        self, source_type: str, index: int, session: str = DEFAULT_SESSION
    ) -> tuple[str, str] | None:
        """根据索引获取特定故事

        Args:
            source_type: "network" - 网络题库, "current" - 当前策略题库, "custom" - 自定义题库
            index: 题目索引（从0开始）
            session: 会话标识，取到的题记在这个会话名下

        Returns:
            (puzzle, answer) 或 None
        """
        self._ensure_story_storages()

        if source_type in ("network", "custom", "local"):
            order = (source_type,)
        elif source_type == "current":
            # 把各题库按策略顺序串成一个连续的索引空间
            strategy = self.puzzle_source_strategy
            order = self._SOURCE_ORDER.get(
                strategy, self._SOURCE_ORDER["network_first"]
            )
        else:
            return None

        offset = index
        if offset < 0:
            return None
        for source in order:
            storage = self._storage_of(source)
            if offset < len(storage.stories):
                story = storage.stories[offset]
                storage.mark_used(session, storage.story_id(story, offset))
                logger.info(f"从 {source} 题库获取指定故事，索引: {offset}")
                return story["puzzle"], story["answer"]
            offset -= len(storage.stories)
        return None

    async def _handle_game_status_in_session(
        self, event: AstrMessageEvent, group_id: str
    ):
        """在会话控制中处理游戏状态查询逻辑"""
        try:
            if self.game_state.is_game_active(group_id):
                game = self.game_state.get_game(group_id)
                await event.send(event.plain_result(self._format_game_status(game)))
            else:
                await event.send(
                    event.plain_result(
                        "🎮 当前没有活跃的海龟汤游戏\n💡 使用 /汤 开始新游戏"
                    )
                )

        except Exception as e:
            logger.error(f"会话游戏状态查询失败: {e}")
            await event.send(event.plain_result(f"查询游戏状态时发生错误：{e}"))

    async def _handle_force_end_in_session(
        self, event: AstrMessageEvent, group_id: str
    ):
        """在会话控制中处理强制结束游戏逻辑"""
        try:
            if self.game_state.end_game(group_id):
                await event.send(event.plain_result("✅ 已强制结束当前海龟汤游戏"))
            else:
                await event.send(event.plain_result("❌ 当前没有活跃的游戏需要结束"))

        except Exception as e:
            logger.error(f"会话强制结束失败: {e}")
            await event.send(event.plain_result(f"强制结束游戏时发生错误：{e}"))

    async def _handle_view_history_in_session(
        self, event: AstrMessageEvent, group_id: str
    ):
        """在会话控制中处理查看历史记录逻辑"""
        try:
            game = self.game_state.get_game(group_id)
            if not game:
                await event.send(event.plain_result("无法获取游戏状态"))
                return

            history = game.get("qa_history", [])

            if not history:
                await event.send(event.plain_result("目前还没有人提问哦~"))
                return

            lines = ["📋 提问记录："]
            for idx, item in enumerate(history, 1):
                lines.append(f"{idx}. 问：{item['question']}\n   答：{item['answer']}")

            response = "\n".join(lines)
            await event.send(event.plain_result(response))

        except Exception as e:
            logger.error(f"会话查看历史失败: {e}")
            await event.send(event.plain_result(f"查看历史记录时发生错误：{e}"))

    async def _build_hint_result(
        self, event: AstrMessageEvent, group_id: str
    ) -> MessageEventResult | None:
        """生成提示结果，供指令或会话控制调用"""
        if not group_id:
            return event.plain_result("提示功能只能在群聊中使用")

        game = self.game_state.get_game(group_id)
        if not game:
            return event.plain_result("当前没有活跃的海龟汤游戏")

        hint_limit = game.get("hint_limit")
        hint_count = game.get("hint_count", 0)
        if hint_limit == 0:
            return event.plain_result("当前难度不可使用提示")
        if hint_limit is not None and hint_count >= hint_limit:
            return event.plain_result("提示次数已用完")

        qa_history = game.get("qa_history", [])
        if not qa_history:
            return event.plain_result("请先进行提问后再请求提示")

        hint_history = game.get("hint_history", [])
        allow_list = self.build_allow_list(game["puzzle"], qa_history)

        hint = await self.generate_hint(
            game["puzzle"],
            game["answer"],
            qa_history,
            hint_history,
            allow_list,
            event.unified_msg_origin,
        )
        game["hint_count"] = hint_count + 1
        game["hint_history"] = hint_history + [hint]
        suffix = ""
        if hint_limit is not None:
            suffix = f"（{game['hint_count']}/{hint_limit}）"
        return event.plain_result(f"提示：{hint}{suffix}")

    # 未猜中时给出的固定引导语。不使用 LLM 生成的评价，
    # 因为它为了说明"哪里偏了"必然要引用答案细节，等于剧透（issue #28）
    _LEVEL_FEEDBACK = {
        "完全还原": "推理方向正确。",
        "核心推理正确": "已经摸到主干了，再补一补细节。",
        "部分正确": "抓到了一些线索，但整体因果链还没串起来。",
        "基本不符": "方向偏了，换个角度重新想想。",
        "验证失败": "这次没能判定，请换种表述再试一次。",
    }

    async def _handle_verification_in_session(
        self, event: AstrMessageEvent, user_guess: str, answer: str
    ):
        """在会话控制中处理验证逻辑"""
        try:
            group_id = event.get_group_id()
            game = self.game_state.get_game(group_id) if group_id else None

            # 先检查次数：验证机会全程有限，不论提问次数是否用完（issue #22）
            if game is not None and self.verification_limit > 0:
                used = game.get("verification_attempts", 0)
                if used >= self.verification_limit:
                    await self._send_reply(
                        event,
                        f"❗️验证次数已用完（{used}/{self.verification_limit}），"
                        f"请继续提问或使用 /揭晓 查看答案。",
                    )
                    return

            # 验证用户推理
            result = await self.verify_user_guess(
                user_guess, answer, event.unified_msg_origin
            )

            accept_levels = (
                game.get("accept_levels", ["完全还原", "核心推理正确"])
                if game
                else ["完全还原", "核心推理正确"]
            )
            is_correct = result.level in accept_levels

            if is_correct:
                # 猜中，游戏结束，此时公布评价和完整故事已无剧透风险
                if group_id:
                    self.game_state.end_game(group_id)
                await self._safe_send(
                    event,
                    f"等级：{result.level}\n评价：{result.comment}\n\n"
                    f"🎉 恭喜！你猜中了！\n\n📖 完整故事：{answer}\n\n游戏结束！",
                )
                return

            # 未猜中：只回等级和固定引导语，不回 LLM 评价
            if game is None:
                await self._send_reply(
                    event,
                    f"等级：{result.level}\n"
                    f"{self._LEVEL_FEEDBACK.get(result.level, '继续加油。')}",
                )
                return

            game["verification_attempts"] = game.get("verification_attempts", 0) + 1
            feedback = self._LEVEL_FEEDBACK.get(result.level, "继续加油。")

            if self.verification_limit <= 0:
                await self._send_reply(event, f"等级：{result.level}\n{feedback}")
                return

            remaining = self.verification_limit - game["verification_attempts"]
            question_limit = game.get("question_limit")
            questions_exhausted = (
                question_limit is not None
                and game.get("question_count", 0) >= question_limit
            )

            if remaining > 0:
                await self._send_reply(
                    event,
                    f"等级：{result.level}\n{feedback}\n"
                    f"❌ 验证未通过，你还有 {remaining} 次机会。",
                )
            elif questions_exhausted:
                # 提问和验证都用尽，本局无路可走，揭晓答案收场
                self.game_state.end_game(group_id)
                await self._safe_send(
                    event,
                    f"等级：{result.level}\n❌ 验证机会已用尽。\n\n"
                    f"📖 完整故事：{answer}\n\n游戏结束！",
                )
            else:
                # 验证用尽但提问还有余量，游戏继续，不能直接揭晓答案
                await self._send_reply(
                    event,
                    f"等级：{result.level}\n{feedback}\n"
                    "❌ 验证次数已用完，请继续提问或使用 /揭晓 查看答案。",
                )

        except Exception as e:
            logger.error(f"会话验证失败: {e}")
            await self._safe_send(event, f"验证过程中发生错误：{e}")

    # 📊 游戏状态查询
    @filter.command("汤状态")
    async def check_game_status(self, event: AstrMessageEvent):
        """查看当前游戏状态"""
        group_id = event.get_group_id()

        if not group_id:
            yield event.plain_result("此功能只能在群聊中使用")
            return

        if self.game_state.is_game_active(group_id):
            game = self.game_state.get_game(group_id)
            yield event.plain_result(self._format_game_status(game))
        else:
            yield event.plain_result(
                "🎮 当前没有活跃的海龟汤游戏\n💡 使用 /汤 开始新游戏"
            )

    @filter.command("查看")
    async def view_question_history(self, event: AstrMessageEvent):
        """查看当前已提问的问题及回答"""
        group_id = event.get_group_id()

        if not group_id:
            yield event.plain_result("此功能只能在群聊中使用")
            return
        if not self.game_state.is_game_active(group_id):
            yield event.plain_result("当前没有活跃的海龟汤游戏")
            return
        game = self.game_state.get_game(group_id)
        history = game.get("qa_history", []) if game else []
        if not history:
            yield event.plain_result("目前还没有人提问哦~")
            return
        lines = ["📋 提问记录："]
        for idx, item in enumerate(history, 1):
            lines.append(f"{idx}. 问：{item['question']}\n   答：{item['answer']}")
        yield event.plain_result("\n".join(lines))

    # 🆘 强制结束游戏（管理员功能）
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("强制结束")
    async def force_end_game(self, event: AstrMessageEvent):
        """强制结束当前游戏（仅管理员）"""
        group_id = event.get_group_id()

        if not group_id:
            yield event.plain_result("此功能只能在群聊中使用")
            return

        if self.game_state.end_game(group_id):
            yield event.plain_result("✅ 已强制结束当前海龟汤游戏")
        else:
            yield event.plain_result("❌ 当前没有活跃的游戏需要结束")

    # 📚 备用故事管理指令
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("备用开始")
    async def start_backup_generation(self, event: AstrMessageEvent):
        """开始生成备用故事（仅管理员）"""

        if self.auto_generating:
            yield event.plain_result("⚠️ 备用故事生成已在运行中")
            return

        # 检查存储库是否已满
        self._ensure_story_storages()
        storage_info = self.local_story_storage.get_storage_info()
        if storage_info["available"] <= 0:
            yield event.plain_result("⚠️ 存储库已满，无法生成更多故事")
            return

        self.auto_generating = True
        asyncio.create_task(self._auto_generate_loop())
        yield event.plain_result(
            f"✅ 开始生成备用故事，存储库状态: {storage_info['total']}/{storage_info['max_size']}"
        )

    # 🔒 全局指令拦截器 - 当正在生成时提醒用户
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def global_command_interceptor(self, event: AstrMessageEvent):
        """全局指令拦截器，当正在生成备用故事时提醒用户"""
        # 检查是否有活跃游戏，如果有活跃游戏，不在这里处理
        group_id = event.get_group_id()
        if group_id and self.game_state.is_game_active(group_id):
            # 有活跃游戏，让会话控制处理
            return

        # 如果正在生成备用故事，且不是 /备用结束 指令，则提醒用户
        if self.auto_generating:
            user_input = event.message_str.strip()
            # 只拦截非本插件的指令，避免阻断自己的指令
            if (
                user_input.startswith("/")
                and not user_input.startswith("/备用结束")
                and not user_input.startswith("/汤")
                and not user_input.startswith("/揭晓")
                and not user_input.startswith("/验证")
                and not user_input.startswith("/汤状态")
                and not user_input.startswith("/强制结束")
                and not user_input.startswith("/备用开始")
                and not user_input.startswith("/备用状态")
                and not user_input.startswith("/汤配置")
                and not user_input.startswith("/重置题库")
                and not user_input.startswith("/题库详情")
                and not user_input.startswith("/查看")
                and not user_input.startswith("/提示")
            ):
                yield event.plain_result(
                    "⚠️ 系统正在生成备用故事，请稍后再试或使用 /备用结束 停止生成"
                )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("备用结束")
    async def stop_backup_generation(self, event: AstrMessageEvent):
        """停止生成备用故事（仅管理员）"""

        if not self.auto_generating:
            yield event.plain_result("⚠️ 备用故事生成未在运行")
            return

        self.auto_generating = False
        yield event.plain_result("✅ 已停止生成备用故事，正在完成当前生成...")

    @filter.command("备用状态")
    async def check_backup_status(self, event: AstrMessageEvent):
        """查看备用故事状态"""
        self._ensure_story_storages()
        storage_info = self.local_story_storage.get_storage_info()
        online_info = self.online_story_storage.get_storage_info()
        status = "🟢 运行中" if self.auto_generating else "🔴 已停止"

        # 检查存储库是否已满
        storage_full_warning = ""
        if storage_info["available"] <= 0:
            storage_full_warning = "\n⚠️ 本地存储库已满，自动生成已停止"

        message = (
            f"📚 备用故事状态：\n"
            f"• 生成状态：{status}\n"
            f"• 本地存储库：{storage_info['total']}/{storage_info['max_size']}\n"
            f"• 已使用题目：{storage_info['used']}\n"
            f"• 剩余题目：{storage_info['remaining']}\n"
            f"• 可用空间：{storage_info['available']}\n"
            f"• 网络题库：{online_info['total']} 个 (已用: {online_info['used']}, 剩余: {online_info['available']})\n"
            f"• 自动生成时间：{self.auto_generate_start}:00-{self.auto_generate_end}:00{storage_full_warning}"
        )

        yield event.plain_result(message)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("重置题库")
    async def reset_story_storage(self, event: AstrMessageEvent, scope: str = ""):
        """重置题库使用记录（仅管理员）。加 all 参数可重置全部会话"""

        self._ensure_story_storages()

        storages = (
            self.online_story_storage,
            self.local_story_storage,
            self.custom_story_storage,
        )
        all_sessions = scope.strip().lower() in ("all", "全部")
        session = None if all_sessions else event.unified_msg_origin
        for storage in storages:
            storage.reset_usage(session)

        if all_sessions:
            yield event.plain_result(
                "✅ 已重置全部会话的题库使用记录，所有题目重新可用"
            )
        else:
            yield event.plain_result(
                "✅ 已重置本会话的题库使用记录，所有题目在这里重新可用\n"
                "💡 其他群/私聊的记录不受影响，需要全部重置请用 /重置题库 all"
            )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("题库详情")
    async def show_storage_details(self, event: AstrMessageEvent):
        """查看题库详细使用记录（仅管理员）"""

        # 确保题库已初始化
        self._ensure_story_storages()

        session = event.unified_msg_origin
        lines = ["📊 题库使用记录（本会话）：", ""]

        for label, storage in (
            ("🌐 网络题库", self.online_story_storage),
            ("💾 本地存储库", self.local_story_storage),
            ("✏️ 自定义题库", self.custom_story_storage),
        ):
            here = storage.get_storage_info(session)
            total = here["total"]
            hidden = here.get("hidden", 0)
            usable = total - hidden
            rate = (here["used"] / usable * 100) if usable > 0 else 0.0
            all_used = storage.get_usage_info()["used"]
            lines.append(label + "：")
            lines.append(
                f"• 总数：{total} 个" + (f"（屏蔽 {hidden} 个）" if hidden else "")
            )
            lines.append(f"• 本会话已出：{here['used']} 个（{rate:.0f}%）")
            lines.append(f"• 本会话剩余：{max(usable - here['used'], 0)} 个")
            lines.append(f"• 所有会话共出过：{all_used} 个")
            lines.append("")

        sessions = set()
        for storage in (
            self.online_story_storage,
            self.local_story_storage,
            self.custom_story_storage,
        ):
            sessions.update(storage.sessions())
        lines.append(f"🗂 共有 {len(sessions)} 个会话出过题，各自独立计数")

        yield event.plain_result("\n".join(lines))

    @filter.command("提示")
    async def hint_command(self, event: AstrMessageEvent):
        """根据当前所有提问记录提供方向性提示"""
        result = await self._build_hint_result(event, event.get_group_id())
        if result:
            yield result

    # 🔍 验证指令（仅在非游戏会话时处理）
    @filter.command("验证")
    async def verify_user_guess_command(self, event: AstrMessageEvent, user_guess: str):
        """验证用户推理（仅在非游戏会话时处理）"""
        group_id = event.get_group_id()

        if not group_id:
            yield event.plain_result("验证功能只能在群聊中使用")
            return

        # 检查是否有活跃游戏，如果有活跃游戏，说明在会话控制中，不在这里处理
        if self.game_state.is_game_active(group_id):
            # 阻止事件继续传播，避免被会话控制系统重复处理
            event.stop_event()
            return
        # 只有在没有活跃游戏时才在这里处理（用于游戏外的验证）
        yield event.plain_result("当前没有活跃的海龟汤游戏，请使用 /汤 开始新游戏")

    # ⚙️ 查看当前配置
    @filter.command("汤配置")
    async def show_config(self, event: AstrMessageEvent):
        """查看当前插件配置"""

        # 确保题库已初始化
        self._ensure_story_storages()

        local_info = self.local_story_storage.get_storage_info()
        online_info = self.online_story_storage.get_storage_info()

        # 获取策略的中文描述
        strategy_names = {
            "network_first": "优先网络题库→本地存储库→LLM生成",
            "local_first": "优先本地存储库→网络题库→LLM生成",
            "custom_first": "优先自定义题库→本地存储库→LLM生成",
            "random": "随机从网络、本地或自定义题库中选择",
        }
        strategy_name = strategy_names.get(
            self.puzzle_source_strategy, self.puzzle_source_strategy
        )

        # 检查存储库是否已满
        storage_full_warning = ""
        if local_info["available"] <= 0:
            storage_full_warning = "\n⚠️ 本地存储库已满，自动生成已停止"

        if self.judge_engine == "jev":
            if self.judge_fallback_to_llm:
                judge_info = (
                    f"Jev {self.jev_model}"
                    f"（置信度<{self.jev_judge_min_confidence:g} 时改用 LLM）"
                )
            else:
                judge_info = f"Jev {self.jev_model}（不兜底）"
        elif str(self.config.get("judge_engine", "")).lower() == "jev":
            judge_info = "LLM（已选 Jev 但未填 API Key）"
        else:
            judge_info = "LLM"

        config_info = (
            f"⚙️ 海龟汤插件配置：\n"
            f"• 生成谜题 LLM：{self.generate_llm_provider_id or '默认'}\n"
            f"• 判断问答 LLM：{self.judge_llm_provider_id or '默认'}\n"
            f"• 游戏超时：{self.game_timeout} 秒\n"
            f"• 网络题库：{online_info['total']} 个谜题 (已用: {online_info['used']}, 剩余: {online_info['available']})\n"
            f"• 本地存储库：{local_info['total']}/{local_info['max_size']} (已用: {local_info['used']}, 剩余: {local_info['remaining']})\n"
            f"• 自动生成时间：{self.auto_generate_start}:00-{self.auto_generate_end}:00\n"
            f"• 谜题来源策略：{strategy_name}\n"
            f"• 提问判定引擎：{judge_info}{storage_full_warning}"
        )
        yield event.plain_result(config_info)

    # ➕ 添加自定义海龟汤
    @filter.command("添加海龟汤")
    async def add_custom_soupai(self, event: AstrMessageEvent, content: str):
        """添加自定义海龟汤故事，格式: /添加海龟汤 <汤面>|<汤底>"""

        # 确保自定义存储库已初始化
        self._ensure_story_storages()

        # 解析内容格式: 汤面|汤底
        if "|" not in content:
            yield event.plain_result(
                "❌ 格式错误！请使用格式: /添加海龟汤 <汤面>|<汤底>"
            )
            return

        puzzle, answer = content.split("|", 1)
        puzzle = puzzle.strip()
        answer = answer.strip()

        if not puzzle or not answer:
            yield event.plain_result("❌ 汤面和汤底都不能为空！")
            return

        # 添加故事到自定义存储库
        success = self.custom_story_storage.add_story(puzzle, answer)

        if success:
            # 获取添加后的故事索引
            story_index = len(self.custom_story_storage.stories) - 1
            yield event.plain_result(
                f"✅ 添加成功！海龟汤编号: {story_index}\n\n"
                f"📖 汤面: {puzzle}\n"
                f"📖 汤底: {answer}"
            )
        else:
            yield event.plain_result("❌ 添加失败，请重试")
