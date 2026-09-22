"""对局存档的回归测试：归档、复盘字段、汤底可见性与容量。"""

import importlib.util
import json
import logging
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch


class ArchiveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        """Load the real plugin without starting AstrBot or reading its data."""

        def decorate(*args, **kwargs):
            return lambda method: method

        plugin_path = Path(__file__).resolve().parents[1]
        bindings = {
            "astrbot": {},
            "astrbot.api": {
                "AstrBotConfig": dict,
                "logger": logging.getLogger("soupai.tests"),
            },
            "astrbot.api.event": {
                "AstrMessageEvent": object,
                "MessageChain": object,
                "MessageEventResult": object,
                "filter": SimpleNamespace(
                    command=decorate,
                    permission_type=decorate,
                    event_message_type=decorate,
                    PermissionType=SimpleNamespace(ADMIN="admin"),
                    EventMessageType=SimpleNamespace(ALL="all"),
                ),
            },
            "astrbot.api.message_components": {"At": object, "Reply": object},
            "astrbot.api.provider": {"LLMResponse": object},
            "astrbot.api.star": {
                "Context": object,
                "Star": object,
                "StarTools": object,
            },
            "astrbot.core": {},
            "astrbot.core.utils": {},
            "astrbot.core.utils.session_waiter": {
                "SessionController": object,
                "SessionFilter": object,
                "session_waiter": decorate,
            },
            "soupai_test_runtime": {"__path__": [str(plugin_path)]},
            "soupai_test_runtime.webui": {"SoupaiWebApi": object},
        }
        modules = {}
        for name, attributes in bindings.items():
            modules[name] = ModuleType(name)
            modules[name].__dict__.update(attributes)
        spec = importlib.util.spec_from_file_location(
            "soupai_test_runtime.main", plugin_path / "main.py"
        )
        cls.module = importlib.util.module_from_spec(spec)
        modules[spec.name] = cls.module
        with patch.dict(sys.modules, modules):
            spec.loader.exec_module(cls.module)

    def setUp(self):
        self.data_path = Path(tempfile.mkdtemp())
        self.archive = self.module.GameArchive(self.data_path)
        self.state = self.module.GameState(
            on_end=lambda gid, game, ending: self.archive.append(gid, game, ending)
        )

    def play(self, group_id="group", session="test:GroupMessage:group", **extra):
        """开一局并填上典型的复盘数据。"""
        self.state.start_game(
            group_id,
            "Synthetic puzzle",
            "Synthetic answer",
            session=session,
            difficulty="普通",
            pass_score=70,
            best_score=58,
            question_count=2,
            question_limit=35,
            hint_count=1,
            hint_limit=5,
            verification_attempts=1,
            started_at="2026-09-22T20:00:00",
            **extra,
        )
        game = self.state.get_game(group_id)
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
                "score": 58,
                "breakdown": {"facts": 60, "motive": 50, "twist": 60},
                "level": "部分正确",
                "pass_score": 70,
                "passed": False,
                "charged": True,
            }
        ]
        return game

    def test_finished_round_is_archived_with_replay_data(self):
        self.play()

        self.assertTrue(self.state.end_game("group", "reveal"))

        (entry,) = self.archive.entries
        self.assertEqual(entry["ending"], "reveal")
        self.assertEqual(entry["group_id"], "group")
        self.assertEqual(len(entry["qa_history"]), 1)
        self.assertEqual(entry["hint_history"], ["关注【条件】：询问环境情况"])
        self.assertEqual(entry["verify_history"][0]["score"], 58)
        self.assertTrue(entry["id"])
        self.assertTrue(entry["ended_at"])
        # 判定来源要跟着一起留档，否则复盘看不出这一问是谁判的
        self.assertEqual(entry["qa_history"][0]["judged_by"]["confidence"], 0.87)

    def test_runtime_fields_never_reach_the_archive(self):
        game = self.play()
        game["_session_task"] = SimpleNamespace(done=lambda: True)
        game["_player_qa"] = {"player-one": [{"question": "q", "answer": "是"}]}

        self.state.end_game("group", "timeout")

        (entry,) = self.archive.entries
        self.assertNotIn("_session_task", entry)
        self.assertNotIn("_player_qa", entry)
        self.assertNotIn("is_active", entry)
        # 存档必须可序列化，否则整份档都写不下去
        json.dumps(entry, ensure_ascii=False)

    def test_answers_are_detail_only(self):
        self.play()
        self.state.end_game("group", "reveal")

        (row,) = self.archive.page()["games"]
        self.assertNotIn("answer", row)

        detail = self.archive.detail(row["id"])
        self.assertEqual(detail["answer"], "Synthetic answer")
        self.assertIsNone(self.archive.detail("no-such-id"))

    def test_empty_rounds_are_not_archived(self):
        self.state.start_game("group", "Synthetic puzzle", "Synthetic answer")

        self.assertTrue(self.state.end_game("group", "aborted"))

        self.assertEqual(self.archive.entries, [])

    def test_archive_survives_a_reload_and_is_newest_first(self):
        for index in range(3):
            self.play(group_id=f"group-{index}")
            self.state.end_game(f"group-{index}", "reveal")

        reloaded = self.module.GameArchive(self.data_path)

        self.assertEqual(len(reloaded.entries), 3)
        rows = reloaded.page()["games"]
        self.assertEqual(
            [row["group_id"] for row in rows], ["group-2", "group-1", "group-0"]
        )

    def test_archive_is_capped_and_drops_the_oldest(self):
        limit = self.module.GameArchive._MAX_ENTRIES
        for index in range(limit + 5):
            self.play(group_id=f"group-{index}")
            self.state.end_game(f"group-{index}", "reveal")

        self.assertEqual(len(self.archive.entries), limit)
        self.assertEqual(self.archive.entries[0]["group_id"], "group-5")

    def test_sessions_filter_and_clear(self):
        self.play(group_id="a", session="s-1")
        self.state.end_game("a", "reveal")
        self.play(group_id="b", session="s-2")
        self.state.end_game("b", "reveal")

        self.assertEqual(self.archive.page(session="s-1")["total"], 1)
        self.assertEqual(
            sorted(item["session"] for item in self.archive.sessions()),
            ["s-1", "s-2"],
        )
        self.assertEqual(self.archive.clear("s-1"), 1)
        self.assertEqual(self.archive.page()["total"], 1)
        self.assertEqual(self.archive.clear(), 1)
        self.assertEqual(self.archive.entries, [])

    def test_a_broken_archive_file_does_not_break_startup(self):
        (self.data_path / "game_history.json").write_text("not json", encoding="utf-8")

        archive = self.module.GameArchive(self.data_path)

        self.assertEqual(archive.entries, [])

    def test_archiving_failure_still_ends_the_round(self):
        self.play()
        with patch.object(
            self.archive,
            "append",
            side_effect=RuntimeError("Synthetic archive failure"),
        ):
            # 存档挂掉不能把对局卡在「进行中」，否则这个群再也开不了新局
            self.assertTrue(self.state.end_game("group", "reveal"))

        self.assertFalse(self.state.is_game_active("group"))


if __name__ == "__main__":
    unittest.main()
