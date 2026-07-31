"""Live web search via Tavily.

This is the one tool that hits a real external service. It is constructed
lazily so the package imports cleanly without `TAVILY_API_KEY` set — useful for
tests and for `--help`, which should not require credentials.
"""

from __future__ import annotations

import os

from langchain.tools import tool
from langchain_tavily import TavilySearch

_search: TavilySearch | None = None


def _client() -> TavilySearch:
    global _search
    if _search is None:
        _search = TavilySearch(max_results=5, search_depth="advanced")
    return _search


@tool
def web_search(query: str) -> str:
    """Search the live web for venue reviews, vendor reputation, or local context.

    Args:
        query: A specific search query. Prefer concrete queries
            ("Presidio Officers Club Hall noise curfew") over broad ones
            ("San Francisco venues"), since the structured directory already
            covers listings.

    Returns ranked search results with titles, URLs, and content snippets.
    """
    if not os.environ.get("TAVILY_API_KEY"):
        return (
            "web_search unavailable: TAVILY_API_KEY is not set. "
            "Continue using the structured directory tools and state clearly "
            "that live reputation and review data could not be checked."
        )
    try:
        result = _client().invoke({"query": query})
    except Exception as exc:  # noqa: BLE001 - surface as a tool result, not a crash
        return f"web_search failed: {type(exc).__name__}: {exc}"

    if isinstance(result, dict):
        results = result.get("results", [])
        if not results:
            return f"No results for '{query}'."
        lines = [f"Results for '{query}':\n"]
        for item in results:
            lines.append(
                f"- {item.get('title', 'untitled')}\n"
                f"    {item.get('url', '')}\n"
                f"    {(item.get('content') or '')[:400]}"
            )
        return "\n".join(lines)
    return str(result)
