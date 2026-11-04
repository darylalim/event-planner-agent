# Plan: "Ink on paper" Streamlit theme

Status: not started. Written 2026-11-04 for a new session to carry out.
Independent of the other plans, except that it should land before
`readme-restructure.md`, which moves the README's description of the theme.

## Goal

Replace the cold grey palette in `.streamlit/config.toml` with a warm pair of light
and dark themes that read as one design. Typography, spacing and the security
settings do not change.

## Design

The page is a working tool for checking costs and approving bookings, so it should
feel calm and precise, with the approval panel as the most prominent thing on it.

- **Warm neutrals, indigo accent.** Paper-toned surfaces and warm charcoal replace
  zinc grey. Indigo sits opposite those warm tones on the colour wheel, so the one
  accent stands out without needing more saturation.
- **Keep `#5850EC` in both modes.** The button that commits money looks the same in
  each. A lighter dark-mode indigo (`#6D66F0`) was tried and rejected: white text
  on it is only 4.37:1.
- **Indigo is not a status colour.** Red stays reserved for irreversible actions and
  errors, and green for success, so the primary action can't be mistaken for either.

## What "complementary" means here

The two modes are mirrors, and that is measurable:

- Every neutral comes from one warm hue family.
- Surfaces step the same way: page, then sidebar, then panels, then code.
- Each step has a similar contrast in both modes (table below).
- Each semantic colour keeps its hue, darker in light mode and lighter in dark mode.

## Measured results

WCAG contrast ratios, calculated on 2026-11-04. Nothing has been checked visually yet.

| Pair | Light now | Light new | Dark now | Dark new | Target |
| --- | --- | --- | --- | --- | --- |
| Text on page | 19.90 | 17.06 | 15.19 | 15.22 | >= 7 |
| Text on panel | 19.06 | 15.52 | 13.96 | 13.58 | >= 7 |
| Text on code | 18.10 | 15.10 | 13.96 | 12.80 | >= 7 |
| Link on panel | 7.57 | 7.01 | 8.89 | 8.24 | >= 4.5 |
| White on primary | 5.56 | 5.56 | 5.56 | 5.56 | >= 4.5 |
| Primary on page | 5.56 | 5.43 | 3.46 | 3.31 | >= 3 |
| Panel against page | 1.044 | 1.099 | 1.088 | 1.120 | Matched |
| Sidebar against page | 1.044 | 1.062 | 1.048 | 1.058 | Matched |
| Border on page | 1.27 | 1.59 | 1.29 | 1.82 | Stronger, matched |

- **Panels:** light mode's panel step was 1.044 against dark's 1.088, so panels were
  harder to see in light mode. The new values nearly match.
- **Borders:** both modes get stronger borders, which matters because
  `showWidgetBorder` separates inputs in the approval panel.
- **Known shortfall:** borders stay under the 3:1 WCAG guideline for control
  boundaries. `borderColor` also draws every divider, so 3:1 would make the page
  heavy. This is a deliberate trade-off, and it still improves on today.
- **Semantic colours:** all are >= 4.5:1 as text on both page and panel in both modes.
- **Badges:** do not set `redBackgroundColor`, `redTextColor` or similar. Hand-picked
  pairs measured worse in light mode than Streamlit's derived ones (red 5.49 against
  7.98). Those derived figures (7.18 or higher) assume the 10%/20% fill and 15%
  lightness shift described in `config.toml`, so confirm the badge visually.

## Change

Edit only `.streamlit/config.toml`. Replace the values below and keep `[server]`,
`[browser]` and `[theme]` as they are.

```toml
[theme.light]
primaryColor = "#5850EC"
backgroundColor = "#FDFCFA"
secondaryBackgroundColor = "#F4F1EC"
codeBackgroundColor = "#F1EEE8"
textColor = "#1C1917"
linkColor = "#4338CA"
borderColor = "#CFCAC2"
codeTextColor = "#1C1917"
redColor = "#C62828"
orangeColor = "#B8430F"
yellowColor = "#8F5C0E"
greenColor = "#1B7A3D"
blueColor = "#2563EB"
violetColor = "#7C3AED"
grayColor = "#6F6A64"

[theme.dark]
primaryColor = "#5850EC"
backgroundColor = "#151413"
secondaryBackgroundColor = "#211F1D"
codeBackgroundColor = "#262421"
textColor = "#EDE9E3"
linkColor = "#A5B4FC"
borderColor = "#45413C"
codeTextColor = "#EDE9E3"
redColor = "#F87171"
orangeColor = "#FB923C"
yellowColor = "#FACC15"
greenColor = "#4ADE80"
blueColor = "#60A5FA"
violetColor = "#A78BFA"
grayColor = "#A8A29E"

[theme.light.sidebar]
backgroundColor = "#F7F5F1"
secondaryBackgroundColor = "#EDE9E2"
codeBackgroundColor = "#EDE9E2"
textColor = "#1C1917"
borderColor = "#CFCAC2"
primaryColor = "#5850EC"

[theme.dark.sidebar]
backgroundColor = "#1B1A18"
secondaryBackgroundColor = "#2A2825"
codeBackgroundColor = "#2A2825"
textColor = "#EDE9E3"
borderColor = "#45413C"
primaryColor = "#5850EC"
```

Keep both `[theme.light]` and `[theme.dark]`, which is what gives the app its
light/dark toggle. Do not add custom CSS or a webfont: the file's header explains why
the page makes no third-party font requests.

## Steps

1. Create a branch and apply the values above.

2. Update the comments in `config.toml`. Keep the existing reasoning, and revise any
   figure that changed. The `#5850EC` comment's 5.56:1 and ">=3:1" claims still hold.
   Add a short note that the two modes are deliberate mirrors, and that a change to
   one needs the matching step checked in the other.

3. Check `README.md` near line 351 and `CLAUDE.md` for descriptions of the theme that
   no longer apply.

4. Review it visually with `uv run streamlit run streamlit_app.py`, in **both** modes
   using the settings-menu toggle. Use a throwaway database through
   `EVENT_PLANNER_DB`, because a real turn writes memories. Check:

   - The approval panel: "Irreversible" badge, JSON arguments, segmented control and
     the primary submit button.
   - Warning, error and info alerts.
   - Tool-result panels, code blocks and `st.metric`.
   - Sidebar inputs, downloads and captions.
   - Chat messages, and the separation between panels and the page.

   Reaching the approval panel needs a live model turn, which costs money, so get the
   operator's go-ahead first. Screenshots of both modes should go to the operator
   for sign-off, because contrast figures can't judge whether it looks right.

## Verification

```bash
uv run pytest
```

No test covers the theme, so this only confirms nothing else moved. `config.toml` is
read as project-level config in the suite, and these keys don't affect how it runs.

If a colour changes during review, recalculate its rows in the table above with any
WCAG contrast tool, and keep every target met.

## Out of scope

Typography, layout, custom CSS, webfonts, chart palettes (the page draws no charts)
and any change to `streamlit_app.py`.
