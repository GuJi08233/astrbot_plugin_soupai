# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This is an AstrBot plugin for a "Sea Turtle Soup" (海龟汤) reasoning game. It's a Chinese puzzle game where players ask yes/no questions to deduce a story.

## Architecture

- **Single-file plugin**: All functionality in `main.py` (~2450 lines)
- **AstrBot framework**: Built on AstrBot >= 4.16, < 5
- **Thread-safe storage**: `ThreadSafeStoryStorage` class for managing puzzle usage
- **Game state management**: `GameState` class tracks active games per group
- **LLM integration**: Uses AstrBot's Provider interface for AI functionality
- **Session management**: Uses `session_waiter` for conversation control

## Key Components

- **Network puzzle database**: `network_soupai.json`, 188 puzzles, each with a
  stable `id` (sha1 of the puzzle text, first 12 hex chars)
- **Configuration**: `_conf_schema.json` defines plugin settings
- **Metadata**: `metadata.yaml` for plugin registration

## Development Commands

This is an AstrBot plugin, so development involves:
1. **Testing**: No formal test framework found - manual testing required
2. **Linting**: No specific linting configuration found
3. **Building**: No build process - it's a Python plugin file
4. **Installation**: Copy to `AstrBot/data/plugins/` directory

## Plugin Structure

- **Main class**: `SoupaiPlugin(Star)`, picked up by AstrBot's auto-discovery.
  The `@register` decorator is deprecated — do not reintroduce it; identity
  lives in `metadata.yaml`.
- **Command handlers**: Decorated with `@filter.command`
- **Session handlers**: Use `@session_waiter` for conversation flow
- **LLM integration**: Go through `self._resolve_provider(provider_id, umo)`
  rather than calling `get_using_provider` / `get_provider_by_id` directly —
  it centralises the fallback and passes `umo` so per-session provider
  isolation keeps working.
- **Replies**: Send through `self._send_reply()` (honours the `reply_mode`
  config) or `self._safe_send()` (swallows send failures). Do not call
  `event.send(event.plain_result(...))` directly on paths that end a game.
- **Jev (optional, question verdicts only)**: when `judge_engine` is `jev`,
  `judge_question` tries `self._jev_choice()` first — a TypeSafe System One
  Choice call that can only return one of the keys you pass in. It returns
  `None` on low confidence, an unknown option, or any transport error, and the
  caller falls back to the LLM. Keep `_JUDGE_CRITERIA` in step with the wording
  in the LLM prompt: both paths must classify the same way, or flipping the
  setting changes how the game feels.
- **Stopping propagation**: `event.stop_event()`. There is no `event.block()`.
- **Config schema**: `_conf_schema.json` is rendered by the dashboard's
  `ConfigItemRenderer`. Two provider fields carry `"_special":
  "select_provider"`, which swaps the text box for the same provider dropdown
  the core settings use — it emits the provider `id`, which is exactly the key
  `get_provider_by_id` expects. Enum fields pair `options` with a same-length
  `labels` array so the panel shows Chinese instead of the raw value.
  Jev-only fields carry `"condition": {"judge_engine": "jev"}` and are hidden
  until that engine is chosen; the keys still exist in the saved config, so
  nothing in `main.py` needs to care whether they were visible.

## Important Patterns

- **Thread safety**: Uses `threading.Lock` for shared state
- **Persistence**: JSON files for usage tracking
- **Error handling**: Comprehensive try-catch blocks with logging
- **Configuration**: Managed through AstrBot's plugin configuration system

## Development Workflow

1. Modify `main.py`
2. Reload plugin in AstrBot WebUI
3. Test commands in chat
4. Check logs for errors

## Key Files

- `main.py` - Core plugin implementation
- `network_soupai.json` - Puzzle database
- `_conf_schema.json` - Configuration schema
- `metadata.yaml` - Plugin metadata

## Web UI

`webui.py` registers the routes behind the panel page in `pages/dashboard/`.
AstrBot loads that page in an iframe with no auth of its own, so the page talks
to the backend by posting `astrbot-plugin-page` messages to the parent window.
That bridge implements only `api:get` and `api:post` — do not add PUT/DELETE
routes, they cannot be reached. Handlers return `{"status": "ok", "data": ...}`;
the bridge unwraps `data` before the page sees it.

Three things about that iframe are easy to get wrong, and all three fail
*silently* — the page renders fine and simply never works:

- **Every outgoing message needs `kind: 'request'`.** The panel's
  `handleWindowMessage` only dispatches `kind === 'ready'` and
  `kind === 'request'`; anything else is dropped without a word, and the page
  then sits there until its own timeout fires. This shipped broken once: every
  single request timed out, which also made the puzzle bank look empty.
- **`window.confirm` / `alert` / `prompt` do nothing.** The panel's sandbox is
  `allow-scripts allow-forms allow-downloads`, with no `allow-modals`, so the
  browser ignores the call and `confirm()` returns `false` — every guarded
  action is cancelled and the button looks dead. Use `confirmDialog()`.
- **Theme comes from the panel, not the OS.** It arrives twice: `?theme=` on
  the iframe URL and `isDark` in the `kind: 'context'` message (re-sent on every
  panel theme switch). `app.js` writes it to `<html data-theme>`, which is what
  `style.css` keys off. A bare `prefers-color-scheme` rule goes white when the
  panel is dark but the OS is light.

Posting `kind: 'ready'` on startup makes the panel re-send that context, which
is also how `locale`/`i18n` would arrive if the page ever needs them.

`network_soupai.json` ships with the repo, so the network bank is read-only
from the web: editing it would dirty the working tree and conflict when merging
upstream. Hiding a puzzle writes to a blocklist under the plugin data dir
instead.

The settings tab is generated from `config.schema`, which the `/config` route
sends to the page verbatim — `options`, `labels`, `condition`, `secret` and
`_special: select_provider` are all honoured, so a new `_conf_schema.json`
item shows up on the web without frontend changes. Saving goes through
`config/save` → `config.save_config(replace)` → `plugin._load_config()`;
`save_config` alone does NOT rebuild the plugin instance (the panel path
reloads, this one doesn't), which is exactly why `_load_config` exists. The
`jev_api_key` field is write-only from the web: GET masks it to `""`, an
empty POST value means "unchanged" and is skipped, and clearing it requires
the explicit `clear_jev_api_key` flag.

## Invariants Worth Keeping

- **Clear game state before sending the closing message.** `event.send`
  raises when the platform is offline; if `end_game()` runs after it, the
  round stays "active" forever and `/汤` can never start a new one.
- **Never echo the verification LLM's critique on a wrong guess.** To explain
  the mistake it retells the answer. Use the `_LEVEL_FEEDBACK` table instead.
- **Never route `/验证` through Jev.** It treats state as trusted data, and the
  player's guess is the one input with an incentive to cheat: submitting the
  literal text `完全还原` scored 完全还原 at 0.87 confidence and won the round.
  A Score primitive is fooled the same way. Verdicts are safe — winning a
  bogus 「是」 does not end the game.
- **Usage records key on story id, never on list position.** Positions shift
  when the local bank evicts its oldest entry or the web UI deletes something,
  which silently reassigns "already used" to a different puzzle. Every bank now
  carries real ids, so `story_id`'s fall back to the index should never fire;
  if you ever regenerate `network_soupai.json`, derive the ids from the puzzle
  text the same way so existing records survive. Records written before the
  network bank had ids are detected and dropped by
  `NetworkSoupaiStorage._drop_index_era_records`.
- **Usage is per session (`unified_msg_origin`), not global.** Each group and
  DM works through the bank independently. Games are still keyed by `group_id`,
  so each game carries a `session` field to tie the two together — keep writing
  it in `start_game`, the web UI relies on it.
- **Never put an answer in a list response.** `stories` and `games` return
  puzzles only; `story/answer` is a separate, deliberate request.
- Format with `ruff check --fix && ruff format` before committing.