# Plan: retention for conversation threads

Status: not started. Written 2026-11-05 for a new session to carry out.
Do `dependency-upgrade.md` first: this work needs its uv version fix. Read the measurements below
before starting, because this is not urgent until real client threads exist.

## Goal

Give operators a safe, explicit way to delete whole conversation threads that have been idle for a
set age, so a client's conversation is not kept in raw form forever. Nothing in the store may be
touched, and a thread waiting on a human decision is never deleted.

## What this does and does not achieve

- **It limits how long raw conversations are kept.** That is the reason to do this. Disk space is
  not: see the growth figures below.
- **It removes the conversation for good.** Unlike an `/artifacts/` purge, no preview is left
  behind, and the thread name starts empty if it is used again.
- **It is not full erasure.** `/memories/`, `/events/`, `/artifacts/`, `exports/`, backups and
  LangSmith traces all outlive the thread. Do not describe this feature as erasing client data.

## Measurements

All taken on 2026-11-05 from `.state/planner.sqlite`, or a scratch copy of it, unless noted.

| Measure | Result |
| --- | --- |
| `checkpoints` | 479 rows, 11.4 MB, across 8 threads and 11 namespaces |
| `writes` | 1,182 rows, 1.9 MB |
| `store` | 4 rows, 44 KB |
| Newest checkpoint | 2 August 2026, so every thread here is about 95 days old |
| File size after deleting 4 of the 8 threads | Unchanged at 14.9 MB, with 2,089 free pages |
| File size after `VACUUM` as well | 6.3 MB |

**These threads predate the current storage format, so they overstate growth.** deepagents now keeps
`messages` in a `DeltaChannel`. An offline scripted run on the installed version grew in a straight
line, with a 4 KB model message per tool round:

| Tool rounds | Checkpoint and write bytes |
| --- | --- |
| 10 | 229 KB |
| 20 | 436 KB |
| 40 | 968 KB |

That is about 24 KB per round, with an occasional larger snapshot row (120 KB at 40 rounds). The
messages are synthetic, so treat this as the shape of the growth, not a real thread's size.

## Findings that shape the design

- **Delete whole threads, never history inside one.** `SqliteSaver.prune` raises
  `NotImplementedError`, and its base-class docs warn why: a `DeltaChannel` rebuilds by walking the
  parent chain, so a hand-written `keep_latest` makes `messages` silently come back empty, with no
  error. Do not write that SQL.
- **`delete_thread` covers every namespace.** It deletes by `thread_id` alone, so subagent rows go
  with the parent. The offline run confirmed both tables end at zero rows.
- **Only an interrupt protects a thread.** 4 of the 8 threads have `next` set and no interrupt:
  abandoned turns, including ones the host reaped. Protecting `next` would keep them forever. A
  pending interrupt is different, because it is a decision nobody has made yet.
- **Last activity must span all namespaces.** In 2 of the 8 threads the newest checkpoint is a
  subagent's, later than the newest root checkpoint. Reading the root alone dates them too early.
  `saver.list({"configurable": {"thread_id": t}}, limit=1)` returns the newest across namespaces.
- **The age comes from `checkpoint["ts"]`.** It is an ISO string in UTC with an offset, for example
  `2026-08-02T05:44:41.590759+00:00`. Parse it, don't compare strings.
- **There is no public list of threads.** Use `select distinct thread_id from checkpoints` on
  `saver.conn`, as `live_check.namespace_messages` already does. An upstream schema change then
  fails loudly rather than finding nothing.
- **`get_state` reads old threads.** All 8 pre-delta threads read without error. `build_agent` also
  builds with its default model and no API key, and makes no model call.
- **Deleted rows stay in the file.** `secure_delete` is off, so without `VACUUM` the text remains in
  free pages. Since removal is the point here, the purge has to compact the file too.
- **Thread-scoped store rows are left behind.** An anonymous caller's files sit under
  `("event_planner", <kind>, "t", <thread>)`. They survive, and a later thread with the same name
  will see them. That is acceptable: they are the planner's files, and the same is true of a named
  user's. Say so in the docs.
- **The store does not know which thread wrote an artifact.** User-scoped `/artifacts/` entries
  cannot be deleted along with a thread, which is one more reason to keep this store-free.

## Design

Operator-run, dry-run by default, matching `artifacts-retention.md`. Do not purge on startup: two
front ends share the database, and a delete nobody asked for should not ride on opening a page.

- `--days` is **required, with no default**. Planning threads can be resumed months later and this
  loses them entirely, so the age should be a choice each time. Suggest 365 in the docs. An event
  can be planned a year out, so 90 is too short here even though it suits spill.
- Real deletion needs `--yes-delete`, matching `live_check.py`'s `--yes-spend`.
- A thread idle for exactly the cutoff is kept: delete only when `last_active < now - older_than`.
- Three outcomes for an old thread:
  - **Pending interrupt:** kept, and reported. Answering it in a front end writes new checkpoints,
    so it then ages from that date.
  - **`next` set, no interrupt:** deleted, and listed separately in the dry run so the operator can
    resume it first.
  - **`get_state` raises:** kept and reported. Fail closed.
- Output names thread ids, last-active dates and row counts, so the operator can see what would go.
  Thread ids are the operator's own labels. Never print message content or store keys.
- After a real delete that removed anything, compact the file. If the database is locked, report
  that the deletes succeeded and the compaction did not, and exit non-zero.

## Steps

1. **Re-measure.** Repeat the table above on the newest database available, reading it with
   `mode=ro` and printing counts only. Note whether any thread is in the current format.

2. **Add `src/event_planner/retention.py`:**

   ```python
   @dataclass(frozen=True)
   class ThreadInfo:
       thread_id: str
       last_active: datetime
       checkpoints: int
       unfinished: bool


   @dataclass(frozen=True)
   class PurgeReport:
       scanned: int
       matched: tuple[ThreadInfo, ...]
       kept_pending: tuple[ThreadInfo, ...]
       kept_unreadable: tuple[str, ...]
       deleted: int


   def purge_threads(
       saver: SqliteSaver, graph: Any, *, older_than: timedelta, now: datetime, delete: bool = False
   ) -> PurgeReport: ...
   ```

   - Collect every decision first, then delete.
   - Re-read last activity and interrupts immediately before each delete, and skip a thread that has
     changed. Someone may resume it between the scan and the delete.
   - Reject `older_than <= 0`, and a naive `now`.
   - Do not import anything from the store. Taking no store argument is what keeps the purge out of
     `/memories/` and `/events/`.

3. **Add `scripts/purge_threads.py`**, beside `live_check.py` so it stays out of the wheel:

   ```bash
   uv run scripts/purge_threads.py --db P --days 365               # dry run
   uv run scripts/purge_threads.py --db P --days 365 --yes-delete  # deletes
   ```

   - `--db` and `--days` are both required.
   - Validate `--db` with `_check_db_outside_workspace`, passing a suitable `knob`.
   - Check the file exists **before** calling `open_persistence`, which creates the file and its
     parent folders, or a mistyped path makes a new empty database and reports nothing to purge.
   - Open with `webui.open_persistence`, for its WAL and busy-timeout settings, and build the graph
     with `build_agent(checkpointer=saver, store=store)`.
   - To compact, call `close_persistence`, then on a plain connection with a 30-second timeout run
     `VACUUM` and `PRAGMA wal_checkpoint(TRUNCATE)`.

4. **Add offline tests** in a new `tests/test_retention.py`. Use a real `SqliteSaver` from
   `open_persistence`, because that is what ships. `saver.put` can write a checkpoint with a chosen
   `ts`. The newest row is picked by `checkpoint_id`, so give those rows ids that sort with their
   timestamps.

   - An old thread is deleted and a recent one is kept.
   - **A store entry of every kind survives a purge**, the most important test.
   - **A thread parked on a real `hold_venue` interrupt survives at any age.** Drive it with
     `scripted` and `_hold_call`, as `test_harness.py` does, and pass a far-future `now`.
   - A thread with `next` set and no interrupt is deleted, and is marked `unfinished`.
   - A thread whose root is old but whose subagent namespace is recent is kept.
   - Subagent rows go with the parent, in both tables.
   - The deletes are visible from a second connection.
   - The dry run reports matches and deletes nothing.
   - A thread idle for exactly the cutoff is kept.
   - `older_than <= 0` and a naive `now` are rejected.
   - A `get_state` that raises keeps the thread.
   - A thread that changes between the scan and the delete is skipped.
   - `SqliteSaver.prune` still raises `NotImplementedError`, so the day upstream adds one, someone
     re-reads this plan rather than hand-rolling it.
   - The script refuses to delete without `--yes-delete`, and without `--days`.
   - A mistyped `--db` is an error, and no file is created.
   - After a real delete through the script, a planted marker string is gone from the file's bytes.

5. **Update the docs:**

   - `docs/architecture.md`: add retention to the Storage section, including what survives and that it isn't
     erasure.
   - `CLAUDE.md`: add the `DeltaChannel` trap to "Invariants that fail silently", add the command,
     and update the test count and file list.

6. **Try it on a copy** of a real database, never the original. Compare the dry-run list with what
   was deleted, confirm the `store` row counts did not change, and confirm a kept thread still reads
   through `get_state`.

## Verification

```bash
uv run pytest
uvx ruff check .
uvx ruff format --check .
uvx ty check
```

No live model run is needed, since prompts and agent behaviour don't change. If the operator runs
this on `.state/planner.sqlite`, have them back it up first. All 8 threads there are past 90 days, so
a short `--days` would delete every one.

## Out of scope

- Compacting history inside a live thread, for the reason given under findings.
- Deleting store data along with a thread.
- Hosted deployments. This script can't reach a platform checkpointer.
- Erasure requests for a single user. `user_id` travels in `context=`, not in the checkpoint's
  columns, so finding a user's threads needs its own design.
- Scheduling. Operators can use cron, and `--yes-delete` makes that their explicit choice.
