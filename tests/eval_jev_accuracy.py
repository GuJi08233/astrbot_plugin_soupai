"""Offline evaluation: Jev vs LLM judging accuracy on the shipped puzzle bank.

Reads AstrBot's cmd_config.json for the api.guji.uno relay key, then for a
sample of (puzzle, question, gold verdict) triples calls:

- Jev  /v1/systemone  (same payload shape as main.py `_jev_choice`)
- LLM  /v1/chat/completions with step-3.7-flash (same prompt as `judge_question`)

Reports accuracy, per-confidence-bin accuracy, threshold tradeoffs, and
probability-distribution stats. Writes results to tests/eval_results.json.

Usage:  python tests/eval_jev_accuracy.py [sample_size] [concurrency]
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import random
import re
import sys
import time

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
from pathlib import Path

import httpx

PLUGIN_DIR = Path(__file__).resolve().parent.parent
ASTRBOT_ROOT = PLUGIN_DIR.parent.parent.parent
CONFIG_PATH = ASTRBOT_ROOT / "data" / "cmd_config.json"
RESULTS_PATH = Path(__file__).resolve().parent / "eval_results.json"

JUDGE_CRITERIA = {
    "是": "玩家命中关键事实或行为，且该信息能直接帮助接近真相。缺少部分细节可以忽略，只要不影响推理方向。",
    "否": "与真相完全不符，或包含明显错误，会使玩家推理走向错误方向。",
    "不重要": "与故事真相无关，或该信息无法推动推理进展。",
    "是也不是": "命中部分事实，但因果关系不完整、或含有可能让玩家推理错误的成分。",
}
JEV_INSTRUCTIONS = "海龟汤推理游戏。请判断`玩家提问`的说法，相对于`真相`应当如何回答。"
VALID = set(JUDGE_CRITERIA)

QUESTION_KINDS = ["fact_yes", "fact_no", "irrelevant", "partial", "vague", "wrong_dir"]
# how many questions of each kind per puzzle (before dedup)
PER_KIND = 2


def load_relay() -> tuple[str, str]:
    """Return (base_url, api_key) for the relay.

    Prefers GUJI_BASE_URL/GUJI_API_KEY env vars; otherwise looks for an
    api.guji.uno entry in AstrBot's cmd_config.json.
    """
    env_url = os.environ.get("GUJI_BASE_URL")
    env_key = os.environ.get("GUJI_API_KEY")
    if env_url and env_key:
        return env_url.rstrip("/"), env_key
    if CONFIG_PATH.exists():
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
        for p in cfg.get("provider", []):
            base = str(p.get("api_base", ""))
            if "guji.uno" in base:
                keys = p.get("key") or []
                if keys:
                    return base.rstrip("/"), keys[0]
    raise SystemExit(
        "no relay credentials: set GUJI_BASE_URL/GUJI_API_KEY or configure "
        "an api.guji.uno provider in cmd_config.json"
    )


def llm_prompt(question: str, true_answer: str) -> str:
    """Same prompt as main.py judge_question."""
    return (
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


SYSTEM_PROMPT = '你是一个海龟汤推理游戏的助手。你必须严格按照游戏规则回答，只能回答"是"、"否"、"不重要"或"是也不是"，不能添加任何其他内容。'

GEN_PROMPT = """你在为海龟汤评测集出题。给定一个海龟汤的汤面与汤底，生成玩家提问样本，并给出每个提问的标准判定（金标准）。

判定定义：
- "是"：命中关键事实或行为，能直接帮助接近真相；缺少量细节不影响方向也算。
- "否"：与真相完全不符或明显错误，会把推理带偏。
- "不重要"：与真相无关，或无法推动推理进展。
- "是也不是"：命中部分事实但因果不完整，或含有可能误导的成分。

对每道题生成以下六类提问，每类 {per_kind} 条，尽量口语化、像真实玩家会问的：
1. fact_yes —— 应判"是"的事实性提问
2. fact_no —— 应判"否"的提问（与真相明显矛盾）
3. irrelevant —— 应判"不重要"的提问（天气、穿着等与真相无关的细节）
4. partial —— 应判"是也不是"的提问（说对一半但因果不完整或有误导成分）
5. vague —— 边界模糊、可此可彼的提问，给出你认为最合理的判定
6. wrong_dir —— 看似合理但方向错误、应判"否"的诱导性提问

严格输出 JSON 数组，每个元素形如：
{{"kind": "fact_yes", "question": "...", "gold": "是"}}
不要输出任何其他内容。

汤面：{puzzle}
汤底：{answer}"""


async def chat(
    client: httpx.AsyncClient,
    model: str,
    prompt: str,
    system: str | None = None,
    temperature: float = 0.0,
    max_tokens: int = 4096,
) -> str:
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    resp = await client.post(
        "/v1/chat/completions",
        json={
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        },
        timeout=120.0,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


def parse_generated(text: str) -> list[dict]:
    m = re.search(r"\[.*\]", text, re.S)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return []
    out = []
    for item in data:
        if (
            isinstance(item, dict)
            and item.get("kind") in QUESTION_KINDS
            and isinstance(item.get("question"), str)
            and item.get("gold") in VALID
        ):
            out.append(
                {
                    "kind": item["kind"],
                    "question": item["question"].strip(),
                    "gold": item["gold"],
                }
            )
    return out


async def gen_questions(client, model, puzzle, answer, sem):
    async with sem:
        try:
            text = await chat(
                client,
                model,
                GEN_PROMPT.format(per_kind=PER_KIND, puzzle=puzzle, answer=answer),
                temperature=0.7,
            )
            return parse_generated(text)
        except Exception as e:
            print(f"  [gen] failed: {e}")
            return []


async def jev_judge(client, model, puzzle, answer, question, sem):
    payload = {
        "model": model,
        "state": {"谜面": puzzle, "真相": answer, "玩家提问": question},
        "questions": {
            "verdict": {
                "type": "choice",
                "instructions": JEV_INSTRUCTIONS,
                "criteria": JUDGE_CRITERIA,
            }
        },
    }
    async with sem:
        t0 = time.monotonic()
        try:
            resp = await client.post("/v1/systemone", json=payload, timeout=30.0)
            resp.raise_for_status()
            ans = resp.json()["answers"]["verdict"]
            probs = ans.get("probabilities") or {}
            return {
                "choice": ans.get("choice"),
                "confidence": ans.get("confidence"),
                "probabilities": probs,
                "latency": round(time.monotonic() - t0, 3),
                "error": None,
            }
        except Exception as e:
            return {
                "choice": None,
                "confidence": None,
                "probabilities": {},
                "latency": round(time.monotonic() - t0, 3),
                "error": str(e),
            }


async def llm_judge(client, model, answer, question, sem):
    async with sem:
        t0 = time.monotonic()
        try:
            text = await chat(
                client,
                model,
                llm_prompt(question, answer),
                system=SYSTEM_PROMPT,
                max_tokens=2048,
            )
            reply = text.strip()
            for v in VALID:  # tolerate e.g. "是。" — same fallback spirit as plugin
                if reply.startswith(v):
                    reply = v
                    break
            return {
                "choice": reply if reply in VALID else None,
                "raw": text.strip()[:80],
                "latency": round(time.monotonic() - t0, 3),
                "error": None if reply in VALID else "invalid_reply",
            }
        except Exception as e:
            return {
                "choice": None,
                "raw": "",
                "latency": round(time.monotonic() - t0, 3),
                "error": str(e),
            }


def accuracy(rows, key):
    valid = [r for r in rows if r[key]["choice"] in VALID]
    if not valid:
        return 0, 0.0
    correct = sum(1 for r in valid if r[key]["choice"] == r["gold"])
    return len(valid), correct / len(valid)


def bins(rows, key):
    """Accuracy per confidence bin; also what threshold adoption would do."""
    edges = [0.0, 0.3, 0.5, 0.7, 0.8, 0.9, 1.01]
    out = []
    for lo, hi in zip(edges, edges[1:]):
        sel = [
            r
            for r in rows
            if r[key]["confidence"] is not None and lo <= r[key]["confidence"] < hi
        ]
        if not sel:
            continue
        correct = sum(1 for r in sel if r[key]["choice"] == r["gold"])
        out.append(
            {
                "range": f"[{lo:.1f},{hi if hi <= 1 else 1.0:.1f})",
                "n": len(sel),
                "acc": round(correct / len(sel), 3),
            }
        )
    return out


async def main():
    sample_size = int(sys.argv[1]) if len(sys.argv) > 1 else 15
    conc = int(sys.argv[2]) if len(sys.argv) > 2 else 5

    base_url, key = load_relay()
    print(f"relay: {base_url}")

    stories = json.loads(
        (PLUGIN_DIR / "network_soupai.json").read_text(encoding="utf-8")
    )
    random.seed(42)
    sample = random.sample(stories, min(sample_size, len(stories)))

    client = httpx.AsyncClient(
        base_url=base_url,
        headers={"Authorization": f"Bearer {key}"},
        timeout=httpx.Timeout(120.0),
    )

    # sanity: available models
    try:
        models = (await client.get("/v1/models", timeout=30.0)).json()
        ids = [m["id"] for m in models.get("data", [])]
        print(
            f"models on relay ({len(ids)}): "
            f"{[i for i in ids if 'jev' in i or 'step-3' in i][:10]}"
        )
    except Exception as e:
        print(f"model list failed (continuing anyway): {e}")

    sem = asyncio.Semaphore(conc)

    # 1) generate questions per puzzle
    print(f"\n== generating questions for {len(sample)} puzzles ==")
    gen_tasks = [
        gen_questions(client, "step-3.7-flash", s["puzzle"], s["answer"], sem)
        for s in sample
    ]
    gen_results = await asyncio.gather(*gen_tasks)

    rows = []
    for story, qs in zip(sample, gen_results):
        for q in qs:
            rows.append(
                {
                    "story_id": story.get("id"),
                    "puzzle": story["puzzle"],
                    "answer": story["answer"],
                    **q,
                }
            )
    # dedup identical questions within a story
    seen, deduped = set(), []
    for r in rows:
        k = (r["story_id"], r["question"])
        if k not in seen:
            seen.add(k)
            deduped.append(r)
    rows = deduped
    print(
        f"generated {len(rows)} questions "
        f"(gold dist: {{v: sum(1 for r in rows if r['gold'] == v) for v in VALID}})"
    )
    by_kind = {}
    for r in rows:
        by_kind[r["kind"]] = by_kind.get(r["kind"], 0) + 1
    print(f"by kind: {by_kind}")

    # 2) judge with Jev and LLM
    print(f"\n== judging {len(rows)} questions with jev-latest + step-3.7-flash ==")
    jev_tasks = [
        jev_judge(client, "jev-latest", r["puzzle"], r["answer"], r["question"], sem)
        for r in rows
    ]
    llm_tasks = [
        llm_judge(client, "step-3.7-flash", r["answer"], r["question"], sem)
        for r in rows
    ]
    jev_results, llm_results = await asyncio.gather(
        asyncio.gather(*jev_tasks), asyncio.gather(*llm_tasks)
    )
    for r, j, llm in zip(rows, jev_results, llm_results):
        r["jev"], r["llm"] = j, llm

    RESULTS_PATH.write_text(
        json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"raw results -> {RESULTS_PATH}")

    # 3) report
    print("\n== accuracy ==")
    for key in ("jev", "llm"):
        n, acc = accuracy(rows, key)
        errs = sum(1 for r in rows if r[key]["error"])
        lat = sorted(r[key]["latency"] for r in rows)
        med = lat[len(lat) // 2] if lat else 0
        print(f"{key}: n={n} acc={acc:.3f} errors={errs} median_latency={med}s")

    print("\n== per-kind accuracy ==")
    for kind in QUESTION_KINDS:
        sub = [r for r in rows if r["kind"] == kind]
        jn, jacc = accuracy(sub, "jev")
        ln, lacc = accuracy(sub, "llm")
        print(f"{kind:10s} n={len(sub):3d}  jev={jacc:.3f}  llm={lacc:.3f}")

    print("\n== jev confidence bins ==")
    for b in bins(rows, "jev"):
        print(f"  {b['range']}: n={b['n']} acc={b['acc']}")

    print("\n== probability distribution sanity ==")
    sums = [
        sum(
            p for p in r["jev"]["probabilities"].values() if isinstance(p, (int, float))
        )
        for r in rows
        if r["jev"]["probabilities"]
    ]
    if sums:
        sums.sort()
        print(
            f"  prob sum: min={sums[0]:.3f} median={sums[len(sums) // 2]:.3f} max={sums[-1]:.3f}"
        )
    agrees = [
        r
        for r in rows
        if r["jev"]["probabilities"]
        and max(r["jev"]["probabilities"], key=r["jev"]["probabilities"].get)
        == r["jev"]["choice"]
    ]
    n_probs = sum(1 for r in rows if r["jev"]["probabilities"])
    print(f"  argmax(probabilities)==choice: {len(agrees)}/{n_probs}")
    spreads = [
        max(r["jev"]["probabilities"].values())
        - sorted(r["jev"]["probabilities"].values())[-2]
        for r in rows
        if len(r["jev"]["probabilities"]) >= 2
    ]
    if spreads:
        spreads.sort()
        print(f"  top1-top2 spread: median={spreads[len(spreads) // 2]:.3f}")

    print(
        "\n== threshold tradeoff (adopt jev if confidence>=t else fall back to llm) =="
    )
    print("  t      adopt%  jev_adopted_acc  blended_acc(jev+llm fallback)")
    llm_map = {id(r): r["llm"]["choice"] for r in rows}
    for t in (0.0, 0.3, 0.5, 0.7, 0.9):
        adopted, adopted_correct, blended, blended_n = 0, 0, 0, 0
        for r in rows:
            conf = r["jev"]["confidence"]
            use_jev = conf is not None and conf >= t and r["jev"]["choice"] in VALID
            final = r["jev"]["choice"] if use_jev else llm_map[id(r)]
            if final not in VALID:
                continue
            blended_n += 1
            blended += final == r["gold"]
            if use_jev:
                adopted += 1
                adopted_correct += r["jev"]["choice"] == r["gold"]
        if blended_n:
            print(
                f"  {t:.1f}    {adopted / blended_n:6.1%}  "
                f"{(adopted_correct / adopted) if adopted else float('nan'):.3f}          "
                f"{blended / blended_n:.3f}"
            )

    print("\n== disagreements (sample) ==")
    shown = 0
    for r in rows:
        j, llm = r["jev"]["choice"], r["llm"]["choice"]
        if j in VALID and llm in VALID and j != llm and shown < 8:
            flag_j = "✓" if j == r["gold"] else "✗"
            flag_l = "✓" if llm == r["gold"] else "✗"
            print(
                f"  [{r['kind']}] gold={r['gold']} jev={j}{flag_j} llm={llm}{flag_l}  {r['question'][:40]}"
            )
            shown += 1

    await client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
