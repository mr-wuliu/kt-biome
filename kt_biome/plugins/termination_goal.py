"""Termination-by-scratchpad-flag plugin.

Demonstrates ``BasePlugin.contribute_termination_check`` (cluster C.2)
by voting to terminate the run when a configured scratchpad key is
truthy. A tiny kt-biome consumer of the pluggable-termination
extension point.

Usage::

    plugins:
      - name: termination_goal
        type: package
        module: kt_biome.plugins.termination_goal
        class: TerminationGoalPlugin
        options:
          scratchpad_key: goal_achieved
          reason: "Goal marked as achieved"

When the agent (or another plugin) writes ``goal_achieved: True`` to
the scratchpad, the next turn's termination vote stops the run with
the configured reason surfaced in ``session_info`` metadata.
"""

from typing import Any

from kohakuterrarium.core.termination import (
    TerminationContext,
    TerminationDecision,
)
from kohakuterrarium.modules.plugin.base import BasePlugin, PluginContext
from kohakuterrarium.utils.logging import get_logger

logger = get_logger(__name__)


class TerminationGoalPlugin(BasePlugin):
    """Vote termination when a scratchpad flag is set to a truthy value.

    ``applies_to`` allows gating to a subset of agents. The plugin
    reads the scratchpad fresh on every termination check; no state is
    stored on the plugin itself.
    """

    name = "termination_goal"
    priority = 50

    def __init__(
        self,
        scratchpad_key: str = "goal_achieved",
        reason: str | None = None,
        **_: Any,
    ) -> None:
        super().__init__()
        self._scratchpad_key = str(scratchpad_key or "goal_achieved")
        self._reason = str(reason or f"Goal flag '{scratchpad_key}' set")

    async def on_load(self, context: PluginContext) -> None:
        logger.info(
            "termination_goal plugin loaded",
            scratchpad_key=self._scratchpad_key,
        )

    def contribute_termination_check(self):
        scratchpad_key = self._scratchpad_key
        reason = self._reason

        def _check(ctx: TerminationContext) -> TerminationDecision | None:
            pad = ctx.scratchpad
            if pad is None:
                return None
            value: Any = None
            # Scratchpad exposes either a dict-like ``get`` or
            # ``to_dict`` depending on implementation.
            if hasattr(pad, "get"):
                try:
                    value = pad.get(scratchpad_key)
                except Exception:
                    value = None
            if value is None and hasattr(pad, "to_dict"):
                try:
                    value = pad.to_dict().get(scratchpad_key)
                except Exception:
                    value = None
            if _is_truthy(value):
                return TerminationDecision(True, reason)
            return None

        return _check


def _is_truthy(value: Any) -> bool:
    """Treat ``True`` / non-empty strings / non-zero numbers as truthy.

    Accepts common string encodings of ``True`` so plugins/tools that
    stringify scratchpad values still work.
    """
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    if isinstance(value, (int, float)):
        return bool(value)
    return bool(value)
