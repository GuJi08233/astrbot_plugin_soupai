"""Regression tests for annotation jobs and checked WebUI admission."""

import asyncio
import importlib.util
import logging
import sys
import threading
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch


class CatalogApiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.request = SimpleNamespace(
            json=AsyncMock(return_value={}), query={}, username="test"
        )
        api_module = ModuleType("astrbot.api")
        api_module.logger = logging.getLogger("soupai.api.tests")
        web_module = ModuleType("astrbot.api.web")
        web_module.json_response = lambda value: value
        web_module.error_response = lambda message, **kwargs: {
            "status": "error",
            "message": message,
            **kwargs,
        }
        web_module.request = self.request
        path = Path(__file__).resolve().parents[1] / "webui.py"
        spec = importlib.util.spec_from_file_location("soupai_webui_test", path)
        module = importlib.util.module_from_spec(spec)
        with patch.dict(
            sys.modules, {"astrbot.api": api_module, "astrbot.api.web": web_module}
        ):
            spec.loader.exec_module(module)
        self.entries = [
            {
                "source": "network",
                "id": "one",
                "puzzle": "First puzzle",
                "answer": "Hidden answer one",
            },
            {
                "source": "custom",
                "id": "two",
                "puzzle": "Second puzzle",
                "answer": "Hidden answer two",
            },
        ]
        self.catalog = SimpleNamespace(
            entries=Mock(return_value=self.entries),
            annotation_status=Mock(return_value="missing"),
            get_annotation=Mock(return_value=None),
            annotate=AsyncMock(return_value={"theme": "test"}),
            forget=Mock(),
        )
        self.storage = SimpleNamespace(
            stories=[dict(self.entries[0])],
            lock=threading.RLock(),
            used_ids=Mock(return_value=set()),
            hidden_ids=set(),
            story_id=lambda story, index: story["id"],
            find_index=lambda sid: 0 if sid == "one" else -1,
            save_stories=Mock(),
            usage={},
            save_usage_record=Mock(),
            save_hidden_record=Mock(),
        )
        self.plugin = SimpleNamespace(
            story_catalog=self.catalog,
            config={},
            _ensure_story_storages=Mock(),
            online_story_storage=self.storage,
            local_story_storage=self.storage,
            custom_story_storage=self.storage,
            _resolve_provider=Mock(return_value=SimpleNamespace(text_chat=AsyncMock())),
            _story_write_lock=asyncio.Lock(),
            save_story_checked=AsyncMock(
                return_value={"id": "saved", "check": {"warnings": []}}
            ),
            generate_and_store_story=AsyncMock(
                return_value={
                    "id": "saved",
                    "puzzle": "Generated",
                    "answer": "Hidden",
                    "check": {"method": "text+llm"},
                }
            ),
        )
        self.api = module.SoupaiWebApi(self.plugin)

    async def test_preview_only_counts_and_does_not_call_models(self):
        self.catalog.annotation_status.side_effect = ["ready", "missing"]
        result = await self.api.annotation_preview()
        self.assertEqual(result["data"], {"total": 2, "ready": 1, "pending": 1})
        self.catalog.annotate.assert_not_awaited()
        self.plugin._resolve_provider.assert_not_called()
        self.assertIsNone(self.api.annotation_task)

    async def test_invalid_confidence_threshold_never_reaches_config_storage(self):
        class Config(dict):
            schema = {"jev_judge_min_confidence": {"type": "float"}}
            save_config = Mock()

        self.plugin.config = Config()
        for value in (-0.1, 1.1, "NaN", "Infinity"):
            with self.subTest(value=value):
                self.request.json.return_value = {"jev_judge_min_confidence": value}
                result = await self.api.config_save()
                self.assertEqual(result["status"], "error")
        self.plugin.config.save_config.assert_not_called()

    async def test_force_preview_includes_already_annotated_stories(self):
        self.request.query = {"source": "network", "force": "1"}
        self.catalog.annotation_status.return_value = "ready"
        result = await self.api.annotation_preview()
        self.assertEqual(result["data"], {"total": 1, "ready": 1, "pending": 1})
        self.catalog.annotate.assert_not_awaited()

    async def test_list_returns_status_without_answer_or_annotation(self):
        self.catalog.get_annotation.return_value = {"twist": "Private twist"}
        result = await self.api.stories()
        item = result["data"]["items"][0]
        self.assertEqual(item["annotation_status"], "missing")
        self.assertNotIn("answer", item)
        self.assertNotIn("annotation", item)
        self.assertNotIn("Hidden answer", str(result))
        self.assertNotIn("Private twist", str(result))
        self.catalog.get_annotation.assert_not_called()

    async def test_annotation_details_require_a_separate_request(self):
        self.request.query = {"source": "network", "id": "one"}
        annotation = {"twist": "Explicitly requested detail"}
        self.catalog.get_annotation.return_value = annotation
        result = await self.api.story_annotation()
        self.assertEqual(result["data"]["annotation"], annotation)

    async def test_batch_skips_ready_records_and_tracks_failures(self):
        self.catalog.annotation_status.side_effect = ["ready", "missing"]
        self.catalog.annotate.side_effect = ValueError("Synthetic model failure")
        result = await self.api.annotation_start()
        self.assertEqual(result["status"], "ok")
        await self.api.annotation_task
        job = self.api.annotation_job
        self.assertEqual(
            (job["processed"], job["total"], job["skipped"], job["failed"]),
            (2, 2, 1, 1),
        )
        self.catalog.annotate.assert_awaited_once_with("custom", "two", force=False)
        self.assertEqual(job["errors"][0]["id"], "two")

    async def test_cancellation_preserves_completed_progress(self):
        waiting = asyncio.Event()

        async def annotate(source, sid, force=False):
            if sid == "two":
                waiting.set()
                await asyncio.Event().wait()
            return {"theme": "saved"}

        self.catalog.annotate.side_effect = annotate
        await self.api.annotation_start()
        await asyncio.wait_for(waiting.wait(), timeout=1)
        result = await self.api.annotation_cancel()
        self.assertEqual(result["data"]["job"]["status"], "cancelled")
        self.assertEqual(self.api.annotation_job["succeeded"], 1)
        self.assertEqual(self.api.annotation_job["processed"], 1)

    async def test_concurrent_starts_create_only_one_job(self):
        gate = asyncio.Event()
        calls = 0

        async def payload(**kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                gate.set()
            await gate.wait()
            return {}

        async def annotate(*args, **kwargs):
            await asyncio.Event().wait()

        self.request.json.side_effect = payload
        self.catalog.annotate.side_effect = annotate
        results = await asyncio.gather(
            self.api.annotation_start(), self.api.annotation_start()
        )
        self.assertEqual(
            sorted(result["status"] for result in results), ["error", "ok"]
        )
        await self.api.annotation_cancel()

    async def test_unavailable_model_does_not_start_batch(self):
        self.plugin._resolve_provider.return_value = None
        result = await self.api.annotation_start()
        self.assertEqual(result["status"], "error")
        self.assertIsNone(self.api.annotation_task)
        self.catalog.annotate.assert_not_awaited()

    async def test_batch_trims_model_id_and_rejects_non_chat_provider(self):
        self.plugin.config = {
            "annotation_llm_provider": "  ",
            "verify_llm_provider": " strong ",
        }
        self.plugin._resolve_provider.return_value = SimpleNamespace(
            get_embeddings=AsyncMock()
        )
        result = await self.api.annotation_start()
        self.assertEqual(result["status"], "error")
        self.plugin._resolve_provider.assert_called_once_with("strong")
        self.assertIsNone(self.api.annotation_task)

    async def test_cancelling_old_task_does_not_cancel_a_new_jobs_state(self):
        old_task = asyncio.create_task(asyncio.Event().wait())
        old_job = dict(self.api.annotation_job, status="running")
        new_job = dict(self.api.annotation_job, status="running")
        self.api.annotation_task = old_task
        self.api.annotation_job = old_job

        def start_replacement(task):
            self.api.annotation_task = asyncio.create_task(asyncio.Event().wait())
            self.api.annotation_job = new_job

        old_task.add_done_callback(start_replacement)
        await self.api.annotation_cancel()
        self.assertEqual(old_job["status"], "cancelled")
        self.assertEqual(new_job["status"], "running")
        self.assertFalse(self.api.annotation_task.done())
        await self.api.annotation_cancel()

    async def test_cache_failure_after_deletion_is_reported_as_a_warning(self):
        self.request.json.return_value = {"source": "custom", "id": "one"}
        self.catalog.forget.side_effect = ValueError("Synthetic cache failure")
        result = await self.api.story_delete()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(self.storage.stories, [])
        self.assertTrue(result["data"]["warnings"])

    async def test_failed_deletion_preserves_story_usage_and_annotation(self):
        self.request.json.return_value = {"source": "custom", "id": "one"}
        self.storage.usage = {"group": {"one"}}
        self.storage.hidden_ids = {"one"}
        before = [dict(story) for story in self.storage.stories]
        self.storage.save_stories.side_effect = ValueError("Disk failure")

        result = await self.api.story_delete()

        self.assertEqual(result["status"], "error")
        self.assertEqual(self.storage.stories, before)
        self.assertEqual(self.storage.usage, {"group": {"one"}})
        self.assertEqual(self.storage.hidden_ids, {"one"})
        self.catalog.forget.assert_not_called()
        self.storage.save_usage_record.assert_not_called()
        self.storage.save_hidden_record.assert_not_called()

    async def test_create_uses_shared_admission_and_exposes_collision(self):
        self.request.json.return_value = {
            "source": "custom",
            "puzzle": "New puzzle",
            "answer": "New answer",
        }
        self.plugin.save_story_checked.side_effect = ValueError("Duplicate story")
        result = await self.api.story_create()
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["message"], "Duplicate story")
        self.plugin.save_story_checked.assert_awaited_once_with(
            "custom", "New puzzle", "New answer"
        )

    async def test_update_checks_with_original_stable_id(self):
        self.request.json.return_value = {
            "source": "custom",
            "id": "one",
            "puzzle": "Edited",
            "answer": "Edited answer",
        }
        result = await self.api.story_update()
        self.assertEqual(result["status"], "ok")
        self.plugin.save_story_checked.assert_awaited_once_with(
            "custom", "Edited", "Edited answer", story_id="one"
        )

    async def test_generated_results_forward_preferences_without_revealing_answers(
        self,
    ):
        self.request.json.return_value = {
            "count": 1,
            "theme": "校园",
            "ideas": "一封寄错的信",
        }
        result = await self.api.story_generate()
        self.plugin.generate_and_store_story.assert_awaited_once_with(
            "校园", "一封寄错的信"
        )
        self.assertEqual(result["data"]["created"][0]["check_method"], "text+llm")
        self.assertNotIn("answer", result["data"]["created"][0])

    async def test_failed_generation_never_reports_created_story(self):
        self.plugin.generate_and_store_story.side_effect = ValueError(
            "Invalid generated output"
        )
        result = await self.api.story_generate()
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["data"]["created"], [])
        self.assertEqual(result["data"]["failed"], ["Invalid generated output"])

    def _archive_with_one_round(self):
        """挂一个真的 GameArchive，跑完一局再交给接口。

        复用 test_archive 的模块加载：main.py 里有相对导入，需要那套
        包名桩才 import 得起来。
        """
        import tempfile

        import test_archive

        test_archive.ArchiveTests.setUpClass()
        main = test_archive.ArchiveTests.module

        archive = main.GameArchive(Path(tempfile.mkdtemp()))
        state = main.GameState(
            on_end=lambda gid, game, ending: archive.append(gid, game, ending)
        )
        state.start_game(
            "group-1",
            "Synthetic puzzle",
            "Synthetic answer",
            session="test:GroupMessage:g1",
            difficulty="普通",
            pass_score=70,
            best_score=86,
            passed=True,
            question_count=1,
            question_limit=35,
            hint_count=1,
            hint_limit=5,
            verification_attempts=1,
            started_at="2026-09-22T20:00:00",
        )
        game = state.get_game("group-1")
        game["qa_history"] = [
            {
                "question": "有凶手吗",
                "answer": "否",
                "judged_by": {"engine": "jev", "confidence": 0.87},
            }
        ]
        game["hint_history"] = ["关注【条件】：询问环境情况"]
        game["verify_history"] = [
            {
                "guess": "Synthetic guess",
                "score": 86,
                "breakdown": {"facts": 90, "motive": 80, "twist": 88},
                "level": "核心推理正确",
                "pass_score": 70,
                "passed": True,
                "charged": False,
            }
        ]
        state.end_game("group-1", "reveal")
        self.plugin.game_archive = archive
        self.plugin.game_state = state
        return archive

    async def test_history_list_hides_answers_and_labels_sessions(self):
        self._archive_with_one_round()

        payload = (await self.api.history())["data"]

        (row,) = payload["games"]
        self.assertNotIn("answer", row)
        self.assertEqual(row["ending"], "reveal")
        self.assertEqual(row["best_score"], 86)
        self.assertTrue(row["label"])
        self.assertEqual(payload["total"], 1)
        self.assertEqual(
            [item["session"] for item in payload["sessions"]],
            ["test:GroupMessage:g1"],
        )

    async def test_history_detail_carries_the_answer_and_full_replay(self):
        archive = self._archive_with_one_round()
        entry_id = archive.entries[0]["id"]
        self.request.query = {"id": entry_id}

        detail = (await self.api.history_detail())["data"]

        self.assertEqual(detail["answer"], "Synthetic answer")
        self.assertEqual(len(detail["qa_history"]), 1)
        self.assertEqual(detail["hint_history"], ["关注【条件】：询问环境情况"])
        self.assertEqual(detail["verify_history"][0]["breakdown"]["twist"], 88)
        # 判定来源要一起留着，否则复盘看不出这一问是谁判的
        self.assertEqual(detail["qa_history"][0]["judged_by"]["engine"], "jev")

    async def test_history_detail_rejects_an_unknown_id(self):
        self._archive_with_one_round()
        self.request.query = {"id": "no-such-id"}

        self.assertEqual((await self.api.history_detail())["status"], "error")

        self.request.query = {}
        self.assertEqual((await self.api.history_detail())["status"], "error")

    async def test_history_clear_can_be_scoped_to_one_session(self):
        archive = self._archive_with_one_round()
        self.request.json.return_value = {"session": "nope"}

        self.assertEqual((await self.api.history_clear())["data"]["removed"], 0)

        self.request.json.return_value = {}
        self.assertEqual((await self.api.history_clear())["data"]["removed"], 1)
        self.assertEqual(archive.entries, [])

    async def test_history_endpoints_survive_a_missing_archive(self):
        self.plugin.game_archive = None

        self.assertEqual((await self.api.history())["data"]["games"], [])
        self.assertEqual((await self.api.history_detail())["status"], "error")
        self.assertEqual((await self.api.history_clear())["status"], "error")


if __name__ == "__main__":
    unittest.main()
