"""Exercise catalog admission and derived caches without production data."""

import copy
import importlib.util
import json
import logging
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch


class CatalogTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        """Import the catalog with only an isolated logger standing in for AstrBot."""
        logger = logging.getLogger("soupai.catalog.tests")
        logger.addHandler(logging.NullHandler())
        logger.propagate = False
        api = ModuleType("astrbot.api")
        api.logger = logger
        spec = importlib.util.spec_from_file_location(
            "soupai_catalog_tests",
            Path(__file__).resolve().parents[1] / "story_catalog.py",
        )
        module = importlib.util.module_from_spec(spec)
        with patch.dict(
            sys.modules,
            {"astrbot": ModuleType("astrbot"), "astrbot.api": api, spec.name: module},
        ):
            spec.loader.exec_module(module)
        cls.catalog_class = module.StoryCatalog

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="soupai-catalog-tests-")
        self.addCleanup(temporary.cleanup)
        self.data_path = Path(temporary.name)
        self.storages = {
            source: SimpleNamespace(
                stories=[],
                lock=threading.RLock(),
                story_id=lambda story, index: str(story["id"]),
            )
            for source in ("network", "local", "custom")
        }
        self.annotation = {
            "theme": "医院",
            "tags": ["灯光", "医生", "检查"],
            "summary": "医生为了进行需要黑暗环境的检查而关闭照明。",
            "causal_chain": ["病人需要视觉检查", "照明干扰检查", "医生关闭照明"],
            "twist": "关灯是在帮助诊断，不是忽视病人。",
        }
        self.llm = SimpleNamespace(text_chat=AsyncMock())
        self.providers = {}
        self.plugin = SimpleNamespace(
            data_path=self.data_path,
            config={"dedup_semantic_enabled": False},
            _storage_of=Mock(side_effect=self.storages.get),
            _resolve_provider=Mock(return_value=self.llm),
            context=SimpleNamespace(
                get_provider_by_id=Mock(side_effect=self.providers.get)
            ),
        )
        self.catalog = self.catalog_class(self.plugin)
        self._set_llm_responses(self.annotation)

    def _put(self, source="network", story_id="existing", puzzle=None, answer=None):
        """Insert a synthetic story into one in-memory source.

        Args:
            source: Target fake bank.
            story_id: Synthetic stable identifier.
            puzzle: Optional puzzle text.
            answer: Optional solution text.

        Returns:
            The mutable source record for simulating later edits.
        """
        story = {
            "id": story_id,
            "puzzle": puzzle or "医生在检查前熄灭了医院走廊的灯。",
            "answer": answer or "走廊灯光干扰了病人的视觉检查，医生于是关闭照明。",
        }
        self.storages[source].stories.append(story)
        return story

    def _set_llm_responses(self, *payloads):
        """Supply raw or JSON model responses in request order.

        Args:
            *payloads: Strings or JSON-compatible objects to return.
        """
        self.llm.text_chat.reset_mock()
        self.llm.text_chat.side_effect = [
            SimpleNamespace(
                completion_text=(
                    payload
                    if isinstance(payload, str)
                    else json.dumps(payload, ensure_ascii=False)
                )
            )
            for payload in payloads
        ]

    def _prepare_semantic_check(self):
        """Prepare one annotated candidate and an unrelated same-theme proposal."""
        self.plugin.config["dedup_semantic_enabled"] = True
        self.existing = self._put()
        self.catalog.remember(
            "network",
            self.existing["id"],
            self.existing["puzzle"],
            self.existing["answer"],
            self.annotation,
        )
        self.proposed = (
            "护士在医院白天把一盏灯关掉，病人却很高兴。",
            "这盏灯显示病房有空床，关灯意味着等候的病人终于获准入住。",
        )
        self.proposed_annotation = {
            "theme": "医院",
            "tags": ["灯光", "护士", "床位"],
            "summary": "床位指示灯熄灭代表病人获得住院床位。",
            "causal_chain": ["病人在等候床位", "护士分配床位", "护士关闭空床指示灯"],
            "twist": "灯是空床指示信号，不是照明设施。",
        }
        self.distinct_review = {
            "matches": [
                {"index": 0, "duplicate": False, "reason": "灯的用途及关灯的因果不同"}
            ]
        }
        self._set_llm_responses(self.proposed_annotation, self.distinct_review)

    async def test_exact_duplicates_are_found_across_all_three_banks(self):
        for source in self.storages:
            self._put(source, f"{source}-id", "同一个题面", "同一个汤底")

        result = await self.catalog.check("同一个题面", "同一个汤底")

        self.assertTrue(result["duplicate"])
        self.assertEqual(
            {item["source"] for item in result["matches"]}, set(self.storages)
        )
        self.assertTrue(all(item["kind"] == "exact" for item in result["matches"]))
        self.assertTrue(all("answer" not in item for item in result["matches"]))
        self.llm.text_chat.assert_not_awaited()

    async def test_exact_comparison_normalizes_width_whitespace_and_punctuation(self):
        self._put("custom", "cosmetic", "Ａ 女孩\n在车站！", "父 亲迟到了。")

        result = await self.catalog.check("a女孩在车站", "父亲迟到了")

        self.assertTrue(result["duplicate"])
        self.assertEqual(result["matches"][0]["kind"], "exact")
        self.llm.text_chat.assert_not_awaited()

    async def test_identical_puzzles_and_identical_answers_report_distinct_conflicts(
        self,
    ):
        self._put("local", "one", "原题面", "原汤底")
        for puzzle, answer, kind in (
            ("原题面", "另一种原因", "puzzle_conflict"),
            ("改写后的题面", "原汤底", "answer_duplicate"),
        ):
            with self.subTest(kind=kind):
                result = await self.catalog.check(puzzle, answer)

                self.assertTrue(result["duplicate"])
                self.assertEqual(result["matches"][0]["kind"], kind)

    async def test_edit_exclusion_only_removes_the_matching_source_and_id(self):
        self._put("local", "same-id", "题面", "汤底")
        self._put("custom", "same-id", "题面", "汤底")

        result = await self.catalog.check("题面", "汤底", exclude=("local", "same-id"))

        self.assertTrue(result["duplicate"])
        self.assertEqual([item["source"] for item in result["matches"]], ["custom"])
        self.storages["custom"].stories.clear()
        result = await self.catalog.check("题面", "汤底", exclude=("local", "same-id"))
        self.assertFalse(result["duplicate"])
        self.llm.text_chat.assert_not_awaited()

    async def test_annotation_is_cached_in_sidecar_without_mutating_network_source(
        self,
    ):
        story = self._put()
        original = copy.deepcopy(self.storages["network"].stories)

        result = await self.catalog.annotate("network", story["id"], umo="test-session")
        reloaded = self.catalog_class(self.plugin)
        cached = await reloaded.annotate("network", story["id"])

        self.assertEqual(result, self.annotation)
        self.assertEqual(cached, self.annotation)
        self.assertEqual(self.storages["network"].stories, original)
        self.llm.text_chat.assert_awaited_once()
        self.plugin._resolve_provider.assert_called_once_with("", "test-session")
        sidecar = json.loads((self.data_path / "story_catalog.json").read_text("utf-8"))
        self.assertEqual(sidecar["version"], 1)
        self.assertEqual(set(sidecar["annotations"]), {"network:existing"})
        result["tags"].append("caller change")
        self.assertNotIn(
            "caller change",
            reloaded.get_annotation(
                "network", story["id"], story["puzzle"], story["answer"]
            )["tags"],
        )

    async def test_edit_makes_old_annotation_stale_until_recomputed(self):
        story = self._put()
        await self.catalog.annotate("network", story["id"])
        story["answer"] = "医院停电，医生带病人到有自然光的房间完成检查。"

        self.assertEqual(
            self.catalog.annotation_status(
                "network", story["id"], story["puzzle"], story["answer"]
            ),
            "stale",
        )
        self.assertIsNone(
            self.catalog.get_annotation(
                "network", story["id"], story["puzzle"], story["answer"]
            )
        )
        updated = {**self.annotation, "summary": "停电后转移到有自然光的检查室。"}
        self._set_llm_responses(updated)
        self.assertEqual(await self.catalog.annotate("network", story["id"]), updated)
        self.assertEqual(
            self.catalog.annotation_status(
                "network", story["id"], story["puzzle"], story["answer"]
            ),
            "ready",
        )
        self.llm.text_chat.assert_awaited_once()

    def test_annotation_keys_are_isolated_by_bank_and_forget_keeps_other_banks(self):
        local_annotation = {**self.annotation, "theme": "本地医院"}
        self.catalog.remember("local", "same-id", "题面", "汤底", local_annotation)
        self.catalog.remember("custom", "same-id", "题面", "汤底", self.annotation)

        self.catalog.forget("local", "same-id")

        self.assertIsNone(
            self.catalog.get_annotation("local", "same-id", "题面", "汤底")
        )
        self.assertEqual(
            self.catalog.get_annotation("custom", "same-id", "题面", "汤底"),
            self.annotation,
        )

    async def test_fenced_json_annotation_is_accepted(self):
        story = self._put()
        self._set_llm_responses("```json\n" + json.dumps(self.annotation) + "\n```")

        result = await self.catalog.annotate("network", story["id"])

        self.assertEqual(result, self.annotation)

    async def test_blank_annotation_provider_follows_verify_judge_then_session(self):
        story = self._put()
        for config, expected in (
            (
                {"annotation_llm_provider": " ", "verify_llm_provider": " verify "},
                "verify",
            ),
            (
                {
                    "annotation_llm_provider": " ",
                    "verify_llm_provider": " ",
                    "judge_llm_provider": " judge ",
                },
                "judge",
            ),
            (
                {
                    "annotation_llm_provider": " ",
                    "verify_llm_provider": " ",
                    "judge_llm_provider": " ",
                },
                "",
            ),
        ):
            with self.subTest(expected=expected):
                self.plugin.config = config
                self.plugin._resolve_provider.reset_mock()
                self._set_llm_responses(self.annotation)

                await self.catalog.annotate(
                    "network", story["id"], force=True, umo="session"
                )

                self.plugin._resolve_provider.assert_called_once_with(
                    expected, "session"
                )

    async def test_invalid_annotation_json_or_schema_never_creates_cache(self):
        story = self._put()
        for output in (
            "not JSON",
            "Here is the result: " + json.dumps(self.annotation),
            [],
            {"error": "Cannot classify"},
            {},
            {**self.annotation, "tags": "医院"},
            {**self.annotation, "causal_chain": []},
            {**self.annotation, "theme": " "},
            {**self.annotation, "unexpected": True},
        ):
            with self.subTest(output=output):
                self._set_llm_responses(output)
                with self.assertRaises(ValueError):
                    await self.catalog.annotate("network", story["id"])

                self.assertEqual(
                    self.catalog.annotation_status(
                        "network", story["id"], story["puzzle"], story["answer"]
                    ),
                    "missing",
                )
                self.assertFalse((self.data_path / "story_catalog.json").exists())

    async def test_deleting_story_during_annotation_does_not_save_stale_result(self):
        story = self._put()

        async def delete_while_annotating(**kwargs):
            self.storages["network"].stories.clear()
            return SimpleNamespace(completion_text=json.dumps(self.annotation))

        self.llm.text_chat.side_effect = delete_while_annotating

        with self.assertRaises(ValueError):
            await self.catalog.annotate("network", story["id"])

        self.assertFalse((self.data_path / "story_catalog.json").exists())

    async def test_editing_story_during_annotation_does_not_save_stale_result(self):
        story = self._put()

        async def edit_while_annotating(**kwargs):
            story["answer"] = "修改后的另一个真实原因。"
            return SimpleNamespace(completion_text=json.dumps(self.annotation))

        self.llm.text_chat.side_effect = edit_while_annotating

        with self.assertRaises(ValueError):
            await self.catalog.annotate("network", story["id"])

        self.assertFalse((self.data_path / "story_catalog.json").exists())

    async def test_same_theme_with_different_cause_and_twist_is_not_duplicate(self):
        self._prepare_semantic_check()

        report = await self.catalog.check(*self.proposed, umo="semantic-session")

        self.assertFalse(report["duplicate"])
        self.assertEqual(report["annotation"], self.proposed_annotation)
        self.assertEqual(report["matches"], [])
        self.assertIn("llm", report["method"])
        review_prompt = self.llm.text_chat.await_args.kwargs["prompt"]
        self.assertIn(self.existing["answer"], review_prompt)
        self.assertIn(self.proposed[1], review_prompt)
        self.assertEqual(self.llm.text_chat.await_count, 2)

    async def test_paraphrase_of_same_cause_and_twist_is_rejected(self):
        self._prepare_semantic_check()
        self._set_llm_responses(
            self.annotation,
            {
                "matches": [
                    {"index": 0, "duplicate": True, "reason": "均因灯光干扰检查而熄灯"}
                ]
            },
        )

        report = await self.catalog.check(
            "大夫在病人检查前让整条走廊暗了下来。",
            "做视力测试必须去除外部光线干扰，因此大夫把走廊的照明关了。",
        )

        self.assertTrue(report["duplicate"])
        self.assertEqual(report["matches"][0]["kind"], "semantic_duplicate")
        self.assertEqual(report["matches"][0]["source"], "network")
        self.assertEqual(report["matches"][0]["id"], "existing")

    async def test_incomplete_or_invalid_review_cannot_silently_pass(self):
        self._prepare_semantic_check()
        valid = {"index": 0, "duplicate": False, "reason": "因果不同"}
        for review in (
            {"matches": []},
            {"matches": [{**valid, "index": True}]},
            {"matches": [{**valid, "index": 1}]},
            {"matches": [{**valid, "duplicate": "false"}]},
            {"matches": [{**valid, "reason": ""}]},
            {"matches": [{**valid, "extra": "unexpected"}]},
            {"matches": [valid, valid]},
        ):
            with self.subTest(review=review):
                self._set_llm_responses(self.proposed_annotation, review)
                with self.assertRaises(ValueError):
                    await self.catalog.check(*self.proposed)

    async def test_embedding_cache_is_isolated_by_provider_model_and_content(self):
        model = SimpleNamespace(model="model-one")

        async def embed(texts):
            return [[1.0, 0.0] for _ in texts]

        embedding = SimpleNamespace(
            meta=Mock(return_value=model), get_embeddings=AsyncMock(side_effect=embed)
        )
        other = SimpleNamespace(
            meta=Mock(return_value=model), get_embeddings=AsyncMock(side_effect=embed)
        )
        self.providers.update(embedding=embedding, other=other)
        texts = ["query", "candidate"]

        await self.catalog._embedding_vectors("embedding", texts)
        await self.catalog._embedding_vectors("embedding", texts)
        embedding.get_embeddings.assert_awaited_once()
        await self.catalog._embedding_vectors(
            "embedding", ["query", "edited candidate"]
        )
        self.assertEqual(
            embedding.get_embeddings.await_args.args[0], ["edited candidate"]
        )
        model.model = "model-two"
        await self.catalog._embedding_vectors("embedding", texts)
        self.assertEqual(embedding.get_embeddings.await_count, 3)
        await self.catalog._embedding_vectors("other", texts)
        other.get_embeddings.assert_awaited_once()
        reloaded = self.catalog_class(self.plugin)
        await reloaded._embedding_vectors("other", texts)
        other.get_embeddings.assert_awaited_once()

    async def test_embedding_batches_are_bounded_and_every_vector_is_returned(self):
        async def embed(texts):
            return [[3.0, 4.0] for _ in texts]

        embedding = SimpleNamespace(
            meta=Mock(return_value=SimpleNamespace(model="bounded-model")),
            get_embeddings=AsyncMock(side_effect=embed),
        )
        self.providers["embedding"] = embedding

        result = await self.catalog._embedding_vectors(
            "embedding", [f"document-{index}" for index in range(35)]
        )

        self.assertEqual(len(result), 35)
        self.assertEqual(
            [len(item.args[0]) for item in embedding.get_embeddings.await_args_list],
            [16, 16, 3],
        )
        self.assertEqual(result[0], [0.6, 0.8])

    async def test_embedding_cache_tracks_adapter_model_and_declared_dimension(self):
        embedding = SimpleNamespace(
            model="adapter-one",
            meta=Mock(return_value=SimpleNamespace(model="")),
            get_dim=Mock(return_value=2),
            get_embeddings=AsyncMock(return_value=[[1.0, 0.0], [0.0, 1.0]]),
        )
        self.providers["embedding"] = embedding
        texts = ["query", "candidate"]
        await self.catalog._embedding_vectors("embedding", texts)
        embedding.model = "adapter-two"
        await self.catalog._embedding_vectors("embedding", texts)
        self.assertEqual(embedding.get_embeddings.await_count, 2)
        embedding.get_dim.return_value = 3
        embedding.get_embeddings.return_value = [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]]

        result = await self.catalog._embedding_vectors("embedding", texts)

        self.assertEqual(embedding.get_embeddings.await_count, 3)
        self.assertEqual(len(result[0]), 3)

    async def test_embedding_declared_dimension_is_enforced_but_zero_is_automatic(self):
        embedding = SimpleNamespace(
            meta=Mock(return_value=SimpleNamespace(model="dimension-model")),
            get_dim=Mock(return_value=3),
            get_embeddings=AsyncMock(return_value=[[1.0, 0.0], [0.0, 1.0]]),
        )
        self.providers["embedding"] = embedding

        with self.assertRaises(ValueError):
            await self.catalog._embedding_vectors("embedding", ["query", "candidate"])

        embedding.get_dim.return_value = 0
        result = await self.catalog._embedding_vectors(
            "embedding", ["query", "candidate"]
        )
        self.assertEqual(result, [[1.0, 0.0], [0.0, 1.0]])

    async def test_invalid_embedding_counts_dimensions_or_values_do_not_create_cache(
        self,
    ):
        embedding = SimpleNamespace(
            meta=Mock(return_value=SimpleNamespace(model="invalid-model")),
            get_embeddings=AsyncMock(),
        )
        self.providers["embedding"] = embedding
        for vectors in (
            [[1.0, 0.0]],
            [[1.0, 0.0], [1.0, 0.0, 0.0]],
            [[], [1.0, 0.0]],
            [[0.0, 0.0], [1.0, 0.0]],
            [[float("nan"), 1.0], [1.0, 0.0]],
            [[float("inf"), 1.0], [1.0, 0.0]],
            [[True, 1.0], [1.0, 0.0]],
            [["1", 0.0], [1.0, 0.0]],
        ):
            with self.subTest(vectors=vectors):
                embedding.get_embeddings.return_value = vectors
                with self.assertRaises(ValueError):
                    await self.catalog._embedding_vectors(
                        "embedding", ["query", "candidate"]
                    )

                self.assertFalse((self.data_path / "story_catalog.json").exists())

    async def test_invalid_cached_embedding_dimension_is_not_trusted(self):
        embedding = SimpleNamespace(
            meta=Mock(return_value=SimpleNamespace(model="cached-model")),
            get_embeddings=AsyncMock(return_value=[[1.0, 0.0], [0.0, 1.0]]),
        )
        self.providers["embedding"] = embedding
        await self.catalog._embedding_vectors("embedding", ["query", "candidate"])
        path = self.data_path / "story_catalog.json"
        cached = json.loads(path.read_text("utf-8"))
        namespace = next(iter(cached["embeddings"].values()))
        key = next(iter(namespace["vectors"]))
        namespace["vectors"][key] = [1.0, 0.0, 0.0]
        path.write_text(json.dumps(cached), encoding="utf-8")
        reloaded = self.catalog_class(self.plugin)

        with self.assertRaises(ValueError):
            await reloaded._embedding_vectors("embedding", ["query", "candidate"])

        embedding.get_embeddings.assert_awaited_once()

    async def test_missing_configured_retrieval_provider_blocks_admission(self):
        self._prepare_semantic_check()
        for field in ("dedup_embedding_provider", "dedup_rerank_provider"):
            with self.subTest(field=field):
                self.plugin.config[field] = "missing"
                self._set_llm_responses(self.proposed_annotation)
                with self.assertRaises(ValueError):
                    await self.catalog.check(*self.proposed)
                self.plugin.config.pop(field)

    async def test_invalid_rerank_indexes_or_scores_block_admission(self):
        self._prepare_semantic_check()
        self.plugin.config["dedup_rerank_provider"] = "reranker"
        reranker = SimpleNamespace(rerank=AsyncMock())
        self.providers["reranker"] = reranker
        invalid = [
            [SimpleNamespace(index=index, relevance_score=0.9)]
            for index in (-1, 1, True, 0.5, "0")
        ]
        invalid.extend(
            [SimpleNamespace(index=0, relevance_score=score)]
            for score in (float("nan"), float("inf"), True, "0.9", None)
        )
        invalid.extend([[], None, [SimpleNamespace(index=0, relevance_score=0.9)] * 2])
        for response in invalid:
            with self.subTest(response=response):
                self._set_llm_responses(self.proposed_annotation)
                reranker.rerank.return_value = response
                with self.assertRaises(ValueError):
                    await self.catalog.check(*self.proposed)

                self.llm.text_chat.assert_awaited_once()

    async def test_high_embedding_and_rerank_similarity_is_not_itself_a_duplicate(self):
        self._prepare_semantic_check()
        self.plugin.config.update(
            dedup_embedding_provider="embedding", dedup_rerank_provider="reranker"
        )
        embedding = SimpleNamespace(
            meta=Mock(return_value=SimpleNamespace(model="similarity-model")),
            get_embeddings=AsyncMock(return_value=[[1.0, 0.0], [1.0, 0.0]]),
        )
        reranker = SimpleNamespace(
            rerank=AsyncMock(
                return_value=[SimpleNamespace(index=0, relevance_score=0.99)]
            )
        )
        self.providers.update(embedding=embedding, reranker=reranker)

        report = await self.catalog.check(*self.proposed)

        self.assertFalse(report["duplicate"])
        self.assertEqual(report["matches"], [])
        self.assertIn("embedding", report["method"])
        self.assertIn("rerank", report["method"])
        self.assertEqual(self.llm.text_chat.await_count, 2)
        self.assertEqual(reranker.rerank.await_args.kwargs["top_n"], 8)

    async def test_incomplete_rerank_results_cannot_discard_unreviewed_candidates(self):
        self._prepare_semantic_check()
        self._put(
            "custom", "second", "另一位医生拒绝点灯。", "医生在寻找微弱的荧光标记。"
        )
        self.plugin.config["dedup_rerank_provider"] = "reranker"
        self.providers["reranker"] = SimpleNamespace(
            rerank=AsyncMock(
                return_value=[SimpleNamespace(index=0, relevance_score=0.99)]
            )
        )
        self._set_llm_responses(self.proposed_annotation)

        with self.assertRaises(ValueError):
            await self.catalog.check(*self.proposed)

        self.llm.text_chat.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
