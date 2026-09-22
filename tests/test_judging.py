"""Regression tests for Jev diagnostics and independent hint providers."""

import asyncio
import importlib.util
import json
import logging
import sys
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx


class JudgingTests(unittest.IsolatedAsyncioTestCase):
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
        self.plugin = self.module.SoupaiPlugin.__new__(self.module.SoupaiPlugin)
        self.plugin._jev_client = None
        self.plugin.config = {
            "judge_engine": "jev",
            "judge_fallback_to_llm": True,
            "jev_api_key": "test-key",
            "jev_base_url": "https://typesafe.test",
            "jev_model": "jev-test",
            "jev_judge_min_confidence": 0.5,
            "judge_llm_provider": "judge",
            "hint_llm_provider": "hint",
            "verify_llm_provider": "verify",
        }
        self.plugin._load_config()
        self.judge_provider = SimpleNamespace(
            text_chat=AsyncMock(return_value=SimpleNamespace(completion_text="否"))
        )
        self.hint_provider = SimpleNamespace(
            text_chat=AsyncMock(
                return_value=SimpleNamespace(completion_text="关注【时间】：比较先后")
            )
        )
        self.verify_provider = SimpleNamespace(
            text_chat=AsyncMock(
                return_value=SimpleNamespace(
                    completion_text="事实：95\n动机：90\n反转：95\n评价：推理正确"
                )
            )
        )
        self.default_provider = SimpleNamespace(
            text_chat=AsyncMock(
                return_value=SimpleNamespace(completion_text="默认提示")
            )
        )
        self.providers = {
            "judge": self.judge_provider,
            "hint": self.hint_provider,
            "verify": self.verify_provider,
        }
        self.plugin.context = SimpleNamespace(
            get_provider_by_id=Mock(side_effect=self.providers.get),
            get_using_provider=Mock(return_value=self.default_provider),
        )
        self.answer = {
            "type": "choice",
            "choice": "是",
            "confidence": 0.82,
            "probabilities": {
                "是": 0.85,
                "否": 0.08,
                "不重要": 0.05,
                "是也不是": 0.02,
            },
        }
        self.response_data = {"answers": {"verdict": self.answer}}
        self.response_status = 200
        self.request_error = None
        self.requests = []

        def handle_request(request):
            self.requests.append(request)
            if self.request_error:
                raise self.request_error
            # Raw JSON also lets the tests exercise invalid NaN/Infinity values.
            return httpx.Response(
                self.response_status,
                content=json.dumps(self.response_data),
                headers={"Content-Type": "application/json"},
            )

        transport = httpx.MockTransport(handle_request)
        client_class = httpx.AsyncClient

        def create_client(**kwargs):
            client = client_class(transport=transport, **kwargs)
            self.addAsyncCleanup(client.aclose)
            return client

        client_patch = patch.object(
            self.module.httpx, "AsyncClient", side_effect=create_client
        )
        self.client_factory = client_patch.start()
        self.addCleanup(client_patch.stop)

    async def test_request_shape_and_full_probabilities_are_preserved(self):
        probabilities = {
            "是": 0.61001,
            "否": 0.35,
            "不重要": 0.02,
            "是也不是": 0.019,
        }
        self.answer["probabilities"] = probabilities
        self.answer["confidence"] = 0.91
        self.plugin.jev_judge_min_confidence = 0.7
        state = {"example": "A public, synthetic test case."}
        result = await self.plugin._jev_choice(
            state, "Choose a verdict.", self.plugin._JUDGE_CRITERIA
        )

        self.assertEqual(result["choice"], "是")
        self.assertEqual(result["confidence"], 0.91)
        self.assertEqual(result["probabilities"], probabilities)
        self.assertEqual(result["threshold"], 0.7)
        self.assertIsNone(result["reason"])
        self.assertEqual(len(self.requests), 1)
        request = self.requests[0]
        self.assertEqual(request.method, "POST")
        self.assertEqual(str(request.url), "https://typesafe.test/v1/systemone")
        self.assertEqual(request.headers["Authorization"], "Bearer test-key")
        self.assertEqual(
            json.loads(request.content),
            {
                "model": "jev-test",
                "state": state,
                "questions": {
                    "verdict": {
                        "type": "choice",
                        "instructions": "Choose a verdict.",
                        "criteria": self.plugin._JUDGE_CRITERIA,
                    }
                },
            },
        )

    async def test_successful_judgment_keeps_jev_diagnostics(self):
        reply, source = await self.plugin.judge_question("Test?", "Test answer")

        self.assertEqual(reply, "是")
        self.assertEqual(source["engine"], "jev")
        self.assertEqual(source["confidence"], 0.82)
        self.assertEqual(source["jev"]["probabilities"], self.answer["probabilities"])
        self.assertEqual(source["jev"]["choice"], "是")
        self.assertIsNone(source["jev"]["reason"])
        self.judge_provider.text_chat.assert_not_awaited()

    async def test_low_confidence_fallback_preserves_original_verdict(self):
        self.answer["confidence"] = 0.3
        self.answer["probabilities"] = {
            "是": 0.48,
            "否": 0.24,
            "不重要": 0.18,
            "是也不是": 0.10,
        }

        reply, source = await self.plugin.judge_question("Test?", "Test answer")

        self.assertEqual(reply, "否")
        self.assertEqual(source["engine"], "llm")
        self.assertTrue(source["fallback"])
        self.assertEqual(source["jev"]["choice"], "是")
        self.assertEqual(source["jev"]["confidence"], 0.3)
        self.assertEqual(source["jev"]["threshold"], 0.5)
        self.assertEqual(source["jev"]["reason"], "low_confidence")
        self.assertEqual(source["jev"]["probabilities"], self.answer["probabilities"])
        self.judge_provider.text_chat.assert_awaited_once()
        self.hint_provider.text_chat.assert_not_awaited()
        self.verify_provider.text_chat.assert_not_awaited()

    async def test_confidence_equal_to_threshold_is_accepted(self):
        self.answer["confidence"] = 0.5

        reply, source = await self.plugin.judge_question("Test?", "Test answer")

        self.assertEqual(reply, "是")
        self.assertEqual(source["engine"], "jev")
        self.assertIsNone(source["jev"]["reason"])
        self.judge_provider.text_chat.assert_not_awaited()

    async def test_http_failure_falls_back_with_empty_diagnostics(self):
        self.response_status = 503

        reply, source = await self.plugin.judge_question("Test?", "Test answer")

        self.assertEqual(reply, "否")
        self.assertEqual(source["engine"], "llm")
        self.assertTrue(source["fallback"])
        self.assertEqual(source["jev"]["reason"], "request_failed")
        self.assertIsNone(source["jev"]["choice"])
        self.assertIsNone(source["jev"]["confidence"])
        self.assertEqual(source["jev"]["probabilities"], {})

    async def test_timeout_falls_back_without_losing_failure_reason(self):
        self.request_error = httpx.ReadTimeout("Synthetic timeout")

        reply, source = await self.plugin.judge_question("Test?", "Test answer")

        self.assertEqual(reply, "否")
        self.assertEqual(source["jev"]["reason"], "request_failed")
        self.assertTrue(source["fallback"])

    async def test_invalid_confidence_is_rejected_and_json_safe(self):
        for confidence in (None, float("nan"), float("inf"), -0.01, 1.01, "0.8", True):
            with self.subTest(confidence=confidence):
                self.answer["confidence"] = confidence
                result = await self.plugin._jev_choice(
                    {}, "Choose.", self.plugin._JUDGE_CRITERIA
                )

                self.assertEqual(result["reason"], "invalid_response")
                self.assertIsNone(result["confidence"])
                self.assertEqual(result["probabilities"], self.answer["probabilities"])
                json.dumps(result, allow_nan=False)

    async def test_missing_confidence_does_not_become_zero(self):
        del self.answer["confidence"]

        result = await self.plugin._jev_choice(
            {}, "Choose.", self.plugin._JUDGE_CRITERIA
        )

        self.assertEqual(result["reason"], "invalid_response")
        self.assertIsNone(result["confidence"])

    async def test_missing_probabilities_are_not_fabricated(self):
        del self.answer["probabilities"]

        result = await self.plugin._jev_choice(
            {}, "Choose.", self.plugin._JUDGE_CRITERIA
        )

        self.assertEqual(result["probabilities"], {})

    async def test_partial_probabilities_are_not_filled_or_normalized(self):
        self.answer["probabilities"] = {"是": 0.61, "否": 0.1}

        result = await self.plugin._jev_choice(
            {}, "Choose.", self.plugin._JUDGE_CRITERIA
        )

        self.assertEqual(result["probabilities"], {"是": 0.61, "否": 0.1})

    async def test_invalid_probabilities_are_omitted_without_fabrication(self):
        for probability in (None, float("nan"), float("inf"), -0.1, 1.1, "0.1", True):
            with self.subTest(probability=probability):
                self.answer["probabilities"] = {
                    "是": 0.61,
                    "否": probability,
                    "不重要": 0.0,
                    "unknown": 0.39,
                }

                result = await self.plugin._jev_choice(
                    {}, "Choose.", self.plugin._JUDGE_CRITERIA
                )

                self.assertEqual(result["probabilities"], {"是": 0.61, "不重要": 0.0})
                json.dumps(result, allow_nan=False)

    async def test_unknown_choice_is_recorded_before_fallback(self):
        self.answer["choice"] = "unknown"

        reply, source = await self.plugin.judge_question("Test?", "Test answer")

        self.assertEqual(reply, "否")
        self.assertEqual(source["jev"]["reason"], "unknown_choice")
        self.assertEqual(source["jev"]["choice"], "unknown")
        self.assertEqual(source["jev"]["probabilities"], self.answer["probabilities"])

    async def test_malformed_response_is_distinct_from_request_failure(self):
        for response in ([], {}, {"answers": []}, {"answers": {"verdict": None}}):
            with self.subTest(response=response):
                self.response_data = response
                result = await self.plugin._jev_choice(
                    {}, "Choose.", self.plugin._JUDGE_CRITERIA
                )

                self.assertEqual(result["reason"], "invalid_response")
                json.dumps(result, allow_nan=False)

    async def test_disabled_fallback_accepts_valid_low_confidence(self):
        self.plugin.judge_fallback_to_llm = False
        self.answer["confidence"] = 0.0

        reply, source = await self.plugin.judge_question("Test?", "Test answer")

        self.assertEqual(reply, "是")
        self.assertEqual(source["engine"], "jev")
        self.assertEqual(source["jev"]["threshold"], 0.0)
        self.assertEqual(source["jev"]["confidence"], 0.0)
        self.assertIsNone(source["jev"]["reason"])
        self.judge_provider.text_chat.assert_not_awaited()

    async def test_disabled_fallback_keeps_error_diagnostics(self):
        self.plugin.judge_fallback_to_llm = False
        self.response_status = 503

        _, source = await self.plugin.judge_question("Test?", "Test answer")

        self.assertEqual(source["engine"], "unavailable")
        self.assertEqual(source["jev"]["reason"], "request_failed")
        self.assertEqual(source["jev"]["threshold"], 0.0)
        self.judge_provider.text_chat.assert_not_awaited()

    async def test_disabled_fallback_still_rejects_invalid_confidence(self):
        self.plugin.judge_fallback_to_llm = False
        self.answer["confidence"] = None

        _, source = await self.plugin.judge_question("Test?", "Test answer")

        self.assertEqual(source["engine"], "unavailable")
        self.assertEqual(source["jev"]["reason"], "invalid_response")
        self.judge_provider.text_chat.assert_not_awaited()

    async def test_unavailable_fallback_provider_keeps_jev_diagnostics(self):
        self.answer["confidence"] = 0.2
        self.providers.clear()

        _, source = await self.plugin.judge_question("Test?", "Test answer")

        self.assertEqual(source["engine"], "unavailable")
        self.assertEqual(source["jev"]["confidence"], 0.2)
        self.assertEqual(source["jev"]["reason"], "low_confidence")
        self.assertEqual(source["jev"]["probabilities"], self.answer["probabilities"])

    async def test_invalid_llm_reply_keeps_jev_diagnostics(self):
        self.answer["confidence"] = 0.2
        self.judge_provider.text_chat.return_value = SimpleNamespace(
            completion_text="Invalid free-form answer"
        )

        _, source = await self.plugin.judge_question("Test?", "Test answer")

        self.assertEqual(source["engine"], "unavailable")
        self.assertEqual(source["jev"]["reason"], "low_confidence")
        self.assertEqual(source["jev"]["probabilities"], self.answer["probabilities"])

    async def test_failed_llm_keeps_jev_diagnostics(self):
        self.answer["confidence"] = 0.2
        self.judge_provider.text_chat.side_effect = RuntimeError(
            "Synthetic LLM failure"
        )

        _, source = await self.plugin.judge_question("Test?", "Test answer")

        self.assertEqual(source["engine"], "unavailable")
        self.assertEqual(source["jev"]["reason"], "low_confidence")
        self.assertEqual(source["jev"]["probabilities"], self.answer["probabilities"])

    async def test_llm_mode_skips_jev_and_uses_judge_provider(self):
        self.plugin.judge_engine = "llm"

        result = await self.plugin._jev_choice(
            {}, "Choose.", self.plugin._JUDGE_CRITERIA
        )
        reply, source = await self.plugin.judge_question("Test?", "Test answer")

        self.assertIsNone(result)
        self.assertEqual(reply, "否")
        self.assertEqual(source["engine"], "llm")
        self.assertFalse(source["fallback"])
        self.assertIsNone(source.get("jev"))
        self.client_factory.assert_not_called()
        self.judge_provider.text_chat.assert_awaited_once()
        self.hint_provider.text_chat.assert_not_awaited()
        self.verify_provider.text_chat.assert_not_awaited()

    async def test_followup_uses_player_context_without_an_extra_jev_request(self):
        history = [{"question": "救援人员救的是小秋吗？", "answer": "是"}]
        reply, source = await self.plugin.judge_question(
            "那她当时戴着项链吗？",
            "小夏戴着项链，小秋没戴。",
            "session-one",
            "母亲认错了双胞胎。",
            qa_history=history,
        )
        self.assertEqual(reply, "否")
        self.assertEqual(source["routing_reason"], "context_required")
        self.assertFalse(source["fallback"])
        self.assertNotIn("jev", source)
        self.client_factory.assert_not_called()
        payload = json.loads(self.judge_provider.text_chat.call_args.kwargs["prompt"])
        self.assertEqual(payload["相关问答"], history)
        self.assertEqual(payload["谜面"], "母亲认错了双胞胎。")
        self.plugin.context.get_provider_by_id.assert_called_once_with("judge")

    async def test_followup_without_context_asks_for_clarification_without_models(self):
        for engine in ("jev", "llm"):
            with self.subTest(engine=engine):
                self.plugin.judge_engine = engine
                reply, source = await self.plugin.judge_question(
                    "那她也进去了吗？", "Story"
                )
                self.assertEqual(source["engine"], "unavailable")
                self.assertIn("具体人物", reply)
        self.client_factory.assert_not_called()
        self.judge_provider.text_chat.assert_not_awaited()

    async def test_compound_claims_use_llm_but_single_negations_still_use_jev(self):
        self.judge_provider.text_chat.return_value.completion_text = "是也不是。"
        for question in (
            "医生关了灯，护士也进来了，对吗？",
            "他在十楼上班，而且车也停在十楼，对吗？",
            "他既借了书又卖掉了书吗？",
        ):
            with self.subTest(question=question):
                reply, source = await self.plugin.judge_question(question, "Story")
                self.assertEqual(reply, "是也不是")
                self.assertEqual(source["routing_reason"], "compound_question")
                self.assertNotIn("jev", source)
        self.client_factory.assert_not_called()
        reply, source = await self.plugin.judge_question(
            "他并不是没有坐电梯，对吗？", "Story"
        )
        self.assertEqual(source["engine"], "jev")

    async def test_disabled_or_unavailable_review_asks_to_split_instead_of_guessing(
        self,
    ):
        for fallback in (False, True):
            with self.subTest(fallback=fallback):
                self.plugin.judge_fallback_to_llm = fallback
                self.providers.clear()
                reply, source = await self.plugin.judge_question(
                    "医生关了灯，护士也进来了，对吗？", "Story"
                )
                self.assertEqual(source["engine"], "unavailable")
                self.assertIn("拆成", reply)
        self.client_factory.assert_not_called()
        self.judge_provider.text_chat.assert_not_awaited()

    async def test_engines_share_facts_and_criteria_and_history_is_bounded(self):
        history = [{"question": f"Question {i}", "answer": "是"} for i in range(8)]
        await self.plugin.judge_question(
            "现在检查灯光吗？",
            "Secret story",
            puzzle="Public puzzle",
            qa_history=history,
        )
        jev = json.loads(self.requests[-1].content)
        self.assertEqual(len(jev["state"]["相关问答"]), 3)
        self.assertEqual(jev["state"]["相关问答"][0]["question"], "Question 5")
        self.plugin.judge_engine = "llm"
        await self.plugin.judge_question(
            "现在检查灯光吗？",
            "Secret story",
            puzzle="Public puzzle",
            qa_history=history,
        )
        llm = self.judge_provider.text_chat.call_args.kwargs
        self.assertEqual(json.loads(llm["prompt"]), jev["state"])
        self.assertIn(jev["questions"]["verdict"]["instructions"], llm["system_prompt"])
        for value in jev["questions"]["verdict"]["criteria"].values():
            self.assertIn(value, llm["system_prompt"])

    async def test_history_drops_invalid_answers_and_unrelated_metadata(self):
        history = [
            {"question": "Bad", "answer": ["是"]},
            {"question": "Error", "answer": "Service unavailable"},
            {"question": "Valid", "answer": "否", "secret": "must not be sent"},
        ]
        await self.plugin.judge_question("Check?", "Story", qa_history=history)
        payload = json.loads(self.requests[-1].content)
        self.assertEqual(
            payload["state"]["相关问答"], [{"question": "Valid", "answer": "否"}]
        )

    async def test_blank_or_oversized_questions_never_call_a_model(self):
        for question in ("   ", "问" * 2001):
            _, source = await self.plugin.judge_question(question, "Story")
            self.assertEqual(source["engine"], "unavailable")
        self.client_factory.assert_not_called()
        self.judge_provider.text_chat.assert_not_awaited()

    async def test_llm_clarification_and_prose_never_become_a_verdict(self):
        self.plugin.judge_engine = "llm"
        for reply in (
            "需要澄清",
            "是，因为实际情况是秘密。",
            "是也不是，原因是秘密",
            "不是",
        ):
            with self.subTest(reply=reply):
                self.judge_provider.text_chat.return_value.completion_text = reply
                text, source = await self.plugin.judge_question("Check?", "Story")
                self.assertEqual(source["engine"], "unavailable")
                self.assertNotIn("秘密", text)
        for reply in ("是也不是。", "“否”", " 是！ "):
            self.judge_provider.text_chat.return_value.completion_text = reply
            text, source = await self.plugin.judge_question("Check?", "Story")
            self.assertEqual(source["engine"], "llm")
            self.assertIn(text, self.plugin._JUDGE_CRITERIA)
        self.client_factory.assert_not_called()

    async def test_provider_resolution_failure_is_a_retryable_judgment_failure(self):
        self.plugin.judge_engine = "llm"
        self.plugin.context.get_provider_by_id.side_effect = RuntimeError(
            "Provider unavailable"
        )
        reply, source = await self.plugin.judge_question("Check?", "Story")
        self.assertEqual(source["engine"], "unavailable")
        self.assertIn("重试", reply)
        self.judge_provider.text_chat.assert_not_awaited()

    async def test_judge_cancellation_propagates_without_triggering_fallback(self):
        self.request_error = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.plugin.judge_question("Check?", "Story")
        self.judge_provider.text_chat.assert_not_awaited()

    async def test_total_request_timeouts_cancel_pending_operations(self):
        real_wait_for = asyncio.wait_for
        limits = []

        async def short_wait(awaitable, timeout):
            limits.append(timeout)
            return await real_wait_for(awaitable, timeout=0.02)

        async def pending(*args, **kwargs):
            await asyncio.Event().wait()

        with patch.object(self.module.asyncio, "wait_for", side_effect=short_wait):
            self.plugin._jev_client = SimpleNamespace(
                post=AsyncMock(side_effect=pending)
            )
            reply, source = await self.plugin.judge_question("Check?", "Story")
            self.assertEqual(reply, "否")
            self.assertEqual(source["jev"]["reason"], "request_failed")
            self.assertEqual(limits, [15.0, 60.0])
            self.plugin.judge_engine = "llm"
            self.judge_provider.text_chat.side_effect = pending
            _, source = await self.plugin.judge_question("Check?", "Story")
            self.assertEqual(source["engine"], "unavailable")

    async def test_config_changes_reuse_pool_and_apply_current_credentials(self):
        await self.plugin.judge_question("First?", "Story")
        client = self.plugin._jev_client
        self.plugin.config.update(
            jev_base_url="https://other.test/v1/", jev_api_key="new-test-key"
        )
        self.plugin._load_config()
        self.assertIs(self.plugin._jev_client, client)
        await self.plugin.judge_question("Second?", "Story")
        self.assertEqual(self.client_factory.call_count, 1)
        self.assertEqual(
            str(self.requests[0].url), "https://typesafe.test/v1/systemone"
        )
        self.assertEqual(self.requests[0].headers["Authorization"], "Bearer test-key")
        self.assertEqual(str(self.requests[1].url), "https://other.test/v1/systemone")
        self.assertEqual(
            self.requests[1].headers["Authorization"], "Bearer new-test-key"
        )

    async def test_contradictory_distributions_and_wrong_answer_types_fall_back(self):
        for probabilities in (
            {"是": 0.1, "否": 0.8, "不重要": 0.05, "是也不是": 0.05},
            {"是": 0.8, "否": 0.8, "不重要": 0.8, "是也不是": 0.8},
        ):
            self.answer["probabilities"] = probabilities
            _, source = await self.plugin.judge_question("Check?", "Story")
            self.assertEqual(source["engine"], "llm")
            self.assertEqual(source["jev"]["reason"], "invalid_response")
            self.assertEqual(source["jev"]["probabilities"], probabilities)
        self.answer["type"] = "score"
        _, source = await self.plugin.judge_question("Check?", "Story")
        self.assertEqual(source["jev"]["reason"], "invalid_response")

    def test_invalid_threshold_config_uses_safe_default(self):
        for value in (None, "invalid", -1, 1.1, float("nan"), float("inf")):
            with self.subTest(value=value):
                self.plugin.config["jev_judge_min_confidence"] = value
                self.plugin._load_config()
                self.assertEqual(self.plugin.jev_judge_min_confidence, 0.5)

    async def test_hints_use_only_the_hint_provider(self):
        hint = await self.plugin.generate_hint(
            "Test puzzle", "Test answer", [], [], [], umo="test-session"
        )

        self.assertEqual(hint, "关注【时间】：比较先后")
        self.plugin.context.get_provider_by_id.assert_called_once_with("hint")
        self.hint_provider.text_chat.assert_awaited_once()
        self.judge_provider.text_chat.assert_not_awaited()
        self.verify_provider.text_chat.assert_not_awaited()
        self.client_factory.assert_not_called()

    async def test_missing_explicit_hint_provider_does_not_use_judge(self):
        del self.providers["hint"]

        hint = await self.plugin.generate_hint(
            "Test puzzle", "Test answer", [], [], [], umo="test-session"
        )

        self.assertIn("无法提供提示", hint)
        self.plugin.context.get_provider_by_id.assert_called_once_with("hint")
        self.judge_provider.text_chat.assert_not_awaited()
        self.default_provider.text_chat.assert_not_awaited()
        self.client_factory.assert_not_called()

    async def test_blank_hint_provider_follows_judge_provider(self):
        self.judge_provider.text_chat.return_value = SimpleNamespace(
            completion_text="判断模型生成的提示"
        )
        for hint_provider in ("", "   "):
            with self.subTest(hint_provider=hint_provider):
                self.plugin.config["hint_llm_provider"] = hint_provider
                self.plugin._load_config()
                self.plugin.context.get_provider_by_id.reset_mock()
                hint = await self.plugin.generate_hint(
                    "Test puzzle", "Test answer", [], [], [], umo="test-session"
                )

                self.assertEqual(hint, "判断模型生成的提示")
                self.plugin.context.get_provider_by_id.assert_called_once_with("judge")
        self.hint_provider.text_chat.assert_not_awaited()
        self.client_factory.assert_not_called()

    async def test_blank_hint_and_judge_follow_session_provider(self):
        self.plugin.config["hint_llm_provider"] = ""
        self.plugin.config["judge_llm_provider"] = ""
        self.plugin._load_config()

        hint = await self.plugin.generate_hint(
            "Test puzzle", "Test answer", [], [], [], umo="test-session"
        )

        self.assertEqual(hint, "默认提示")
        self.plugin.context.get_using_provider.assert_called_once_with(
            umo="test-session"
        )
        self.plugin.context.get_provider_by_id.assert_not_called()
        self.default_provider.text_chat.assert_awaited_once()

    async def test_missing_verify_configuration_follows_judge_provider(self):
        del self.plugin.config["verify_llm_provider"]
        self.plugin._load_config()
        self.judge_provider.text_chat.return_value = SimpleNamespace(
            completion_text="事实：95\n动机：90\n反转：95\n评价：推理正确"
        )

        result = await self.plugin.verify_user_guess(
            "Test guess", "Test answer", umo="test-session"
        )

        self.assertTrue(result.is_correct)
        self.assertEqual(result.level, "完全还原")
        self.plugin.context.get_provider_by_id.assert_called_once_with("judge")
        self.judge_provider.text_chat.assert_awaited_once()
        self.hint_provider.text_chat.assert_not_awaited()
        self.verify_provider.text_chat.assert_not_awaited()
        self.client_factory.assert_not_called()

    async def test_verification_uses_its_own_provider(self):
        self.plugin.config["verify_llm_provider"] = " verify "
        self.plugin._load_config()

        result = await self.plugin.verify_user_guess(
            "Test guess", "Test answer", umo="test-session"
        )

        self.assertEqual(self.plugin.verify_llm_provider_id, "verify")
        self.assertTrue(result.is_correct)
        self.assertEqual(result.level, "完全还原")
        self.plugin.context.get_provider_by_id.assert_called_once_with("verify")
        self.verify_provider.text_chat.assert_awaited_once()
        self.judge_provider.text_chat.assert_not_awaited()
        self.hint_provider.text_chat.assert_not_awaited()
        self.client_factory.assert_not_called()

    async def test_blank_verify_provider_follows_judge_provider(self):
        self.judge_provider.text_chat.return_value = SimpleNamespace(
            completion_text="事实：95\n动机：90\n反转：95\n评价：推理正确"
        )
        for verify_provider in ("", "   ", None):
            with self.subTest(verify_provider=verify_provider):
                self.plugin.config["verify_llm_provider"] = verify_provider
                self.plugin._load_config()
                self.plugin.context.get_provider_by_id.reset_mock()

                result = await self.plugin.verify_user_guess(
                    "Test guess", "Test answer", umo="test-session"
                )

                self.assertTrue(result.is_correct)
                self.plugin.context.get_provider_by_id.assert_called_once_with("judge")
        self.verify_provider.text_chat.assert_not_awaited()
        self.hint_provider.text_chat.assert_not_awaited()
        self.client_factory.assert_not_called()

    async def test_blank_verify_and_judge_follow_session_provider(self):
        self.plugin.config["verify_llm_provider"] = ""
        self.plugin.config["judge_llm_provider"] = ""
        self.plugin._load_config()
        self.default_provider.text_chat.return_value = SimpleNamespace(
            completion_text="事实：95\n动机：90\n反转：95\n评价：推理正确"
        )

        result = await self.plugin.verify_user_guess(
            "Test guess", "Test answer", umo="test-session"
        )

        self.assertTrue(result.is_correct)
        self.plugin.context.get_using_provider.assert_called_once_with(
            umo="test-session"
        )
        self.plugin.context.get_provider_by_id.assert_not_called()
        self.default_provider.text_chat.assert_awaited_once()
        self.judge_provider.text_chat.assert_not_awaited()
        self.hint_provider.text_chat.assert_not_awaited()
        self.verify_provider.text_chat.assert_not_awaited()
        self.client_factory.assert_not_called()

    async def test_missing_explicit_verify_provider_does_not_use_judge(self):
        del self.providers["verify"]

        result = await self.plugin.verify_user_guess(
            "Test guess", "Test answer", umo="test-session"
        )

        self.assertFalse(result.is_correct)
        self.assertEqual(result.level, "验证失败")
        self.plugin.context.get_provider_by_id.assert_called_once_with("verify")
        self.plugin.context.get_using_provider.assert_not_called()
        self.judge_provider.text_chat.assert_not_awaited()
        self.hint_provider.text_chat.assert_not_awaited()
        self.default_provider.text_chat.assert_not_awaited()
        self.client_factory.assert_not_called()

    async def test_failed_verify_provider_does_not_use_judge(self):
        self.verify_provider.text_chat.side_effect = RuntimeError(
            "Synthetic verification failure"
        )

        result = await self.plugin.verify_user_guess(
            "Test guess", "Test answer", umo="test-session"
        )

        self.assertFalse(result.is_correct)
        self.assertEqual(result.level, "验证失败")
        self.plugin.context.get_provider_by_id.assert_called_once_with("verify")
        self.verify_provider.text_chat.assert_awaited_once()
        self.judge_provider.text_chat.assert_not_awaited()
        self.hint_provider.text_chat.assert_not_awaited()
        self.default_provider.text_chat.assert_not_awaited()
        self.client_factory.assert_not_called()

    def test_verification_score_is_the_weighted_average_of_its_parts(self):
        result = self.plugin._parse_verification_result(
            "事实：80\n动机：60\n反转：40\n评价：主干接近"
        )

        # 0.35*80 + 0.25*60 + 0.40*40 = 59
        self.assertEqual(result.score, 59)
        self.assertEqual(result.breakdown, {"facts": 80, "motive": 60, "twist": 40})
        self.assertEqual(result.comment, "主干接近")
        self.assertEqual(result.level, "部分正确")
        self.assertFalse(result.is_correct)

    def test_verification_tolerates_half_width_colons_and_stray_lines(self):
        result = self.plugin._parse_verification_result(
            "这是多余的前言\n事实: 90\n动机：90\n反转: 90\n评价: 很接近了\n多余的解释"
        )

        self.assertEqual(result.score, 90)
        self.assertEqual(result.comment, "很接近了")
        self.assertEqual(result.level, "完全还原")

    def test_verification_renormalizes_when_a_dimension_is_missing(self):
        result = self.plugin._parse_verification_result(
            "事实：80\n反转：40\n评价：少了一项"
        )

        # 缺动机时按剩下的 0.35 / 0.40 归一化，而不是把缺项当 0 分
        self.assertEqual(result.score, 59)
        self.assertEqual(result.breakdown, {"facts": 80, "twist": 40})

    def test_verification_clamps_out_of_range_scores(self):
        result = self.plugin._parse_verification_result(
            "事实：120\n动机：100\n反转：999\n评价：越界"
        )

        self.assertEqual(result.breakdown, {"facts": 100, "motive": 100, "twist": 100})
        self.assertEqual(result.score, 100)

    def test_verification_without_any_score_line_reports_no_score(self):
        result = self.plugin._parse_verification_result("我无法评价这段推理。")

        # score=None 让调用方放弃本次验证，而不是当成 0 分扣掉一次机会
        self.assertIsNone(result.score)
        self.assertEqual(result.level, "验证失败")
        self.assertFalse(result.is_correct)

    def test_verification_falls_back_to_a_spoiler_free_comment(self):
        result = self.plugin._parse_verification_result("事实：20\n动机：20\n反转：20")

        self.assertEqual(result.score, 20)
        self.assertEqual(result.comment, "方向偏了，换个角度重新想想。")

    def test_pass_score_prefers_the_config_override_then_the_round(self):
        self.plugin.difficulty_settings = {
            "普通": {"limit": 35, "pass_score": 70, "hint_limit": 5}
        }
        self.plugin.verification_pass_score = 0

        self.assertEqual(self.plugin._pass_score_for({"pass_score": 90}), 90)
        self.assertEqual(self.plugin._pass_score_for(None), 70)
        # 对局里的值不可信时同样回落到普通难度
        self.assertEqual(self.plugin._pass_score_for({"pass_score": 0}), 70)
        self.assertEqual(self.plugin._pass_score_for({"pass_score": "80"}), 70)

        self.plugin.verification_pass_score = 55
        self.assertEqual(self.plugin._pass_score_for({"pass_score": 90}), 55)

    def test_invalid_pass_score_config_falls_back_to_difficulty(self):
        for raw in (-1, 101, "abc", None):
            with self.subTest(raw=raw):
                self.plugin.config["verification_pass_score"] = raw
                self.plugin._load_config()
                self.assertEqual(self.plugin.verification_pass_score, 0)


if __name__ == "__main__":
    unittest.main()
