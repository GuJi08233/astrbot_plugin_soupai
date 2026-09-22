"""Turning a finished round into per-player token rewards."""

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import test_judging as judging_runtime


class FakeChain:
    """Stand-in for MessageChain, which the loader binds to a bare object."""

    def __init__(self):
        self.parts = []

    def message(self, text):
        self.parts.append(text)
        return self


class RewardSettlementTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        judging_runtime.JudgingTests.setUpClass()
        cls.module = judging_runtime.JudgingTests.module

    def setUp(self):
        self.plugin = self.module.SoupaiPlugin.__new__(self.module.SoupaiPlugin)
        self.plugin.config = {
            "reward_enabled": True,
            "reward_pool": 20,
            "reward_daily_cap": 100,
            "reward_min_questions": 3,
            "reward_hint_penalty": 1.0,
            "reward_verify_penalty": 0.5,
            "reward_verify_weight": 3.0,
        }
        self.plugin._load_config()
        self.plugin._reward_tasks = set()
        chain_patch = patch.object(self.module, "MessageChain", FakeChain)
        chain_patch.start()
        self.addCleanup(chain_patch.stop)

    def game(self, questions, hints=(), verifies=()):
        """Build a finished round.

        Args:
            questions: ``(player_id, grade)`` per asked question, where grade is
                a grade string, a probability distribution, or None for neither.
            hints: Player id per hint spent.
            verifies: ``(player_id, score, passed)`` per verification.

        Returns:
            A round shaped like the ones the archive stores.
        """
        return {
            "puzzle": "Synthetic puzzle",
            "answer": "Synthetic answer",
            "session": "test:GroupMessage:group",
            "started_at": "2026-09-22T20:00:00",
            "qa_history": [
                {
                    "question": "Synthetic question",
                    "answer": "是",
                    "judged_by": {"engine": "jev"},
                    "player_id": player,
                    "player_name": player.upper(),
                    "contribution": grade if isinstance(grade, str) else None,
                    "contribution_probabilities": (
                        grade if isinstance(grade, dict) else None
                    ),
                }
                for player, grade in questions
            ],
            "hint_credits": [
                {"player_id": player, "player_name": player.upper()} for player in hints
            ],
            "verify_history": [
                {
                    "guess": "Synthetic guess",
                    "score": score,
                    "passed": passed,
                    "player_id": player,
                    "player_name": player.upper(),
                }
                for player, score, passed in verifies
            ],
        }

    def amounts(self, shares):
        return {item["player_name"]: item["amount"] for item in shares}

    def faucet(self, granted=None):
        """Wire a fake token plugin and return its grant mock."""
        grant = AsyncMock(side_effect=granted) if granted else AsyncMock(return_value=4)
        self.plugin.context = SimpleNamespace(
            get_registered_star=Mock(
                return_value=SimpleNamespace(star_cls=SimpleNamespace(grant=grant))
            ),
            send_message=AsyncMock(),
        )
        return grant

    # ---------------------------------------------------------------- #
    # Contribution table
    # ---------------------------------------------------------------- #

    def test_pool_is_split_by_contribution_and_fully_handed_out(self):
        game = self.game([("a", "关键"), ("a", "关键"), ("b", "次要"), ("b", "次要")])
        shares = self.plugin._contribution_table(game)
        amounts = self.amounts(shares)

        self.assertGreater(amounts["A"], amounts["B"])
        # 取整的余数必须回到奖池里，不能凭空蒸发
        self.assertEqual(sum(amounts.values()), self.plugin.reward_pool)

    def test_short_rounds_pay_nothing(self):
        """开一局问两句就收场，不该能印币。"""
        game = self.game([("a", "关键"), ("a", "关键")])

        self.assertEqual(self.plugin._contribution_table(game), [])

    def test_repeated_questions_earn_nothing_but_are_not_punished(self):
        game = self.game([("a", "有效"), ("a", "重复"), ("a", "重复")])
        shares = self.plugin._contribution_table(game)

        self.assertEqual(self.amounts(shares), {"A": 20})

    def test_hints_and_failed_verifications_cut_the_share(self):
        game = self.game(
            [("a", "有效"), ("a", "有效"), ("b", "有效"), ("b", "有效")],
            hints=["b"],
            verifies=[("b", 30, False)],
        )
        amounts = self.amounts(self.plugin._contribution_table(game))

        self.assertGreater(amounts["A"], amounts["B"])

    def test_players_who_only_consume_get_nothing(self):
        game = self.game(
            [("a", "有效"), ("a", "有效"), ("a", "有效")],
            hints=["b", "b"],
        )
        amounts = self.amounts(self.plugin._contribution_table(game))

        self.assertNotIn("B", amounts)
        self.assertEqual(amounts["A"], 20)

    def test_solver_is_rewarded_without_taking_the_whole_pool(self):
        game = self.game(
            [("a", "有效"), ("a", "有效"), ("b", "有效"), ("b", "有效")],
            verifies=[("b", 90, True)],
        )
        amounts = self.amounts(self.plugin._contribution_table(game))

        self.assertGreater(amounts["B"], amounts["A"])
        # 汤是一起问出来的，临门一脚不能把别人清零
        self.assertGreater(amounts["A"], 0)

    def test_missing_grades_fall_back_to_a_neutral_weight(self):
        """Jev 关着或那一问回退给了 LLM 时，所有提问同权。"""
        game = self.game([("a", None), ("a", None), ("b", None), ("b", None)])
        amounts = self.amounts(self.plugin._contribution_table(game))

        self.assertEqual(amounts["A"], amounts["B"])

    def test_fuzzy_grades_are_weighted_by_their_whole_distribution(self):
        """最大项相同，分布不同，拿到的也该不同。

        两组的 argmax 都是「次要」，但 a 的概率质量明显更靠上。照最大项
        一口价会把这两种问题算成同一档。
        """
        blurry = {"关键": 0.15, "有效": 0.41, "次要": 0.44, "重复": 0.0}
        sharp = {"关键": 0.0, "有效": 0.0, "次要": 1.0, "重复": 0.0}
        game = self.game([("a", blurry), ("a", blurry), ("b", sharp), ("b", sharp)])
        amounts = self.amounts(self.plugin._contribution_table(game))

        self.assertGreater(amounts["A"], amounts["B"])

    def test_a_sharp_distribution_agrees_with_its_top_option(self):
        """分布集中时期望折算和最大项一致，改动只影响模糊的那些。"""
        entry = {
            "contribution_probabilities": {
                "关键": 0.0,
                "有效": 0.0,
                "次要": 1.0,
                "重复": 0.0,
            }
        }

        self.assertAlmostEqual(
            self.plugin._contribution_weight(entry),
            self.plugin._CONTRIBUTION_WEIGHTS["次要"],
        )

    def test_distributions_are_normalised_before_weighting(self):
        """接口返回的分布未必严格和为 1。"""
        entry = {"contribution_probabilities": {"关键": 0.5, "有效": 0.5}}
        midpoint = (
            self.plugin._CONTRIBUTION_WEIGHTS["关键"]
            + self.plugin._CONTRIBUTION_WEIGHTS["有效"]
        ) / 2

        self.assertAlmostEqual(self.plugin._contribution_weight(entry), midpoint)

    def test_weight_falls_back_when_no_distribution_came_back(self):
        self.assertAlmostEqual(
            self.plugin._contribution_weight({"contribution": "关键"}),
            self.plugin._CONTRIBUTION_WEIGHTS["关键"],
        )
        self.assertAlmostEqual(
            self.plugin._contribution_weight({}),
            self.plugin._CONTRIBUTION_DEFAULT_WEIGHT,
        )

    def test_junk_probability_values_do_not_poison_the_weight(self):
        entry = {
            "contribution": "关键",
            "contribution_probabilities": {"关键": "很高", "未知档位": 0.9},
        }

        self.assertAlmostEqual(
            self.plugin._contribution_weight(entry),
            self.plugin._CONTRIBUTION_WEIGHTS["关键"],
        )

    def test_anonymous_records_are_skipped(self):
        game = self.game([("a", "有效"), ("a", "有效"), ("a", "有效")])
        game["qa_history"].append(
            {"question": "旧存档", "answer": "是", "judged_by": {}}
        )
        amounts = self.amounts(self.plugin._contribution_table(game))

        self.assertEqual(amounts, {"A": 20})

    # ---------------------------------------------------------------- #
    # Settlement
    # ---------------------------------------------------------------- #

    async def test_settlement_runs_once_per_round(self):
        game = self.game([("a", "关键")] * 3)
        self.plugin._grant_rewards = AsyncMock()

        self.plugin._settle_rewards("group", game, "reveal")
        self.plugin._settle_rewards("group", game, "reveal")
        await asyncio.gather(*list(self.plugin._reward_tasks), return_exceptions=True)

        self.assertEqual(self.plugin._grant_rewards.await_count, 1)
        self.assertTrue(game["rewarded"])
        self.assertEqual(self.amounts(game["rewards"]), {"A": 20})

    async def test_unloading_the_plugin_does_not_settle(self):
        """插件卸载不是玩完一局，不该发钱。"""
        game = self.game([("a", "关键")] * 3)
        self.plugin._grant_rewards = AsyncMock()

        self.plugin._settle_rewards("group", game, "unload")

        self.plugin._grant_rewards.assert_not_awaited()
        self.assertNotIn("rewards", game)

    async def test_rewards_stay_off_until_enabled(self):
        self.plugin.reward_enabled = False
        game = self.game([("a", "关键")] * 3)
        self.plugin._grant_rewards = AsyncMock()

        self.plugin._settle_rewards("group", game, "reveal")

        self.plugin._grant_rewards.assert_not_awaited()

    def test_contribution_rider_is_only_asked_for_when_rewards_are_on(self):
        self.assertIn("contribution", self.plugin._contribution_question())
        self.plugin.reward_enabled = False
        self.assertIsNone(self.plugin._contribution_question())

    # ---------------------------------------------------------------- #
    # Handing the tokens over
    # ---------------------------------------------------------------- #

    async def test_grant_is_called_with_the_configured_cap(self):
        grant = self.faucet()
        game = self.game([("a", "关键")] * 3)
        shares = self.plugin._contribution_table(game)

        await self.plugin._grant_rewards("group", game, shares)

        self.assertEqual(grant.await_args.kwargs["source"], "soupai")
        self.assertEqual(grant.await_args.kwargs["daily_cap"], 100)
        self.assertEqual(grant.await_args.args, ("a", 20))

    async def test_announcement_reports_what_landed_not_what_was_planned(self):
        """日限额削掉一部分时，播报应发数会让玩家查不到账。"""
        grant = self.faucet(granted=[4])
        game = self.game([("a", "关键")] * 3)
        shares = self.plugin._contribution_table(game)

        await self.plugin._grant_rewards("group", game, shares)

        self.assertEqual(shares[0]["amount"], 20)
        self.assertEqual(shares[0]["granted"], 4)
        chain = self.plugin.context.send_message.await_args.args[1]
        self.assertIn("A +4", chain.parts[0])
        self.assertNotIn("+20", chain.parts[0])
        self.assertEqual(grant.await_count, 1)

    async def test_a_capped_out_player_is_not_announced(self):
        self.faucet(granted=[0])
        game = self.game([("a", "关键")] * 3)

        await self.plugin._grant_rewards(
            "group", game, self.plugin._contribution_table(game)
        )

        self.plugin.context.send_message.assert_not_awaited()

    async def test_a_missing_token_plugin_is_not_fatal(self):
        """发币插件没装，这一局照样正常收场，只是不发币。"""
        self.plugin.context = SimpleNamespace(
            get_registered_star=Mock(return_value=None),
            send_message=AsyncMock(),
        )
        game = self.game([("a", "关键")] * 3)

        await self.plugin._grant_rewards(
            "group", game, self.plugin._contribution_table(game)
        )

        self.plugin.context.send_message.assert_not_awaited()

    async def test_one_failed_grant_does_not_strand_the_others(self):
        grant = self.faucet(granted=[RuntimeError("Synthetic faucet failure"), 6])
        game = self.game([("a", "关键"), ("a", "关键"), ("b", "次要"), ("b", "次要")])

        await self.plugin._grant_rewards(
            "group", game, self.plugin._contribution_table(game)
        )

        self.assertEqual(grant.await_count, 2)
        chain = self.plugin.context.send_message.await_args.args[1]
        self.assertIn("B +6", chain.parts[0])


if __name__ == "__main__":
    unittest.main()
