# Development

How the toolchain is pinned, what each key and extra does, and how changes reach `main`.

## Setup

Requires [uv](https://docs.astral.sh/uv/). Two Python versions are declared and
they do different jobs: `pyproject.toml` sets `>=3.11` as the compatibility
floor, while `.python-version` pins *development* to 3.14 so every checkout
builds the same environment. `uv sync` provisions the pinned interpreter
automatically; change it with `uv python pin <version>`.

uv itself is pinned too, by `[tool.uv] required-version` in `pyproject.toml`.
`astral-sh/setup-uv` reads that key, so one line pins the CI runner and your
checkout alike — and a mismatch is a hard error naming the version rather than a
silent split between what your machine checks and what CI does. If `uv sync`
greets you with *"Required uv version `==0.13.7` does not match the running
version"*, that is this, working as intended — `uv self update 0.13.7` puts you
back in step. It gates the project-aware commands only; `uvx ruff` and
`uv self update` are deliberately untouched, the latter so the way out is never
the thing being blocked.

```bash
uv sync
cp .env.example .env   # then fill in ANTHROPIC_API_KEY
```

`ANTHROPIC_API_KEY` is required. `TAVILY_API_KEY` is optional — without it,
`web_search` degrades gracefully, telling the agent to rely on the structured
directory and to flag that reputation data went unchecked. `LANGSMITH_API_KEY`
turns on tracing (see [Tracing](architecture.md#tracing), including what it sends off the
machine).

The browser UI's dependency is a `web` **extra** rather than a core one, so the
CLI and the LangGraph Platform image — neither of which imports Streamlit — do
not carry it or its ~27 transitive packages. A checkout still gets it from
`uv sync` alone, because the `dev` group asks for `event-planner-agent[web]`; a
non-dev install that wants the page needs `uv sync --extra web`.

## Contributing

`main` requires a pull request and passing CI, so every change goes through a
branch, including docs edits and version bumps. The `main requires CI` ruleset
makes the six CI checks required and blocks force pushes and deletion of `main`.
