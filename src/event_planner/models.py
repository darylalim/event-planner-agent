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
from typing import Any, Literal, get_args

from langchain.chat_models import init_chat_model
from langchain_core.language_models import BaseChatModel

Effort = Literal["low", "medium", "high", "xhigh", "max"]

#: In ascending order, so a picker can list them as a scale.
EFFORT_LEVELS: tuple[Effort, ...] = get_args(Effort)

#: The output cap for every model built here, set explicitly for the same reason
#: as effort. `langchain-anthropic` derives its default `max_tokens` from a table
#: of model profiles, and an id newer than the installed package is not in it:
#: 1.6.1 knows `claude-opus-5` (128,000) but not `claude-opus-5-5` or
#: `claude-sonnet-5-5`, which fall back to 4,096 with no warning. Measured live:
#: the budget analyst's `budget.md` ran past 4,096 tokens, every `write_file` was
#: cut off before its `content` argument, and it retried 34 times — $1.85 of a
#: $2.50 brief. 64,000 is within every current model's output limit, Haiku 4.5's
#: included, and leaves a long file far from the edge.
MAX_OUTPUT_TOKENS = 64_000


@dataclass(frozen=True)
class ModelChoice:
    """A model id and the effort to run it at.

    `effort=None` sends no effort at all, so the model's own default applies.
    That exists for models that reject the parameter — Claude Haiku 4.5 is one —
    not as a way to spell "default": the roster below never uses it.
    """

    model: str
    effort: Effort | None

    def build(self) -> BaseChatModel:
        """Resolve to a chat model with this choice's effort and the output cap.

        Always built here, never handed to deepagents as a bare id: a bare id is
        resolved with the package's profile default, which is the 4,096 trap
        above. Through `init_chat_model` rather than `ChatAnthropic` directly, so
        a provider-prefixed id like `anthropic:claude-opus-5-5` still parses.
        """
        kwargs: dict[str, Any] = {"max_tokens": MAX_OUTPUT_TOKENS}
        if self.effort is not None:
            kwargs["effort"] = self.effort
        return init_chat_model(self.model, **kwargs)


#: The orchestrator plans, delegates, and is the only role that calls the two
#: approval-gated tools, so it gets the strongest everyday model. `high` rather
#: than `xhigh`: this agent is interactive — a live brief already takes 672s
#: end to end — and `xhigh` should be earned by measurement, not assumed.
ORCHESTRATOR_MODEL = ModelChoice("claude-opus-5-5", "high")
