# Notes on `deepagents` 0.7.32

Three places where the published guidance and the installed package disagree.
All were found by inspecting the package, and all are covered by tests:

1. **`write_todos` is not bound by default.** The docs describe
   `TodoListMiddleware` as always present; it is not. `agent.py` adds it
   explicitly, since the orchestrator prompt instructs the model to plan with
   it. `test_planning_tool_is_bound` guards this.
2. **Backend constructors changed.** The docs show `StateBackend(rt)` and
   `lambda rt: StoreBackend(rt)`. The installed signatures are `StateBackend()`
   and `StoreBackend(*, namespace, store=None)`, and `backend` takes a direct
   instance rather than a factory.
3. **`create_deep_agent` gained a `memory=[...]` parameter** backed by
   `MemoryMiddleware`, which loads memory files into the system prompt. It is
   not mentioned in the current guidance.
