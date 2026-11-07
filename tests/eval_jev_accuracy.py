"""Offline evaluation of question judging against a hand-labelled gold set.

Loads the real ``main.py`` with AstrBot stubbed out (as the unit tests do), so
the Jev payload, the judging rules and the question routing all come from the
production code instead of a copy that can drift. Decision models are called
through ``SoupaiPlugin._jev_choice`` (``/v1/systemone``); chat models go
through ``judge_question`` in LLM mode with an OpenAI-compatible provider shim,
which reproduces the production prompt and reply parsing.

The gold set is ``tests/eval_jev_gold.jsonl``: one question per line, keyed by
network-bank story id, labelled against the current ``_JUDGE_CRITERIA``. It is
committed because it carries no puzzle answers — only story id, tag, the
question itself and the expected verdict. By default each question carries the
previous three questions of the same story as ``相关问答``, the way production
passes the round's recent questions.
Raw results go to ``tests/eval_jev_results.json``, which is gitignored because
it contains puzzle answers.

This is the regression gate for judging changes. Change ``_JUDGE_CRITERIA``,
``_JUDGE_INSTRUCTIONS``, ``JEV_PROMPT_VERSION`` or ``_is_compound_question``
and re-run it before shipping; bump the prompt version in the same commit, so
records can still be read under the rules they were judged by.

Usage (from the plugin directory, in AstrBot's Python environment)::

    GUJI_API_KEY=sk-... python tests/eval_jev_accuracy.py \\
        --jev jev-latest decision-model-preview \\
        --llm deepseek-v4.1-flash step-5-preview

``--reuse PATH [PATH ...]`` takes model outputs from earlier results files for
every question whose state (puzzle, answer, wording and history) is unchanged,
and only queries what is missing; with everything cached it re-scores against
the current gold labels without any API call. The API key is read from the
environment only and is never written to disk.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import io
import json
import logging
import math
import os
import sys
import time
from contextvars import ContextVar
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

PLUGIN_DIR = Path(__file__).resolve().parent.parent
GOLD_PATH = Path(__file__).resolve().parent / "eval_jev_gold.jsonl"
RESULTS_PATH = Path(__file__).resolve().parent / "eval_jev_results.json"
LABELS = ("是", "否", "不重要", "是也不是")
THRESHOLDS = (0.0, 0.3, 0.5, 0.7, 0.9)
# A dict the chat shim writes the last HTTP status into. judge_question turns
# every provider error into the same reply, so this is how run_llm tells a
# relay rate limit (worth retrying) from a real failure (worth recording).
CHAT_STATUS: ContextVar[dict] = ContextVar("chat_status")


def load_plugin_class():
    """Import main.py with AstrBot stubbed out, mirroring tests/test_judging.py.

    Returns:
        The real ``SoupaiPlugin`` class.
    """

    def decorate(*args, **kwargs):
        return lambda method: method

    logger = logging.getLogger("soupai.eval")
    logger.setLevel(logging.ERROR)
    bindings = {
        "astrbot": {},
        "astrbot.api": {"AstrBotConfig": dict, "logger": logger},
        "astrbot.api.event": {
            "AstrMessageEvent": object,
            "MessageChain": object,
            "MessageEventResult": object,
            "filter": SimpleNamespace(
                command=decorate,
                permission_type=decorate,
                event_message_type=decorate,
                on_astrbot_loaded=decorate,
                PermissionType=SimpleNamespace(ADMIN="admin"),
                EventMessageType=SimpleNamespace(ALL="all"),
            ),
        },
        "astrbot.api.message_components": {"At": object, "Reply": object},
        "astrbot.api.provider": {"LLMResponse": object},
        "astrbot.api.star": {"Context": object, "Star": object, "StarTools": object},
        "astrbot.core": {},
        "astrbot.core.utils": {},
        "astrbot.core.utils.session_waiter": {
            "SessionController": object,
            "SessionFilter": object,
            "session_waiter": decorate,
        },
        "soupai_eval_runtime": {"__path__": [str(PLUGIN_DIR)]},
        "soupai_eval_runtime.webui": {"SoupaiWebApi": object},
    }
    modules = {}
    for name, attributes in bindings.items():
        modules[name] = ModuleType(name)
        modules[name].__dict__.update(attributes)
    spec = importlib.util.spec_from_file_location(
        "soupai_eval_runtime.main", PLUGIN_DIR / "main.py"
    )
    module = importlib.util.module_from_spec(spec)
    modules[spec.name] = module
    with patch.dict(sys.modules, modules):
        spec.loader.exec_module(module)
    return module.SoupaiPlugin


def make_plugin(plugin_cls, config: dict, provider=None):
    """Build a plugin instance without AstrBot, the way the unit tests do.

    Args:
        plugin_cls: The ``SoupaiPlugin`` class.
        config: Plugin configuration read by ``_load_config``.
        provider: Chat provider returned for every provider lookup.

    Returns:
        A plugin instance ready for ``judge_question`` and ``_jev_choice``.
    """
    plugin = plugin_cls.__new__(plugin_cls)
    plugin._jev_client = None
    plugin.config = config
    plugin._load_config()
    plugin.context = SimpleNamespace(
        get_provider_by_id=Mock(return_value=provider),
        get_using_provider=Mock(return_value=provider),
    )
    return plugin


class ChatProvider:
    """The one method of an AstrBot provider that ``judge_question`` uses."""

    def __init__(self, client: httpx.AsyncClient, model: str):
        self.client = client
        self.model = model

    async def text_chat(self, prompt, contexts, func_tool, image_urls, system_prompt):
        resp = await self.client.post(
            "/v1/chat/completions",
            json={
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": prompt},
                ],
            },
        )
        status = CHAT_STATUS.get(None)
        if status is not None:
            status["code"] = resp.status_code
        resp.raise_for_status()
        message = resp.json()["choices"][0]["message"]
        return SimpleNamespace(completion_text=message.get("content") or "")


def load_items(history_mode: str) -> list[dict]:
    """Read the gold set and attach puzzle, answer, history and Jev state.

    Args:
        history_mode: ``seq`` gives each question the previous three questions
            of its story as history; ``none`` sends no history. A line with an
            explicit ``history`` always uses it.

    Returns:
        One dict per gold question.

    Raises:
        ValueError: A gold line is malformed or names an unknown story.
    """
    bank = json.loads((PLUGIN_DIR / "network_soupai.json").read_text(encoding="utf-8"))
    stories = {story["id"]: story for story in bank}
    items, asked = [], {}
    for number, line in enumerate(
        GOLD_PATH.read_text(encoding="utf-8").splitlines(), 1
    ):
        if not line.strip():
            continue
        row = json.loads(line)
        story = stories.get(row.get("story_id"))
        if story is None or row.get("gold") not in LABELS or not row.get("question"):
            raise ValueError(f"gold line {number} is malformed: {line}")
        previous = asked.setdefault(row["story_id"], [])
        if row.get("history") is not None:
            history = row["history"]
        else:
            history = previous[-3:] if history_mode == "seq" else []
        # Same shape judge_question builds; route() asserts the two still match.
        state = {"谜面": story["puzzle"], "真相": story["answer"]}
        state["玩家提问"] = row["question"].strip()
        if history:
            state["相关问答"] = history[-3:]
        items.append(
            {
                "line": number,
                "tag": row.get("tag", ""),
                "story_id": row["story_id"],
                "question": row["question"],
                "gold": row["gold"],
                "puzzle": story["puzzle"],
                "answer": story["answer"],
                "history": history,
                "state": state,
            }
        )
        previous.append({"question": row["question"], "answer": row["gold"]})
    return items


async def route(plugin_cls, items: list[dict]) -> None:
    """Record, offline, which engine production would pick for each question.

    Sets ``item["route"]`` to ``jev``, ``compound_question`` or
    ``context_required`` (sent straight to the judge LLM), or ``clarify``
    (answered with a clarification request and no model call).
    """
    llm = SimpleNamespace(
        text_chat=AsyncMock(return_value=SimpleNamespace(completion_text="否"))
    )
    plugin = make_plugin(
        plugin_cls,
        {"judge_engine": "jev", "jev_api_key": "offline", "judge_llm_provider": "x"},
        llm,
    )
    plugin._jev_choice = AsyncMock(
        return_value={
            "choice": "是",
            "confidence": 1.0,
            "probabilities": {},
            "threshold": 0.5,
            "reason": None,
            "extra": {},
        }
    )
    for item in items:
        plugin._jev_choice.reset_mock()
        llm.text_chat.reset_mock()
        _, judged_by = await plugin.judge_question(
            item["question"],
            item["answer"],
            puzzle=item["puzzle"],
            qa_history=item["history"],
        )
        if plugin._jev_choice.await_args is not None:
            if plugin._jev_choice.await_args.kwargs["state"] != item["state"]:
                raise RuntimeError("main.py now builds the Jev state differently")
        if judged_by.get("engine") == "jev":
            item["route"] = "jev"
        elif llm.text_chat.await_args is not None:
            item["route"] = judged_by.get("routing_reason") or "llm"
        else:
            item["route"] = "clarify"


async def run_jev(plugin_cls, models, items, base_url, key, repeat) -> None:
    """Judge every question with each decision model, one request at a time.

    Models alternate which goes first on each question so network drift is
    shared evenly. Requests are sequential so latency is not queueing time.
    Transient request failures are retried twice; ``attempts`` records it.
    """
    captured: dict = {}

    async def capture(response: httpx.Response) -> None:
        await response.aread()
        captured["status"] = response.status_code
        captured["upstream_ms"] = response.headers.get("x-envoy-upstream-service-time")
        try:
            body = response.json()
        except ValueError:
            body = {}
        if isinstance(body, dict):
            captured["model"] = body.get("model")
            captured["server_ms"] = body.get("latency_ms")
            captured["usage"] = body.get("usage")

    plugins = {}
    for model in models:
        plugin = make_plugin(
            plugin_cls,
            {
                "judge_engine": "jev",
                "judge_fallback_to_llm": True,
                "jev_api_key": key,
                "jev_base_url": base_url,
                "jev_model": model,
                "jev_judge_min_confidence": 0.5,
            },
        )
        plugin._jev_client = httpx.AsyncClient(
            timeout=httpx.Timeout(15.0),
            follow_redirects=False,
            event_hooks={"response": [capture]},
        )
        plugins[model] = plugin
        # Warm the connection so the first question is not charged the handshake.
        await plugin._jev_choice(items[0]["state"], "warm up", {"a": "a", "b": "b"})

    total = len(items) * len(models) * repeat
    done, started = 0, time.monotonic()
    for run in range(repeat):
        for index, item in enumerate(items):
            order = models if (index + run) % 2 == 0 else models[::-1]
            for model in order:
                for attempt in range(1, 4):
                    captured.clear()
                    begin = time.perf_counter()
                    result = await plugins[model]._jev_choice(
                        item["state"],
                        plugin_cls._JUDGE_INSTRUCTIONS,
                        plugin_cls._JUDGE_CRITERIA,
                    )
                    elapsed = time.perf_counter() - begin
                    if result["reason"] != "request_failed" or attempt == 3:
                        break
                    await asyncio.sleep(2 * attempt)
                result.pop("extra", None)
                runs = item.setdefault("jev", {}).setdefault(model, [])
                runs.append(
                    {
                        **result,
                        "latency": round(elapsed, 3),
                        "attempts": attempt,
                        **captured,
                    }
                )
                done += 1
                if done % 50 == 0:
                    rate = done / (time.monotonic() - started)
                    print(f"  jev {done}/{total} ({rate:.1f} req/s)", flush=True)
    for plugin in plugins.values():
        await plugin._jev_client.aclose()


async def run_llm(plugin_cls, models, items, base_url, key, concurrency) -> None:
    """Judge every question with each chat model through judge_question.

    A relay rate limit (HTTP 429) is not a verdict, so that question is asked
    again after a backoff, up to five times; ``attempts`` records it. Any
    other failure is kept as the model's result.
    """
    async with httpx.AsyncClient(
        base_url=base_url,
        headers={"Authorization": f"Bearer {key}"},
        timeout=httpx.Timeout(90.0),
    ) as client:
        for model in models:
            plugin = make_plugin(
                plugin_cls,
                {"judge_engine": "llm", "judge_llm_provider": "judge"},
                ChatProvider(client, model),
            )
            semaphore = asyncio.Semaphore(concurrency)
            progress = {"done": 0}

            async def judge(item: dict) -> None:
                async with semaphore:
                    for attempt in range(1, 6):
                        status = {}
                        CHAT_STATUS.set(status)
                        begin = time.perf_counter()
                        reply, judged_by = await plugin.judge_question(
                            item["question"],
                            item["answer"],
                            puzzle=item["puzzle"],
                            qa_history=item["history"],
                        )
                        elapsed = time.perf_counter() - begin
                        if status.get("code") != 429 or attempt == 5:
                            break
                        await asyncio.sleep(5 * attempt)
                    item.setdefault("llm", {})[model] = {
                        "choice": reply if judged_by.get("engine") == "llm" else None,
                        "reply": reply,
                        "latency": round(elapsed, 3),
                        "attempts": attempt,
                        "status": status.get("code"),
                    }
                    progress["done"] += 1
                    if progress["done"] % 50 == 0:
                        print(f"  {model} {progress['done']}/{len(items)}", flush=True)

            await asyncio.gather(*(judge(item) for item in items))


def quantile(values: list[float], q: float) -> float:
    """Nearest-rank quantile of an unsorted list; NaN when empty."""
    if not values:
        return math.nan
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, math.ceil(q * len(ordered)) - 1))]


def score(rows: list[dict], pick) -> dict:
    """Accuracy, coverage and per-label precision/recall for one engine.

    Args:
        rows: Gold items.
        pick: Returns the engine's verdict for an item, or None.

    Returns:
        ``n``, ``answered``, ``correct``, ``acc`` (no verdict counts as wrong),
        ``macro_f1`` and per-label ``p``/``r``/``f1``.
    """
    answered = [row for row in rows if pick(row) in LABELS]
    correct = sum(pick(row) == row["gold"] for row in answered)
    per_label, f1s = {}, []
    for label in LABELS:
        gold_n = sum(row["gold"] == label for row in rows)
        pred_n = sum(pick(row) == label for row in rows)
        hit = sum(pick(row) == label == row["gold"] for row in rows)
        precision = hit / pred_n if pred_n else 0.0
        recall = hit / gold_n if gold_n else 0.0
        f1 = 2 * precision * recall / (precision + recall) if hit else 0.0
        per_label[label] = {"n": gold_n, "p": precision, "r": recall, "f1": f1}
        if gold_n:
            f1s.append(f1)
    return {
        "n": len(rows),
        "answered": len(answered),
        "correct": correct,
        "acc": correct / len(rows) if rows else math.nan,
        "macro_f1": sum(f1s) / len(f1s) if f1s else math.nan,
        "labels": per_label,
    }


def mcnemar(rows: list[dict], pick_a, pick_b) -> tuple[int, int, int, int, float]:
    """Paired comparison of two engines on the same questions.

    Returns:
        Both right, only A right, only B right, both wrong, and the exact
        two-sided McNemar p-value over the discordant pairs.
    """
    both = only_a = only_b = neither = 0
    for row in rows:
        a, b = pick_a(row) == row["gold"], pick_b(row) == row["gold"]
        both += a and b
        only_a += a and not b
        only_b += b and not a
        neither += not a and not b
    k, n = min(only_a, only_b), only_a + only_b
    p = min(1.0, 2 * sum(math.comb(n, i) for i in range(k + 1)) / 2**n) if n else 1.0
    return both, only_a, only_b, neither, p


def report(items: list[dict], jev_models: list[str], llm_models: list[str]) -> None:
    """Print the comparison tables."""

    def jev_first(model):
        return lambda row: row["jev"][model][0]

    def jev_pick(model):
        return lambda row: row["jev"][model][0]["choice"]

    def llm_pick(model):
        return lambda row: row["llm"][model]["choice"]

    def adopted(result, threshold):
        return (
            result["choice"] in LABELS
            and result["confidence"] is not None
            and result["confidence"] >= threshold
            and result["reason"] in (None, "low_confidence")
        )

    routed = [row for row in items if row["route"] == "jev"]
    gold = {label: sum(row["gold"] == label for row in items) for label in LABELS}
    routes = {}
    for row in items:
        routes[row["route"]] = routes.get(row["route"], 0) + 1
    print(f"\n题目 {len(items)} 道，来自 {len({r['story_id'] for r in items})} 个汤")
    print(f"金标准分布 {gold}")
    print(f"线上分流 {routes}（jev = 生产环境里会交给决策模型的题）")

    engines = [(m, jev_pick(m)) for m in jev_models]
    engines += [(m, llm_pick(m)) for m in llm_models]
    for title, rows in (("全部题目", items), ("线上会交给决策模型的题", routed)):
        print(f"\n== 准确率：{title}（n={len(rows)}）==")
        print(
            f"{'引擎':26s} {'准确率':>7s} {'macroF1':>8s} {'有判定':>6s}  "
            + "  ".join(f"{label}召回" for label in LABELS)
        )
        for name, pick in engines:
            s = score(rows, pick)
            recalls = "  ".join(
                f"{s['labels'][label]['r']:>{len(label) + 2}.2f}" for label in LABELS
            )
            print(
                f"{name:26s} {s['acc']:7.3f} {s['macro_f1']:8.3f} "
                f"{s['answered']:>4d}/{s['n']:<3d} {recalls}"
            )

    for model in jev_models:
        first = jev_first(model)
        fails = [row for row in items if first(row)["reason"] == "request_failed"]
        retried = sum(first(row)["attempts"] > 1 for row in items)
        versions = sorted({str(first(row).get("model")) for row in items})
        print(f"\n== {model}（响应 model 字段：{', '.join(versions)}）==")
        print(f"请求失败 {len(fails)}，重试后成功 {retried - len(fails)}")
        reasons = {}
        for row in routed:
            reason = first(row)["reason"] or "adopted"
            reasons[reason] = reasons.get(reason, 0) + 1
        print(f"线上门槛 0.5 下的去向（仅决策模型题）：{reasons}")
        print("  门槛  采纳率  采纳部分准确率")
        for threshold in THRESHOLDS:
            taken = [row for row in routed if adopted(first(row), threshold)]
            right = sum(first(row)["choice"] == row["gold"] for row in taken)
            share = len(taken) / len(routed) if routed else math.nan
            acc = right / len(taken) if taken else math.nan
            print(f"  {threshold:.1f}   {share:6.1%}  {acc:.3f}")
        print("  置信度分桶（全部题目）：")
        edges = (0.0, 0.3, 0.5, 0.7, 0.9, 1.01)
        for low, high in zip(edges, edges[1:]):
            chosen = [
                row
                for row in items
                if first(row)["confidence"] is not None
                and low <= first(row)["confidence"] < high
            ]
            if chosen:
                right = sum(first(row)["choice"] == row["gold"] for row in chosen)
                print(
                    f"    [{low:.1f},{min(high, 1.0):.1f}) n={len(chosen):3d} "
                    f"acc={right / len(chosen):.3f}"
                )
        ok = [first(row) for row in items if first(row)["reason"] != "request_failed"]
        total = [r["latency"] * 1000 for r in ok]
        upstream = [float(r["upstream_ms"]) for r in ok if r.get("upstream_ms")]
        mean = sum(total) / len(total) if total else math.nan
        peak = max(total) if total else math.nan
        print(
            f"  端到端延迟 ms：均值 {mean:.0f}  p50 {quantile(total, 0.5):.0f}  "
            f"p90 {quantile(total, 0.9):.0f}  p99 {quantile(total, 0.99):.0f}  "
            f"最大 {peak:.0f}"
        )
        if upstream:
            print(
                "  上游处理 ms（x-envoy-upstream-service-time）："
                f"p50 {quantile(upstream, 0.5):.0f}  p90 {quantile(upstream, 0.9):.0f}  "
                f"p99 {quantile(upstream, 0.99):.0f}"
            )
        runs = [row["jev"][model] for row in items if len(row["jev"][model]) > 1]
        if runs:
            flips = sum(len({r["choice"] for r in rs}) > 1 for rs in runs)
            print(f"  重复请求：{flips}/{len(runs)} 道题的判定前后不一致")

    for i, model_a in enumerate(jev_models):
        for model_b in jev_models[i + 1 :]:
            for title, rows in (("全部题目", items), ("决策模型题", routed)):
                both, only_a, only_b, neither, p = mcnemar(
                    rows, jev_pick(model_a), jev_pick(model_b)
                )
                print(
                    f"\n== 配对比较（{title}）{model_a} vs {model_b} ==\n"
                    f"都对 {both}  仅前者对 {only_a}  仅后者对 {only_b}  都错 {neither}  "
                    f"McNemar p={p:.4f}"
                )

    tags = sorted({row["tag"] for row in items})
    print("\n== 按题型准确率（全部题目）==")
    print(
        f"{'题型':12s} {'n':>4s}  "
        + "  ".join(f"{name[:16]:>16s}" for name, _ in engines)
    )
    for tag in tags:
        rows = [row for row in items if row["tag"] == tag]
        cells = "  ".join(f"{score(rows, pick)['acc']:16.3f}" for _, pick in engines)
        print(f"{tag:12s} {len(rows):4d}  {cells}")

    for model in llm_models:
        latency = [row["llm"][model]["latency"] * 1000 for row in items]
        missing = [row for row in items if row["llm"][model]["choice"] is None]
        print(
            f"\n== {model} ==\n无判定 {len(missing)}（需要澄清/失败/格式无效），"
            f"延迟 ms p50 {quantile(latency, 0.5):.0f} p90 {quantile(latency, 0.9):.0f}"
        )

    if jev_models and llm_models:
        print("\n== 线上组合准确率（决策模型题且置信度≥0.5 用决策模型，其余用 LLM）==")
        for model in jev_models:
            for llm in llm_models:

                def blended(row, model=model, llm=llm):
                    result = row["jev"][model][0]
                    if row["route"] == "jev" and result["reason"] is None:
                        return result["choice"]
                    return row["llm"][llm]["choice"]

                s = score(items, blended)
                print(
                    f"  {model} + {llm}: {s['acc']:.3f}（有判定 {s['answered']}/{s['n']}）"
                )


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--jev", nargs="*", default=["jev-latest", "decision-model-preview"]
    )
    parser.add_argument(
        "--llm", nargs="*", default=["deepseek-v4.1-flash", "step-5-preview"]
    )
    parser.add_argument("--history", choices=("seq", "none"), default="seq")
    parser.add_argument(
        "--concurrency", type=int, default=3, help="parallel calls per chat model"
    )
    parser.add_argument(
        "--repeat", type=int, default=1, help="Jev passes, for consistency"
    )
    parser.add_argument(
        "--limit", type=int, default=0, help="only the first N questions"
    )
    parser.add_argument(
        "--reuse", type=Path, nargs="+", default=[], help="earlier results files"
    )
    parser.add_argument("--out", type=Path, default=RESULTS_PATH)
    args = parser.parse_args()

    plugin_cls = load_plugin_class()
    items = load_items(args.history)
    if args.limit:
        items = items[: args.limit]
    await route(plugin_cls, items)

    for path in args.reuse:
        cached = {
            (row["story_id"], row["question"]): row
            for row in json.loads(path.read_text(encoding="utf-8"))
        }
        for item in items:
            old = cached.get((item["story_id"], item["question"]))
            # An output only answers the exact state it was asked about; a gold
            # edit changes the history of the questions that follow it.
            if old is None or old.get("state") != item["state"]:
                continue
            for kind, models in (("jev", args.jev), ("llm", args.llm)):
                for model in models:
                    output = old.get(kind, {}).get(model)
                    # A rate limit or dropped request is not an answer; ask again.
                    if not output or (
                        output[0]["reason"] == "request_failed"
                        if kind == "jev"
                        else output.get("status") == 429
                    ):
                        continue
                    item.setdefault(kind, {})[model] = output
    jev_todo = {m: [i for i in items if m not in i.get("jev", {})] for m in args.jev}
    llm_todo = {m: [i for i in items if m not in i.get("llm", {})] for m in args.llm}
    pending = sum(map(len, jev_todo.values())) + sum(map(len, llm_todo.values()))
    print(
        f"{len(items)} questions, history={args.history}, {pending} model calls to make"
    )

    key = os.environ.get("GUJI_API_KEY", "").strip()
    base_url = os.environ.get("GUJI_BASE_URL", "https://api.guji.uno").rstrip("/")
    if pending and not key:
        raise SystemExit("set GUJI_API_KEY (and optionally GUJI_BASE_URL)")

    # Models that lack the same full set share one alternating pass, which keeps
    # their latency comparable; partial gaps are filled model by model.
    together = [m for m in args.jev if len(jev_todo[m]) == len(items)]
    if together:
        print(f"== decision models {together} ==", flush=True)
        await run_jev(plugin_cls, together, items, base_url, key, args.repeat)
    for model in args.jev:
        if model not in together and jev_todo[model]:
            print(f"== {model}: {len(jev_todo[model])} questions ==", flush=True)
            await run_jev(
                plugin_cls, [model], jev_todo[model], base_url, key, args.repeat
            )
    for model in args.llm:
        if llm_todo[model]:
            print(f"== {model}: {len(llm_todo[model])} questions ==", flush=True)
            await run_llm(
                plugin_cls, [model], llm_todo[model], base_url, key, args.concurrency
            )

    args.out.write_text(
        json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    print(f"raw results -> {args.out}")
    report(items, args.jev, args.llm)


if __name__ == "__main__":
    asyncio.run(main())
