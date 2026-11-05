# Plan: retention for `/artifacts/`

Status: parked 2026-11-05 at step 1, with nothing built. Written 2026-11-04 for a new session to carry out.

Step 1 found nothing to purge, so the operator chose `checkpoint-retention.md` instead:

- `.state/planner.sqlite` holds no artifact rows, but its last store write (2 August) predates the
  `/artifacts/` route (25 August), so it proves nothing alone.
- None of the 83 runs captured in `evals/results/` stored an artifact file, including the full
  briefs. Each capture collects `artifacts` for the run's user.
- Spill needs a tool result over about 80,000 characters, or a human message over about 200,000. The
  largest recorded tool result is 30.6 KB, and `web_search` cuts each result's content to 400
  characters.

**Reopen this plan if** any artifact row appears in a real database, or a new tool can return a large
result. The findings and design below still hold. Re-check them then.

Do `dependency-upgrade.md` first: this work calls the store API that upgrade moves,
and it needs that plan's uv version fix. It does not depend on
`budget-analyst-effort.md`.

## Goal

Give operators a safe, explicit way to delete `/artifacts/` entries older than a
set age, for every user and thread. Nothing else in the store may be touched.

## What this does and does not achieve

- **It bounds spill growth.** Today nothing evicts `/artifacts/`.
- **It is not a privacy deletion.** `_evict_and_truncate_messages` truncates the
  model request, so an offloaded brief probably remains in the thread's
  checkpoints. Do not describe this feature as erasing client data.
- **It does not address the largest data.** In the local `.state/planner.sqlite`
  on 2026-11-04, `checkpoints` held about 11.3 MB in 479 rows, and `store` held
  about 44 KB in 4 rows, none of them artifacts. That database may predate the
  `/artifacts/` route, so measure first (step 1).

## Findings that shape the design

- **Do not use the store-wide TTL.** `StoreBackend` calls `store.put` with no
  `ttl`, so that write takes the store's `default_ttl`. Setting
  `SqliteStore(ttl=...)` would therefore also expire `/memories/` and `/events/`.
  The same trap applies to any platform-level store TTL.
- **Two default page limits apply.** `store.search` returns 10 rows and
  `store.list_namespaces` returns 100, both silently. Page through both, or the
  purge misses data and reports success.
- **`namespace_for_user` is not enough.** Anonymous callers are stored under
  `("event_planner", "artifacts", "t", <thread>)`, and `_stored` returns nothing
  for them. Enumerate by prefix instead.
- **Deletion degrades old threads.** An offloaded tool result stays in the
  conversation as a head and tail preview plus its path. After a purge, the agent
  keeps the preview but `read_file` on that path fails. Planning threads can be
  resumed months later, so the default age must be long.
- **Never match on deepagents' subfolder names.** Those are deepagents' to change,
  so select by the `artifacts` namespace only.

## Design

Operator-run, dry-run by default. Do not purge on startup: two front ends share the
database, and a delete nobody asked for should not ride on opening a page.

- Default age: **90 days**, measured from `updated_at`. This is a judgment call,
  set high because keeping spill is cheap and losing it hurts a resumed thread.
- Real deletion needs `--yes-delete`, matching `live_check.py`'s `--yes-spend`.
- Operators can schedule the command themselves, for example with cron.

## Steps

1. **Measure.** Report row counts and bytes per namespace kind for `store`, plus
   `checkpoints` and `writes`, from a recent database. Print counts only, not
   values, because these rows are client data. If artifacts are still negligible,
   tell the operator before continuing, since checkpoint retention may be the
   better use of this work.

2. **Expose the prefix.** Add `artifacts_prefix()` to `context.py`, returning
   `(*_ROOT, "artifacts")`, rather than importing the private `_ROOT` or repeating
   the literal. Have `artifacts_namespace` share the same `"artifacts"` constant.

3. **Add `src/event_planner/retention.py`:**

   ```python
   @dataclass(frozen=True)
   class PurgeReport:
       namespaces: int
       matched: int
       deleted: int


   def purge_artifacts(
       store: BaseStore, *, older_than: timedelta, now: datetime, delete: bool = False
   ) -> PurgeReport: ...
   ```

   - Page through `list_namespaces(prefix=artifacts_prefix())`, then `search` in each.
   - Collect every match first, then delete. Deleting while paging by offset skips
     rows.
   - Refuse any namespace that does not start with `artifacts_prefix()`, even
     though the query should never return one.
   - Reject `older_than <= 0`.
   - Confirm `updated_at`'s timezone from a real `SqliteStore` item before comparing.
   - Report counts only, never keys or values.

4. **Add `scripts/purge_artifacts.py`**, beside `live_check.py` so it stays out of
   the wheel:

   ```bash
   uv run scripts/purge_artifacts.py --db P --days 90               # dry run
   uv run scripts/purge_artifacts.py --db P --days 90 --yes-delete  # deletes
   ```

   Open the database with `webui.open_persistence` and close it with
   `close_persistence`, so it gets the same WAL and busy-timeout settings. Validate
   `--db` with `_check_db_outside_workspace`, passing a suitable `knob`. A missing
   `--db` must be an error. `open_persistence` creates the file and its parent folders,
   so check the file exists **before** calling it, or a mistyped path makes a new empty
   database and reports nothing to purge.

5. **Add offline tests** in a new `tests/test_retention.py`:

   - An old artifact is deleted and a recent one is kept.
   - **Old `/memories/` and `/events/` entries survive**, the most important test.
   - The dry run reports matches and deletes nothing.
   - User-scoped and thread-scoped namespaces are both purged.
   - More than 10 items in a namespace, and more than 100 namespaces, are all
     reached. Also assert the bare calls still stop at 10 and 100, as
     `test_stored_items_lists_past_the_stores_default_page` does.
   - An entry exactly at the cutoff follows a defined rule.
   - `older_than <= 0` is rejected.
   - A namespace outside the prefix is refused.
   - Reading a purged path through the agent's backend returns an error and does
     not raise.
   - The script refuses to delete without `--yes-delete`.
   - A mistyped `--db` is an error, and no file is created.

   Use a real `SqliteStore` for at least the timestamp and paging cases, because
   that is the store that ships.

6. **Update the docs:**

   - `README.md`: add retention to the Storage section, including that it isn't
     erasure and that a resumed thread keeps only the preview.
   - `CLAUDE.md`: add the store-wide TTL trap to "Invariants that fail silently",
     add the command, and update the test count and file list.
   - `_stored`'s docstring in `cli.py`, which says "nothing evicts it".

7. **Try it on a copy** of a real database, never the original. Compare the dry-run
   count with the real count, and confirm the memories and events row counts did
   not change.

## Verification

```bash
uv run pytest
uvx ruff check .
uvx ruff format --check .
uvx ty check
```

No live model run is needed, since prompts and agent behaviour don't change. If the
operator runs this on `.state/planner.sqlite`, have them back it up first.

SQLite reuses freed pages, so the file will not shrink without `VACUUM`. That is
fine here and is not part of this work.

## Out of scope

- Checkpoint retention, which is the larger data and deserves its own plan.
- Hosted deployments. This script can't reach a platform store, so a hosted
  deployment needs its own per-namespace approach.
- Erasure requests for a single user.
- Cleaning `/events/` or `/memories/`.
