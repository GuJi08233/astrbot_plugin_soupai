"""Regression tests for checked writes and configurable story generation."""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call, patch

import test_judging as judging_runtime


class GenerationTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        """Reuse the existing isolated AstrBot import fixture."""
        judging_runtime.JudgingTests.setUpClass()
        cls.module = judging_runtime.JudgingTests.module

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="soupai-generation-tests-")
        self.addCleanup(temporary.cleanup)
        self.data_path = Path(temporary.name)
        self.plugin = self.module.SoupaiPlugin.__new__(self.module.SoupaiPlugin)
        self.plugin.config = {}
        self.plugin._story_write_lock = asyncio.Lock()
        self.local = self.module.LocalSoupaiStorage(
            self.data_path / "local.json", max_size=3, data_path=self.data_path
        )
        self.custom = self.module.CustomSoupaiStorage(
            self.data_path / "custom.json", data_path=self.data_path
        )
        self.storages = {"local": self.local, "custom": self.custom}
        self.plugin._storage_of = Mock(side_effect=self.storages.get)
        self.report = {
            "duplicate": False,
            "matches": [],
            "annotation": None,
            "method": "exact",
            "warnings": [],
        }
        self.plugin.story_catalog = SimpleNamespace(
            check=AsyncMock(return_value=self.report),
            forget=Mock(),
            remember=Mock(),
            moderate=AsyncMock(return_value={"passed": True, "reason": "无明显问题"}),
        )
        self.plugin.content_moderation_enabled = True
        self.plugin.generate_llm_provider_id = "generator"
        self.provider = SimpleNamespace(
            text_chat=AsyncMock(
                return_value=SimpleNamespace(
                    completion_text="题面：医生熄灭了走廊的灯。\n答案：检查室需要黑暗环境。"
                )
            )
        )
        self.plugin._resolve_provider = Mock(return_value=self.provider)

    async def test_both_writable_banks_use_the_same_check_before_saving(self):
        for source, storage in self.storages.items():
            with self.subTest(source=source):
                saved = await self.plugin.save_story_checked(
                    source, " 新题面 ", " 新汤底 ", umo="test-session"
                )

                self.assertEqual(storage.stories[-1]["id"], saved["id"])
                self.assertEqual(saved["puzzle"], "新题面")
                self.assertEqual(saved["answer"], "新汤底")
                self.assertEqual(saved["check"], self.report)
                self.assertEqual(
                    saved["check"]["moderation"],
                    {"passed": True, "reason": "无明显问题"},
                )
                self.plugin.story_catalog.check.assert_awaited_with(
                    "新题面", "新汤底", exclude=None, umo="test-session"
                )
        self.assertEqual(self.plugin.story_catalog.check.await_count, 2)

    async def test_duplicate_does_not_evict_or_rewrite_full_local_storage(self):
        self.local.max_size = 1
        self.local.add_story("旧题面", "旧汤底")
        old_stories = json.loads((self.data_path / "local.json").read_text("utf-8"))
        self.report.update(
            duplicate=True, matches=[{"source": "network", "id": "network-1"}]
        )

        with self.assertRaises(self.module.DuplicateStoryError):
            await self.plugin.save_story_checked("local", "重复题面", "重复汤底")

        self.assertEqual(self.local.stories, old_stories)
        self.assertEqual(
            json.loads((self.data_path / "local.json").read_text("utf-8")),
            old_stories,
        )
        self.plugin.story_catalog.forget.assert_not_called()

    async def test_saved_story_returns_warning_if_annotation_cache_write_fails(self):
        self.report["annotation"] = {"theme": "医院"}
        self.plugin.story_catalog.remember.side_effect = ValueError(
            "Synthetic annotation persistence failure"
        )

        saved = await self.plugin.save_story_checked("local", "新题面", "新汤底")

        stored = json.loads((self.data_path / "local.json").read_text("utf-8"))
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]["id"], saved["id"])
        self.assertEqual(stored[0]["puzzle"], "新题面")
        self.assertEqual(self.local.stories, stored)
        self.assertFalse(saved["check"]["duplicate"])
        self.assertTrue(saved["check"]["warnings"])
        self.plugin.story_catalog.remember.assert_called_once()

    async def test_edit_excludes_its_own_id_and_keeps_new_annotation(self):
        self.custom.add_story("原题面", "原汤底")
        story_id = self.custom.stories[0]["id"]
        annotation = {"theme": "医院"}
        self.report["annotation"] = annotation

        saved = await self.plugin.save_story_checked(
            "custom", "修改题面", "修改汤底", story_id=story_id, umo="edit-session"
        )

        self.assertEqual(saved["id"], story_id)
        self.assertEqual(len(self.custom.stories), 1)
        self.assertEqual(self.custom.stories[0]["answer"], "修改汤底")
        self.plugin.story_catalog.check.assert_awaited_once_with(
            "修改题面", "修改汤底", exclude=("custom", story_id), umo="edit-session"
        )
        self.plugin.story_catalog.remember.assert_called_once_with(
            "custom", story_id, "修改题面", "修改汤底", annotation
        )

    async def test_failed_initial_write_does_not_report_or_retain_a_saved_story(self):
        for source, storage in self.storages.items():
            with self.subTest(source=source):
                with patch.object(
                    Path, "open", side_effect=PermissionError("Read only")
                ):
                    with self.assertRaisesRegex(ValueError, "保存失败"):
                        await self.plugin.save_story_checked(
                            source, "New puzzle", "New answer"
                        )
                self.assertEqual(storage.stories, [])
                self.assertFalse(Path(storage.storage_file).exists())
        self.plugin.story_catalog.remember.assert_not_called()

    async def test_failed_replacement_preserves_full_bank_and_usage(self):
        self.local.max_size = 1
        for source, storage in self.storages.items():
            with self.subTest(source=source):
                storage.add_story("Original puzzle", "Original answer")
                old_story = dict(storage.stories[0])
                storage.mark_used("group", old_story["id"])
                before = Path(storage.storage_file).read_bytes()
                with patch.object(Path, "replace", side_effect=OSError("Disk failure")):
                    with self.assertRaisesRegex(ValueError, "保存失败"):
                        await self.plugin.save_story_checked(
                            source, "New puzzle", "New answer"
                        )
                self.assertEqual(storage.stories, [old_story])
                self.assertEqual(storage.used_ids("group"), {old_story["id"]})
                self.assertEqual(Path(storage.storage_file).read_bytes(), before)
                self.assertEqual(list(self.data_path.glob("*.tmp")), [])
        self.plugin.story_catalog.forget.assert_not_called()
        self.plugin.story_catalog.remember.assert_not_called()

    async def test_partial_serialization_does_not_truncate_bank_or_commit_edit(self):
        def fail_after_partial_write(value, stream, **kwargs):
            stream.write('[{"partial":')
            raise OSError("Disk full")

        for source, storage in self.storages.items():
            with self.subTest(source=source):
                storage.add_story("Original puzzle", "Original answer")
                original = dict(storage.stories[0])
                before = Path(storage.storage_file).read_bytes()
                with patch.object(
                    self.module.json, "dump", side_effect=fail_after_partial_write
                ):
                    with self.assertRaisesRegex(ValueError, "保存失败"):
                        await self.plugin.save_story_checked(
                            source,
                            "Edited puzzle",
                            "Edited answer",
                            story_id=original["id"],
                        )
                self.assertEqual(storage.stories, [original])
                self.assertEqual(Path(storage.storage_file).read_bytes(), before)
                self.assertEqual(list(self.data_path.glob("*.tmp")), [])
        self.plugin.story_catalog.remember.assert_not_called()

    def test_failed_id_migration_keeps_loaded_stories(self):
        for storage_type in (
            self.module.LocalSoupaiStorage,
            self.module.CustomSoupaiStorage,
        ):
            with self.subTest(storage_type=storage_type.__name__):
                path = self.data_path / "legacy.json"
                original = [{"puzzle": "Legacy puzzle", "answer": "Legacy answer"}]
                path.write_text(json.dumps(original), encoding="utf-8")
                with patch.object(Path, "replace", side_effect=OSError("Read only")):
                    storage = storage_type(path, data_path=self.data_path)
                self.assertEqual(len(storage.stories), 1)
                self.assertEqual(storage.stories[0]["puzzle"], "Legacy puzzle")
                self.assertEqual(json.loads(path.read_text("utf-8")), original)

    async def test_deleted_story_is_not_recreated_after_check_returns(self):
        self.custom.add_story("原题面", "原汤底")
        story_id = self.custom.stories[0]["id"]

        async def delete_during_check(*args, **kwargs):
            self.custom.stories.clear()
            return self.report

        self.plugin.story_catalog.check.side_effect = delete_during_check

        with self.assertRaises(ValueError):
            await self.plugin.save_story_checked(
                "custom", "修改题面", "修改汤底", story_id=story_id
            )

        self.assertEqual(self.custom.stories, [])
        self.plugin.story_catalog.remember.assert_not_called()

    async def test_concurrent_cross_bank_writes_cannot_both_commit_same_story(self):
        async def check_current_stories(puzzle, answer, **kwargs):
            existing = [
                {"source": source, "id": story["id"]}
                for source, storage in self.storages.items()
                for story in storage.stories
                if story["puzzle"] == puzzle and story["answer"] == answer
            ]
            # Yield after taking the snapshot to expose a missing write lock.
            await asyncio.sleep(0)
            return {**self.report, "duplicate": bool(existing), "matches": existing}

        self.plugin.story_catalog.check.side_effect = check_current_stories
        results = await asyncio.gather(
            self.plugin.save_story_checked("local", "同一题面", "同一汤底"),
            self.plugin.save_story_checked("custom", "同一题面", "同一汤底"),
            return_exceptions=True,
        )

        self.assertEqual(sum(isinstance(result, dict) for result in results), 1)
        self.assertEqual(
            sum(
                isinstance(result, self.module.DuplicateStoryError)
                for result in results
            ),
            1,
        )
        self.assertEqual(
            sum(len(storage.stories) for storage in self.storages.values()), 1
        )

    async def test_readonly_and_invalid_inputs_never_start_a_check(self):
        for source, puzzle, answer in (
            ("network", "题面", "汤底"),
            ("local", " ", "汤底"),
            ("custom", "题面", ""),
        ):
            with self.subTest(source=source, puzzle=puzzle):
                with self.assertRaises(ValueError):
                    await self.plugin.save_story_checked(source, puzzle, answer)

        self.plugin.story_catalog.check.assert_not_awaited()

    async def test_generation_passes_theme_ideas_and_session_to_checked_write(self):
        saved = await self.plugin.generate_and_store_story(
            theme="医院", ideas="用停电制造误会", umo="generation-session"
        )

        self.assertEqual(saved["puzzle"], "医生熄灭了走廊的灯。")
        self.assertEqual(len(self.local.stories), 1)
        self.plugin._resolve_provider.assert_called_once_with(
            "generator", "generation-session"
        )
        prompt = self.provider.text_chat.await_args.kwargs["prompt"]
        self.assertIn("医院", prompt)
        self.assertIn("用停电制造误会", prompt)
        self.plugin.story_catalog.check.assert_awaited_once_with(
            "医生熄灭了走廊的灯。",
            "检查室需要黑暗环境。",
            exclude=None,
            umo="generation-session",
        )

    async def test_duplicate_generation_stops_after_three_attempts_without_writing(
        self,
    ):
        self.report.update(
            duplicate=True, matches=[{"source": "network", "id": "existing"}]
        )
        self.plugin.generate_story_with_llm = AsyncMock(
            side_effect=[
                ("旧题一", "旧底一"),
                ("旧题二", "旧底二"),
                ("旧题三", "旧底三"),
            ]
        )

        with self.assertRaises(self.module.DuplicateStoryError):
            await self.plugin.generate_and_store_story("医院", "创作想法", "session")

        self.assertEqual(
            self.plugin.generate_story_with_llm.await_args_list,
            [
                call("session", "医院", "创作想法"),
                call(
                    "session", "医院", "创作想法", avoid_stories=[("旧题一", "旧底一")]
                ),
                call(
                    "session",
                    "医院",
                    "创作想法",
                    avoid_stories=[("旧题一", "旧底一"), ("旧题二", "旧底二")],
                ),
            ],
        )
        self.assertEqual(self.plugin.story_catalog.check.await_count, 3)
        self.assertEqual(self.local.stories, [])

    async def test_duplicate_generation_can_retry_once_then_commit(self):
        duplicate = {
            **self.report,
            "duplicate": True,
            "matches": [{"source": "custom", "id": "old-story"}],
        }
        self.plugin.story_catalog.check.side_effect = [duplicate, self.report]
        self.plugin.generate_story_with_llm = AsyncMock(
            side_effect=[("旧题面", "旧汤底"), ("新题面", "新汤底")]
        )

        saved = await self.plugin.generate_and_store_story()

        self.assertEqual(saved["puzzle"], "新题面")
        self.assertEqual(self.plugin.generate_story_with_llm.await_count, 2)
        self.assertEqual(len(self.local.stories), 1)

    async def test_duplicate_retry_adds_rejected_story_to_model_prompt(self):
        duplicate = {
            **self.report,
            "duplicate": True,
            "matches": [{"source": "network", "id": "existing"}],
        }
        self.plugin.story_catalog.check.side_effect = [duplicate, self.report]
        old_puzzle = "护士在下班前关闭了蓝色指示灯。"
        old_answer = "蓝灯表示病房空闲，护士关灯代表病床终于分配成功。"
        self.provider.text_chat.side_effect = [
            SimpleNamespace(completion_text=f"题面：{old_puzzle}\n答案：{old_answer}"),
            SimpleNamespace(
                completion_text="题面：新生成的题面。\n答案：另一个核心因果。"
            ),
        ]

        saved = await self.plugin.generate_and_store_story(
            "医院", "围绕灯光设计", "session"
        )

        first_prompt, retry_prompt = [
            request.kwargs["prompt"]
            for request in self.provider.text_chat.await_args_list
        ]
        self.assertNotIn(old_answer, first_prompt)
        self.assertIn(old_puzzle, retry_prompt)
        self.assertIn(old_answer, retry_prompt)
        self.assertIn("仅用于避重", retry_prompt)
        self.assertIn("更换核心因果链", retry_prompt)
        self.assertIn("关键反转", retry_prompt)
        self.assertIn("围绕灯光设计", retry_prompt)
        self.assertEqual(saved["puzzle"], "新生成的题面。")
        self.assertEqual(len(self.local.stories), 1)

    async def test_failed_duplicate_check_is_not_retried_or_written(self):
        self.plugin.story_catalog.check.side_effect = ValueError(
            "Synthetic check failure"
        )
        self.plugin.generate_story_with_llm = AsyncMock(return_value=("题面", "汤底"))

        with self.assertRaises(ValueError):
            await self.plugin.generate_and_store_story()

        self.plugin.generate_story_with_llm.assert_awaited_once()
        self.assertEqual(self.local.stories, [])

    async def test_failed_or_malformed_generation_is_never_written(self):
        for failure in (RuntimeError("Synthetic generation failure"), "格式不正确"):
            with self.subTest(failure=type(failure).__name__):
                self.provider.text_chat.reset_mock()
                self.provider.text_chat.side_effect = (
                    failure if isinstance(failure, Exception) else None
                )
                self.provider.text_chat.return_value = SimpleNamespace(
                    completion_text=failure
                )

                with self.assertRaises(ValueError):
                    await self.plugin.generate_and_store_story()

                self.provider.text_chat.assert_awaited_once()
                self.plugin.story_catalog.check.assert_not_awaited()
                self.assertEqual(self.local.stories, [])

    def test_prompt_uses_config_defaults_and_explicit_overrides(self):
        self.plugin.config.update(
            generation_theme="默认医院题材", generation_ideas="默认创作参考"
        )

        default_prompt = self.plugin._build_puzzle_prompt()
        explicit_prompt = self.plugin._build_puzzle_prompt("航海题材", "灯塔的创作想法")

        self.assertIn("默认医院题材", default_prompt)
        self.assertIn("默认创作参考", default_prompt)
        self.assertIn("航海题材", explicit_prompt)
        self.assertIn("灯塔的创作想法", explicit_prompt)
        self.assertNotIn("默认医院题材", explicit_prompt)
        self.assertNotIn("默认创作参考", explicit_prompt)

    async def test_oversized_creative_input_is_rejected_before_model_call(self):
        for theme, ideas in (("题" * 121, ""), ("医院", "想" * 2001)):
            with self.subTest(theme_length=len(theme), ideas_length=len(ideas)):
                with self.assertRaises(ValueError):
                    await self.plugin.generate_and_store_story(theme, ideas)

        self.provider.text_chat.assert_not_awaited()
        self.plugin.story_catalog.check.assert_not_awaited()
        self.assertEqual(self.local.stories, [])

    async def test_rejected_story_never_reaches_the_bank(self):
        """群聊里可能有未成年人，没通过内容审核的题一道都不能入库。"""
        self.plugin.story_catalog.moderate = AsyncMock(
            return_value={"passed": False, "reason": "含有色情内容"}
        )

        with self.assertRaises(ValueError) as caught:
            await self.plugin.save_story_checked("custom", "新题面", "新汤底")

        self.assertIn("内容审核", str(caught.exception))
        self.assertEqual(self.custom.stories, [])
        # 审核没通过就不该再花一次标注/查重的模型调用
        self.plugin.story_catalog.check.assert_not_awaited()

    async def test_moderation_can_be_switched_off(self):
        self.plugin.content_moderation_enabled = False

        await self.plugin.save_story_checked("custom", "新题面", "新汤底")

        self.plugin.story_catalog.moderate.assert_not_awaited()
        self.assertEqual(len(self.custom.stories), 1)

    async def test_moderation_runs_before_the_duplicate_check(self):
        """审核成本低于标注查重，先拦下来的题不必再生成标注。"""
        self.plugin.story_catalog.moderate = AsyncMock(
            return_value={"passed": False, "reason": "涉及政治人物"}
        )

        with self.assertRaises(ValueError):
            await self.plugin.save_story_checked("custom", "新题面", "新汤底")

        self.plugin.story_catalog.moderate.assert_awaited_once()
        self.plugin.story_catalog.check.assert_not_awaited()


class StoryDifficultyPickTests(unittest.TestCase):
    """群难度决定抽到多难的题，而不只是次数上限。"""

    @classmethod
    def setUpClass(cls):
        cls.module = judging_runtime.JudgingTests.module

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="soupai-difficulty-tests-")
        self.addCleanup(temporary.cleanup)
        self.data_path = Path(temporary.name)

    def _storage(self, stories):
        storage = self.module.LocalSoupaiStorage(
            self.data_path / "local.json", max_size=50, data_path=self.data_path
        )
        storage.stories = [dict(item) for item in stories]
        return storage

    def test_group_difficulty_maps_to_story_difficulty(self):
        mapping = self.module.SoupaiPlugin._DIFFICULTY_TO_STORY
        for group_level in ("简单", "普通", "困难", "666开挂了"):
            with self.subTest(group_level=group_level):
                self.assertIn(
                    self.module.SoupaiPlugin._story_difficulty(group_level),
                    ("easy", "medium", "hard"),
                )
        # 题目只有三档，简单与开挂两端各映射到最近的档位
        self.assertEqual(mapping["简单"], "easy")
        self.assertEqual(mapping["666开挂了"], "hard")
        # 未知档位返回 None，表示不限难度
        self.assertIsNone(self.module.SoupaiPlugin._story_difficulty("不存在的档位"))

    def test_pick_respects_the_requested_difficulty(self):
        storage = self._storage(
            [
                {"id": "a", "puzzle": "p1", "answer": "a1", "difficulty": "easy"},
                {"id": "b", "puzzle": "p2", "answer": "a2", "difficulty": "hard"},
                {"id": "c", "puzzle": "p3", "answer": "a3", "difficulty": "hard"},
            ]
        )
        # 只抽一轮：两道 hard 都没出过时，easy 那道不能混进来
        picked = {storage.pick_story("s", "hard") for _ in range(2)}
        self.assertEqual({puzzle for puzzle, _ in picked}, {"p2", "p3"})

    def test_unlabelled_stories_stay_reachable(self):
        """没有 difficulty 的旧题不能因为筛选而永远抽不到。"""
        storage = self._storage(
            [{"id": "old", "puzzle": "legacy", "answer": "legacy answer"}]
        )
        self.assertEqual(storage.pick_story("s", "hard"), ("legacy", "legacy answer"))

    def test_exhausted_difficulty_falls_back_to_the_whole_bank(self):
        """指定难度的题全部出完后要退回全库，否则这个群就没题可出了。"""
        storage = self._storage(
            [
                {"id": "a", "puzzle": "p1", "answer": "a1", "difficulty": "easy"},
                {"id": "b", "puzzle": "p2", "answer": "a2", "difficulty": "hard"},
                {"id": "c", "puzzle": "p3", "answer": "a3", "difficulty": "hard"},
            ]
        )
        first = storage.pick_story("s", "hard")
        second = storage.pick_story("s", "hard")
        self.assertEqual({first[0], second[0]}, {"p2", "p3"})
        # 两道 hard 都出过了，这一轮退回全库而不是空转
        third = storage.pick_story("s", "hard")
        self.assertEqual(third, ("p1", "a1"))


if __name__ == "__main__":
    unittest.main()
