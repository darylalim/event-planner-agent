# Plan: make `send_invitations` send real email

Status: not started. Written 2026-11-04 for a new session to carry out.
This is the project's first real side effect. The operator must make the decisions below
before implementation starts.

## Goal

Replace the `send_invitations` stub with real email to a real guest list, without
weakening the approval gate. A sent invitation can't be recalled, so a wrong or
duplicate send is the failure to design against.

`hold_venue` stays a stub. Venues have no common booking API, so it needs its own design.

## Findings that shape the design

- **There is no guest list.** The tool takes `recipient_count`, a number the model
  supplies. No addresses exist anywhere, so this can't be a drop-in swap. README says to
  keep tool signatures stable, but this one has to change.
- **A resume can run a tool again.** CLAUDE.md records turns stopping with `next` set,
  continued by the "Resume unfinished turn" button. A crash after sending and before the
  checkpoint would send everything twice unless the tool itself prevents it.
- **Traces sit outside tenant isolation.** Tool arguments and results reach LangSmith
  when tracing is on, so guest addresses must not appear in either.
- **The approval panel shows tool arguments.** An operator must be able to see who
  would receive the email, and a count alone doesn't show that.
- **`edit` is offered on every gated tool.** `ALLOWED_DECISIONS` is one global list, so
  today an `edit` could change `event_slug` and point an approved send at another list.
- **No tool reaches the store yet.** Nothing under `src/` uses `ToolRuntime`, so a tool
  reading user-scoped data is a new pattern here.
- **Tracing defaults on.** `.env.example` sets `LANGSMITH_TRACING=true`, so anyone who
  copies it traces by default.

## Operator decisions

| Decision | Recommendation |
| --- | --- |
| Email provider | A transactional API with idempotency keys and a test mode. Compare current options when this starts, then confirm the pick |
| Sending domain | A domain the operator controls, with SPF, DKIM and DMARC set up |
| Maximum recipients per send | 500, set in config |
| Who replies go to | The planner's own address, through `Reply-To` |
| Consent and legal review | The operator confirms guests expect these emails, and checks what applies to them |

## Design

### Guest list stays out of the model's hands

The model names a list. It never supplies addresses, so injected text can't add a
recipient.

- The operator uploads a guest list through a front end. The agent can't write it.
- Store it in a new `guests` namespace, with a route or store access added per CLAUDE.md:
  a new path holding user data needs a route in `build_backend()` and a factory in
  `context.py`. If the agent should not read it at all, keep it out of the agent's routes
  and reach it only from the tool. **Prefer that.**
- Validate on upload: address syntax, duplicates, and the maximum size.
- Give each saved list a `list_version`, a hash of its normalised contents. Any change
  makes a new version.

**Reaching the store.** Give the tool a `ToolRuntime` parameter, hidden from the model's
schema, and read through `runtime.store` with a `guests_namespace(runtime)` helper in
`context.py`. Confirm this against the installed `langchain` first, and add a test
proving the parameter isn't exposed to the model. Validate `event_slug` with the same
rules as namespace components. Guest lists need a real `user_id`: refuse a send on the
anonymous thread-scoped path.

### New tool contract

```python
send_invitations(
    event_slug: str,
    event_name: str,
    event_date: str,
    venue_name: str,
    list_version: str,
    expected_recipient_count: int,
    rsvp_deadline: str,
)
```

Add a read-only `guest_list_summary(event_slug)` tool that returns the count and
`list_version`, never addresses, so the model can fill these in. Bind it to the
orchestrator only, and name it in the prompt so the drift tests pass.

The tool loads the list for `event_slug` and refuses if `list_version` or the count
differs. This binds an approval to the exact list: replacing addresses after approval,
even at the same count, sends nothing. Results report counts and a reference only, never
addresses.

**No `edit` on this tool.** Offer only approve and reject for `send_invitations`, so an
approved send can't be redirected. To change something, the operator rejects with a
reason and the model proposes again. Keep `INTERRUPT_ON` derived from
`IRREVERSIBLE_TOOLS`, adding a small per-tool override, and confirm both front ends take
their options from the interrupt's `allowed_decisions` rather than assuming all three.
`hold_venue` keeps all three.

Update the three-file loop together: the tool, `prompts.py`, and any subagent or skill
naming it. `IRREVERSIBLE_TOOLS` is unchanged, so the gate stays.

### Approval panel and CLI

Show the operator the real recipient count, the list's name and `list_version`, a few
masked addresses, the rendered subject and body, and the mode (`dry-run` or `live`). Render these with the
page's existing helpers: `markdown_literal` for values. The panel must not read graph
state while rendering, so resolve this before the panel draws. Both front ends must show
the same facts.

### Fixed template

The body comes from a fixed template with approved fields, not free text from the model.
Escape every field. This stops a compromised or careless turn from writing arbitrary
email content, and it's what the operator reviews.

### Duplicate protection

1. Derive a send key from user, event slug, list version and content.
2. Before sending, record `pending` under that key in the store.
3. Send in provider batches, passing an idempotency key per batch.
4. Record each batch as sent. Mark the whole send `complete` at the end.
5. A repeat with the same key sends only what isn't recorded, and reports that.
6. A changed list or edited content gets a new key, and goes back through approval.
7. The ledger is the source of truth, because provider idempotency keys expire. A batch
   left `pending` may or may not have gone out, so never resend it automatically. Report
   it, and require the operator to review it.

### Modes and failure

- `EVENT_PLANNER_SEND_MODE` is `dry-run` by default. `live` must be set explicitly, and a
  missing or unknown value means `dry-run`.
- `dry-run` does everything except the provider call, and says so in its result.
- **Live sending is for the CLI and web page only.** There is no upload screen or masked
  preview in Studio or on a hosted deployment, so an operator there could approve without
  seeing recipients. Decide this at build time: `hosted=True` must give a dry-run sender
  whatever the environment says. Don't rely on caller-supplied context, which a hosted
  API client controls.
- **`live` refuses to run while tracing is on,** until a test proves no guest data reaches
  traces. Use LangSmith's own check for whether tracing is enabled, so all its env var
  names are covered.
- Put the provider behind a small interface: `FakeSender` for tests, one real adapter.
- A partial failure reports sent and failed counts, and never claims success. Rate limits
  are retried a bounded number of times. Refuse to run if credentials are missing in
  `live` mode.
- Every result must be accurate enough that the model doesn't retry. Follow
  `_decline_message`'s framing for refusals.

## Steps

1. Confirm the operator's decisions, and read the chosen provider's current docs.
2. Add the guest-list storage, upload and validation, with isolation tests.
3. Add the sender interface, `FakeSender` and send modes.
4. Change the tool, prompts and skills together.
5. Add the send ledger and the duplicate protection.
6. Update the approval panel and the CLI prompt.
7. Add the real adapter. It must import cleanly in the deployed shape and must add no
   network call at import time. Use `/astral:uv` to add any dependency.
8. Update `.env.example`, README's "What's real and what's stubbed", and CLAUDE.md.
9. Bump the version in `pyproject.toml` for this feature. `release.yml` tags and releases
   from that version on `main`, so get the operator's go-ahead first.

## Tests

All offline, with `FakeSender`. The suite must never reach a provider.

- Approval is still required, and reject sends nothing.
- `edit` isn't offered for `send_invitations`, and a forged `edit` decision sends nothing.
- `hold_venue` still offers all three decisions.
- Unknown list, count mismatch, `list_version` mismatch and over-limit each refuse and send
  nothing.
- A list replaced after approval at the same count sends nothing.
- An anonymous thread-scoped caller can't send.
- `hosted=True` never reaches the real sender, even with `live` set.
- `live` refuses while tracing is enabled.
- A batch left `pending` is reported and not resent.
- **A crash after sending and before the checkpoint, then a resume, sends nothing twice.**
- A crash mid-batch resumes without repeats.
- One tenant can't reach another's list, including through a crafted `event_slug`.
- No address appears in tool arguments, tool results, messages or checkpoints.
- Template fields are escaped, including header injection in `event_name`.
- `dry-run` is the default, and an unknown mode doesn't send.

Add `reject-send`, `count-mismatch` and injection scenarios to `evals/`, if it exists.

## Verification

```bash
uv run pytest
uvx ruff check .
uvx ruff format --check .
uvx ty check
```

Then, with the operator's go-ahead, test in this order:

1. `dry-run` from both front ends.
2. The provider's test mode.
3. One live send, to addresses the operator owns.
4. Repeat that send, and confirm no second email arrives.

Keep `dry-run` as the default in every committed config. Do not run a live send in CI or
in `scripts/live_check.py`.

## Before real guests

Do not use this on a real guest list until these are also done:

- Authentication, if the page serves anyone beyond one person on loopback. `user_id` is
  an unverified text field, so anyone reaching the port could use another tenant's list.
- A decision on retention and deletion for guest data.
- Tracing confirmed off in the deployed setup. The `live` refusal above enforces this
  until a test proves traces carry no guest data.

## Out of scope

RSVP tracking, bounce handling, calendar attachments, a real `hold_venue`, and bulk
marketing email.
