# Plan: a Streamlit layout that adapts to any display

Status: phase 1 done 2026-11-06, $0 spent, reviewed on a scripted model. Phase 2 stopped at
its spike, which failed criteria 3 and 6; see "Spike results". Written 2026-11-04 for a new
session to carry out.

## Goal

Keep the page comfortable from a phone to an ultrawide monitor, with the approval
panel easiest to use on every size. On narrow displays the page should stay in
today's order: transcript, then approval panel.

## Constraints

- **Python can't see the screen.** `st.context` has no viewport width, so no
  `if width < 768` is possible. All adaptation has to come from how Streamlit's
  layout behaves in the browser.
- **No custom CSS,** following Streamlit's guidance and the project's approach.
- **No new widgets that queue a rerun,** such as a "wide view" toggle. CLAUDE.md
  explains why nothing that reruns may stay live during a streaming turn.
- **`_approval_panel` keeps its rules.** No graph reads while rendering, and widget
  keys stay as they are. Layout changes must not touch `review_token` or `_panel_key`.
- **Streamlit behaviour here is from 1.65.0.** `dependency-upgrade.md` moves it to
  1.66.x, so confirm the signatures below against the installed version.

## Target behaviour

| Display | Sidebar | Main area | Approval panel |
| --- | --- | --- | --- |
| Phone | Hidden, by Streamlit's `auto` default | One column | Below the transcript; figures stack; full-width submit |
| Tablet, laptop | Open | One readable column | Below the transcript |
| Large monitor (phase 2) | Open | Transcript at a capped width | Beside the transcript |
| Ultrawide (phase 2) | Open | Content centred and capped | Beside the transcript |

## Phase 1: make the current layout robust

Small and low-risk. Keep `layout="centered"`.

All in `streamlit_app.py`:

1. **Committed figures** (around line 624). Replace `st.columns(len(figures))` with a
   horizontal container, so figures wrap by available space instead of only below
   640px. `st.columns` gives every figure an equal slice however many there are.

   ```python
   with st.container(horizontal=True):
       for label, value in figures:
           st.metric(label, value, format=..., border=True)
   ```

   Keep the existing order and formatting. `st.columns(0)` no longer applies, but
   keep `_committed_figures` returning an empty list for unusable shapes. Update its
   docstring, which mentions that guard. If metrics don't wrap at the default
   `width="stretch"`, give them a fixed integer `width`.

2. **Submit button** (around line 642). Add `width="stretch"`. The action that commits
   money becomes an easy target on touch screens, and it matches the sidebar's
   download buttons.

3. **Decision control** (around line 559). Add `width="stretch"` to the
   `st.segmented_control`, so the three options are comfortably tappable.

4. **Resume button** (around line 1072). Add `width="stretch"` for the same reason.

5. **Leave as they are:** `chat_input`, the sidebar, and `initial_sidebar_state`
   (`auto` already hides it on small devices). Keep `height=400` on tool results unless
   review shows it trapping scroll on a phone.

## Phase 2: use the space on large displays

### Design

Switch to `layout="wide"`, then let wrapping do the job of a breakpoint:

```python
MAIN_WIDTH = 760  # about today's reading width; confirm in the spike
REVIEW_WIDTH = 440

with st.container(horizontal=True, horizontal_alignment="center"):
    transcript = st.container(width=MAIN_WIDTH)
    review = st.container(width=REVIEW_WIDTH)  # only when an approval is pending
```

If both fit, the panel sits beside the transcript, so an operator can read the
reasoning and the proposed booking together. If not, it should wrap below, which is
today's order. An integer width is documented to shrink to the parent's width, which
is what should keep this safe on a phone.

**This wrapping is unverified.** Streamlit documents wrapping for horizontal
containers, but nothing here has tested it with nested fixed-width containers.

### Spike first

Build a throwaway app outside the repo using the same structure, with a long
transcript, `st.chat_input` and a fragment inside the review container. No model is
needed. Test at 390, 768, 1280, 1920 and 2560px wide, with the sidebar open and closed.

Go ahead only if all of these hold:

1. The panel wraps below the transcript when it doesn't fit, with no horizontal scroll.
2. On a phone, the transcript fills the width.
3. `st.chat_input` stays pinned to the bottom and lines up acceptably with the capped
   transcript. Try `width=MAIN_WIDTH` if it doesn't.
4. The fragment reruns on its own inside the nested container.
5. Streaming into the `st.empty()` slots still works.
6. The page doesn't jump badly when the panel appears.

If any fails, stop at phase 1 and report what happened. Phase 1 stands by itself.

### Implementation

1. Set `layout="wide"` in `st.set_page_config`, and update the comment near line 769
   that credits `layout="centered"`.
2. Put the title, startup alerts, replay and streaming output in the transcript
   container. Prefer `with transcript:` blocks over moving code around. Keep the
   existing order of operations: the replay still runs before the turn streams.
3. Call `_approval_panel` inside the review container. Keep `st.divider()` only where
   the panel stacks; if that can't be detected, drop it in favour of the panel's border.
4. Keep `st.chat_input` at the top level, or it stops pinning to the bottom.
5. Keep the "Resume unfinished turn" flow and its ordering after `_stream_turn`.

### Spike results

Run 2026-11-06 on Streamlit 1.66.1 in headless Chromium, at all five widths with the sidebar
open and closed. Phase 2 was not built.

| # | Criterion | Result |
| --- | --- | --- |
| 1 | Wraps, no horizontal scroll | Pass. Beside from 1920px, stacked below that. |
| 2 | Transcript fills a phone | Pass: 360px of 390px. |
| 3 | `chat_input` pinned and aligned | **Fail**, see below. |
| 4 | Fragment reruns alone | Pass: fragment runs went up, full runs did not. |
| 5 | Streaming into slots | Pass. |
| 6 | No bad jump when the panel appears | **Fail**: the transcript moves 228px sideways at 1920px. |

Criterion 3 has no good option, measured at 1920px with the sidebar open and the transcript
at x=721:

- `width="stretch"` at the top level stays centred, but is 1489px wide under a 760px
  transcript, and 2129px at 2560px.
- `width=760` at the top level sits at x=356, 365px left of the transcript. It cannot be
  centred, because nothing wraps a top-level `chat_input`.
- Inside `st.bottom` with a centred row, it lines up exactly but the page stops following the
  output: `scrollTop` stayed at 0 through a streamed turn. A 672s turn would run off-screen.

Criterion 6 is the panel recentring the pair, and it moves back after each decision. Reserving
the column would stop that, but leaves the transcript off-centre the rest of the time.

If this is reopened, use `vertical_alignment="top"`. The page scrolls to the top when the panel
appears, so a top-aligned panel was fully visible and a bottom-aligned one was not visible at all.

### Found on the way: the approval panel lands off-screen

This is the current page, not phase 2. `chat_input` is not rendered during an approval, and the
page jumps to the top when it goes. On the real page with a long transcript, `scrollTop` was
0 of 5026 right after the turn, with the submit button 5702px down. Not fixed here.

## Verification

```bash
uv run pytest
uvx ruff check .
uvx ruff format --check .
uvx ty check
```

In particular, these must still pass:

- `test_the_panel_reads_no_graph_state_while_rendering`
- `test_a_second_interrupt_is_not_pre_approved`
- `test_panels_are_inert_on_a_run_that_streams_a_turn`
- `test_a_resumed_proposal_is_replaced_rather_than_drawn_twice`
- The `at.metric` assertions in `tests/test_streamlit_page.py`, which depend on order

`AppTest` can't measure layout, so check by hand in a real browser at the five spike
widths, in both light and dark modes. Use a throwaway `EVENT_PLANNER_DB`. Check:

- No horizontal scrolling anywhere, including `st.json` with long argument values.
- The approval panel on a phone: badge, arguments, decision, figures and submit can
  all be reached and tapped. Check edit and reject as well as approve.
- Wrapping with one figure, two figures, and more than four.

Reaching a real approval needs a live model turn, so get the operator's go-ahead for
that cost. Send them screenshots at each width.

Add `CLAUDE.md` notes for anything that bites silently, such as `chat_input` having to
stay at the top level.

## Out of scope

Custom CSS, JavaScript-based viewport detection, a separate mobile page, `st.tabs` for
switching views, and moving sidebar settings into the main area.
