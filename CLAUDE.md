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

- **Network puzzle database**: `network_soupai.json` with ~300 pre-scraped puzzles
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
- **Stopping propagation**: `event.stop_event()`. There is no `event.block()`.

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

## Invariants Worth Keeping

- **Clear game state before sending the closing message.** `event.send`
  raises when the platform is offline; if `end_game()` runs after it, the
  round stays "active" forever and `/汤` can never start a new one.
- **Never echo the verification LLM's critique on a wrong guess.** To explain
  the mistake it retells the answer. Use the `_LEVEL_FEEDBACK` table instead.
- Format with `ruff check --fix && ruff format` before committing.