"""Exercise round lifecycle with the real waiter and isolated model responses."""

import asyncio
import importlib.machinery
import importlib.util
import sys
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import test_judging as judging_runtime
import test_webui_catalog as webui_runtime


class GameSessionTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        """Load AstrBot's waiter without initializing its application or data."""
        judging_runtime.JudgingTests.setUpClass()
        cls.module = judging_runtime.JudgingTests.module
        root = Path(__file__).resolve().parents[4]
        package = importlib.machinery.PathFinder.find_spec(
            "astrbot", [str(root), *sys.path]
        )
        if package is None or not package.submodule_search_locations:
            raise unittest.SkipTest(
                "AstrBot source is required for waiter integration tests"
            )
        path = (
            Path(next(iter(package.submodule_search_locations)))
            / "core/utils/session_waiter.py"
        )
        spec = importlib.util.spec_from_file_location("soupai_session_tests", path)
        cls.runtime = importlib.util.module_from_spec(spec)
        modules = {}
        for name in (
            "astrbot",
            "astrbot.core",
            "astrbot.core.message",
            "astrbot.core.message.components",
            "astrbot.core.platform",
        ):
            modules[name] = ModuleType(name)
            modules[name].__path__ = []
            if "." in name:
                parent, attribute = name.rsplit(".", 1)
                setattr(modules[parent], attribute, modules[name])
        modules["astrbot.core.message.components"].BaseMessageComponent = object
        modules["astrbot.core.platform"].AstrMessageEvent = object
        with patch.dict(sys.modules, modules):
            spec.loader.exec_module(cls.runtime)
        cls.module.session_waiter = cls.runtime.session_waiter
        cls.module.GroupSessionFilter = type(
            "GroupSessionFilter",
            (cls.module.GroupSessionFilter, cls.runtime.SessionFilter),
            {},
        )

    def setUp(self):
        self.rounds = []
        self.plugin = self.module.SoupaiPlugin.__new__(self.module.SoupaiPlugin)
        self.plugin.config = {}
        self.plugin._jev_client = None
        self.plugin._load_config()
        self.plugin.game_state = self.module.GameState()
        self.plugin.generating_games = set()
        self.plugin.group_difficulty = {}
        self.plugin.difficulty_settings = {
            "普通": {"limit": 35, "accept_levels": ["完全还原"], "hint_limit": 5}
        }
        self.plugin._ensure_story_storages = Mock()
        self.plugin.get_story_by_strategy = AsyncMock(
            return_value=("Synthetic puzzle", "Synthetic answer")
        )
        self.plugin._is_at_bot = Mock(return_value=True)
        self.plugin._send_reply = AsyncMock()
        self.plugin._safe_send = AsyncMock(return_value=True)
        self.plugin.judge_question = AsyncMock(return_value=("是", {"engine": "llm"}))
        self.plugin.auto_generate_task = None
        fixture = webui_runtime.CatalogApiTests()
        fixture.setUp()
        self.api = fixture.api
        self.api.plugin = self.plugin
        self.request = fixture.request
        self.request.json.return_value = {"key": "group"}

    async def asyncTearDown(self):
        for _, task in self.rounds:
            task.cancel()
        await asyncio.gather(*(task for _, task in self.rounds), return_exceptions=True)
        for generator, _ in self.rounds:
            await generator.aclose()
        self.assertEqual(self.runtime.USER_SESSIONS, {})
        self.assertEqual(self.runtime.FILTERS, [])

    def event(self, message: str, admin: bool = False):
        """Create a group event without platform or network access.

        Args:
            message: Message delivered to the round.
            admin: Whether the sender has AstrBot administrator privileges.

        Returns:
            An event recording all replies and permission checks.
        """
        return SimpleNamespace(
            message_str=message,
            unified_msg_origin="test:GroupMessage:group",
            get_group_id=lambda: "group",
            plain_result=lambda text: text,
            send=AsyncMock(),
            is_admin=Mock(return_value=admin),
            is_at_or_wake_command=False,
            stop_event=Mock(),
            get_messages=lambda: [],
        )

    async def start_round(self):
        """Start a game command and wait for its child waiter to register.

        Returns:
            The round state and real AstrBot waiter.
        """
        generator = self.plugin.start_soupai_game(self.event("汤"))
        opening = await anext(generator)
        self.assertIn("游戏开始", opening)
        task = asyncio.create_task(anext(generator))
        self.rounds.append((generator, task))
        game = self.plugin.game_state.get_game("group")
        for _ in range(10):
            await asyncio.sleep(0)
            for waiter in self.runtime.USER_SESSIONS.values():
                if waiter.session_filter.game is game:
                    return game, waiter
        self.fail("Game waiter did not register")

    async def test_force_end_requires_admin_with_or_without_prefix(self):
        game, waiter = await self.start_round()
        for message in ("强制结束", "/强制结束"):
            event = self.event(message)
            await self.runtime.SessionWaiter.trigger(waiter.session_id, event)
            self.assertIs(self.plugin.game_state.get_game("group"), game)
            event.is_admin.assert_called_once()
            self.assertIn("管理员", self.plugin._send_reply.call_args.args[1])

        await self.runtime.SessionWaiter.trigger(
            waiter.session_id, self.event("强制结束", admin=True)
        )
        await asyncio.gather(game["_session_task"], return_exceptions=True)
        self.assertFalse(self.plugin.game_state.is_game_active("group"))
        self.assertEqual(self.runtime.USER_SESSIONS, {})

    async def test_web_end_clears_waiter_and_allows_immediate_restart(self):
        game, waiter = await self.start_round()
        self.assertNotIn("group", self.plugin.generating_games)

        result = await self.api.games_end()

        self.assertTrue(result["data"]["ended"])
        self.assertFalse(game["is_active"])
        self.assertEqual(self.runtime.USER_SESSIONS, {})
        self.assertEqual(self.runtime.FILTERS, [])
        new_game, new_waiter = await self.start_round()
        self.assertNotEqual(waiter.session_id, new_waiter.session_id)
        await asyncio.sleep(0)
        self.assertIs(self.plugin.game_state.get_game("group"), new_game)

    async def test_old_waiter_cleanup_does_not_remove_a_replacement_round(self):
        old_game, old_waiter = await self.start_round()
        self.plugin.game_state.end_game("group")
        self.assertEqual(old_waiter.session_filter.filter(self.event("汤")), "")
        new_game, new_waiter = await self.start_round()
        await asyncio.gather(old_game["_session_task"], return_exceptions=True)
        self.assertIs(self.plugin.game_state.get_game("group"), new_game)
        self.assertIs(self.runtime.USER_SESSIONS[new_waiter.session_id], new_waiter)

    async def test_end_before_waiter_registration_does_not_reopen_the_old_round(self):
        old_command = self.plugin.start_soupai_game(self.event("汤"))
        await anext(old_command)
        self.plugin.game_state.end_game("group")
        new_game, _ = await self.start_round()
        with self.assertRaises(StopAsyncIteration):
            await anext(old_command)
        self.assertIs(self.plugin.game_state.get_game("group"), new_game)

    async def test_closed_opening_message_does_not_leave_a_game(self):
        command = self.plugin.start_soupai_game(self.event("汤"))
        await anext(command)
        await command.aclose()
        self.assertFalse(self.plugin.game_state.is_game_active("group"))
        self.assertNotIn("group", self.plugin.generating_games)

    async def test_cancelled_generation_clears_the_pending_flag(self):
        entered = asyncio.Event()

        async def generate(*args):
            entered.set()
            await asyncio.Event().wait()

        self.plugin.get_story_by_strategy.side_effect = generate
        command = self.plugin.start_soupai_game(self.event("汤"))
        task = asyncio.create_task(anext(command))
        self.rounds.append((command, task))
        await asyncio.wait_for(entered.wait(), 1)
        self.assertIn("group", self.plugin.generating_games)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self.assertNotIn("group", self.plugin.generating_games)

    async def test_unavailable_judgment_preserves_quota_and_history(self):
        game, waiter = await self.start_round()
        game["question_limit"] = 1
        self.plugin.judge_question.return_value = (
            "Service unavailable",
            {"engine": "unavailable"},
        )
        await self.runtime.SessionWaiter.trigger(
            waiter.session_id, self.event("Question")
        )
        self.assertEqual(game["question_count"], 0)
        self.assertEqual(game["qa_history"], [])
        self.assertIn("不计提问次数", self.plugin._send_reply.call_args.args[1])
        self.plugin._safe_send.assert_not_awaited()

        self.plugin.judge_question.return_value = (
            "否",
            {"engine": "llm", "fallback": True},
        )
        await self.runtime.SessionWaiter.trigger(waiter.session_id, self.event("Retry"))
        self.assertEqual(game["question_count"], 1)
        self.assertEqual(len(game["qa_history"]), 1)
        self.assertTrue(game["qa_history"][0]["judged_by"]["fallback"])

    async def test_unlimited_mode_still_counts_successful_questions(self):
        game, waiter = await self.start_round()
        game["question_limit"] = None
        await self.runtime.SessionWaiter.trigger(
            waiter.session_id, self.event("Question")
        )
        self.assertEqual(game["question_count"], 1)
        self.assertEqual(self.plugin._send_reply.call_args.args[1], "是")

    async def test_late_model_results_do_not_reply_to_or_end_a_new_round(self):
        for method, message, result in (
            ("judge_question", "Question", ("是", {"engine": "llm"})),
            (
                "verify_user_guess",
                "验证 Answer",
                self.module.VerificationResult("完全还原", "Correct"),
            ),
            ("generate_hint", "提示", "A synthetic hint"),
        ):
            with self.subTest(method=method):
                game, waiter = await self.start_round()
                game["qa_history"] = [{"question": "Earlier question", "answer": "是"}]
                entered, release = asyncio.Event(), asyncio.Event()

                async def delayed(*args):
                    entered.set()
                    await release.wait()
                    return result

                with patch.object(self.plugin, method, side_effect=delayed):
                    event = self.event(message)
                    trigger = asyncio.create_task(
                        self.runtime.SessionWaiter.trigger(waiter.session_id, event)
                    )
                    try:
                        await asyncio.wait_for(entered.wait(), 1)
                        await self.api.games_end()
                        replacement, _ = await self.start_round()
                        release.set()
                        await asyncio.wait_for(trigger, 1)
                        self.assertIs(
                            self.plugin.game_state.get_game("group"), replacement
                        )
                        self.assertEqual(replacement["question_count"], 0)
                        self.assertEqual(game["hint_count"], 0)
                        self.assertEqual(game["verification_attempts"], 0)
                        event.send.assert_not_awaited()
                        self.plugin._send_reply.assert_not_awaited()
                        self.plugin._safe_send.assert_not_awaited()
                    finally:
                        release.set()
                        await asyncio.gather(trigger, return_exceptions=True)
                        await self.api.games_end()

    async def test_reveal_accepts_both_prefix_forms_and_cleans_up(self):
        for message in ("揭晓", "/揭晓"):
            game, waiter = await self.start_round()
            await self.runtime.SessionWaiter.trigger(
                waiter.session_id, self.event(message)
            )
            await asyncio.gather(game["_session_task"], return_exceptions=True)
            self.assertFalse(self.plugin.game_state.is_game_active("group"))
            self.assertEqual(self.runtime.USER_SESSIONS, {})
            self.assertIn("Synthetic answer", self.plugin._safe_send.call_args.args[1])

    async def test_timeout_and_unload_release_registered_waiters(self):
        game, waiter = await self.start_round()
        waiter.session_controller.stop(TimeoutError("Synthetic timeout"))
        await asyncio.gather(self.rounds[-1][1], return_exceptions=True)
        self.assertFalse(self.plugin.game_state.is_game_active("group"))
        self.assertEqual(self.runtime.USER_SESSIONS, {})
        self.assertIn("游戏超时", self.plugin._safe_send.call_args.args[1])

        await self.start_round()
        await self.plugin.terminate()
        self.assertFalse(self.plugin.game_state.is_game_active("group"))
        self.assertEqual(self.runtime.USER_SESSIONS, {})


if __name__ == "__main__":
    unittest.main()
