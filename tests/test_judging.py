"""Regression tests for Jev diagnostics and independent hint providers."""

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
                    completion_text="等级：完全还原\n评价：推理正确"
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
            completion_text="等级：完全还原\n评价：推理正确"
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
            completion_text="等级：完全还原\n评价：推理正确"
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
            completion_text="等级：完全还原\n评价：推理正确"
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


if __name__ == "__main__":
    unittest.main()
