"""Hermes Rescue — keeps your agent from breaking Hermes, and says what really went wrong.

* Guard (``pre_tool_call``): when the agent tries to delete or rewrite Hermes's own data
  (saved conversations, memories, skills, settings, keys, profiles), Rescue blocks it and
  points to the supported command, or asks the human first and saves a snapshot.
  NousResearch/hermes-agent#144562.
* Real cause (``api_request_error`` → ``transform_llm_output``): when a reply fails or quietly
  comes from a backup, Rescue adds one short block saying what each attempt actually hit and
  the one fix. NousResearch/hermes-agent#147620.
* ``/rescue``: health, log, undo and the guard switch, in every surface Hermes runs on.

Local only: no network calls, no model calls, no credentials read.
"""

from __future__ import annotations

from typing import Any

from .commands import RescueCommand
from .runtime import Rescue


def register(ctx: Any) -> None:
    rescue = Rescue(ctx)
    ctx.register_hook("pre_tool_call", rescue.on_pre_tool_call)
    ctx.register_hook("post_tool_call", rescue.on_post_tool_call)
    ctx.register_hook("api_request_error", rescue.on_api_request_error)
    ctx.register_hook("post_api_request", rescue.on_post_api_request)
    ctx.register_hook("transform_llm_output", rescue.on_transform_llm_output)
    ctx.register_hook("on_session_end", rescue.on_session_end)
    ctx.register_command(
        "rescue", RescueCommand(rescue, ask=getattr(ctx, "ask_choice", None)),
        description="Rescue: health, what went wrong, undo, guard switch",
        args_hint="[log|undo|snapshots|allow|lock|help]",
    )
