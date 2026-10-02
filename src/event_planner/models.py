"""Which model each agent role runs on, and at what effort.

Effort is `output_config.effort` in the Messages API: how much the model
thinks, and how many tool calls it spends getting there. It is a property of
the role, not the model — the same Sonnet does a shortlist at `medium` and a
budget check at `high` — so the two travel together as a `ModelChoice`.

Effort is always set explicitly rather than left to the API, because the
defaults differ per model and move between releases: Claude Opus 5.5 defaults
to `medium`, one level *below* the `high` Claude Opus 5 defaulted to. Moving
the orchestrator from one to the other with effort unset would have quietly
lowered how hard the agent that books venues thinks.

Kept apart from `agent.py` so `subagents.py` can name its roles' choices
beside the roles themselves without importing the module that imports it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, get_args

from langchain.chat_models import init_chat_model
from langchain_core.language_models import BaseChatModel

Effort = Literal["low", "medium", "high", "xhigh", "max"]

#: In ascending order, so a picker can list them as a scale.
EFFORT_LEVELS: tuple[Effort, ...] = get_args(Effort)


@dataclass(frozen=True)
class ModelChoice:
    """A model id and the effort to run it at.

    `effort=None` sends no effort at all, so the model's own default applies.
    That exists for models that reject the parameter — Claude Haiku 4.5 is one —
    not as a way to spell "default": the roster below never uses it.
    """

    model: str
    effort: Effort | None

    def build(self) -> str | BaseChatModel:
        """Resolve to what `create_deep_agent` accepts as a model.

        With no effort the id is passed through untouched, and deepagents
        resolves it exactly as it did before effort existed here. With one, it
        goes through `init_chat_model` — not `ChatAnthropic` directly — so a
        provider-prefixed id like `anthropic:claude-opus-5-5` still parses.
        """
        if self.effort is None:
            return self.model
        return init_chat_model(self.model, effort=self.effort)


#: The orchestrator plans, delegates, and is the only role that calls the two
#: approval-gated tools, so it gets the strongest everyday model. `high` rather
#: than `xhigh`: this agent is interactive — a live brief already takes 672s
#: end to end — and `xhigh` should be earned by measurement, not assumed.
ORCHESTRATOR_MODEL = ModelChoice("claude-opus-5-5", "high")
