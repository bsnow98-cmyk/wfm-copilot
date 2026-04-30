# Design System — WFM Copilot

> The AI shows its math.

Every visual decision in this document serves that one line. When in doubt, ask: does this make the math more visible or less? Cut anything that doesn't.

---

## Product Context

- **What this is:** AI-native, open-source workforce management platform for contact centers. Forecasts call volume, computes staffing via Erlang C, optimizes schedules with CP-SAT, and surfaces it all through a chat copilot that calls real tools and renders charts inline.
- **Who it's for:** Contact center operations supervisors (the daily user who flagged "not user-friendly to review"). Plus recruiters and hiring managers scanning a portfolio piece.
- **Space/industry:** Workforce management (WFM). Adjacent to Genesys Cloud, NICE IEX, Verint, Calabrio. None of those products lead with chat.
- **Project type:** Web app (dashboard) with a persistent chat panel. No marketing site in v1.

---

## Aesthetic Direction

- **Direction:** Industrial / Utilitarian, with editorial-grade typographic care.
- **Decoration level:** Minimal. Typography and spacing do all the work.
- **Mood:** Calm, focused, professional. Real software, not a marketing demo. The AI is invisible; the work is visible.
- **Reference sites (target feel, not style copy):** Linear's calm precision, Stripe's typographic rigor, Vercel's restraint. Explicitly **not**: Genesys Cloud's enterprise UI density, ChatGPT's avatar-first chat, generic SaaS dashboards.

---

## Typography

| Role | Font | Weights | Notes |
|---|---|---|---|
| Display + body | **Geist** | 400 / 500 / 600 | Free, Google Fonts deliverable, distinctive. |
| Monospace (IDs, timestamps, exact numbers) | **IBM Plex Mono** | 400 / 500 | The "math" font. Citations live here. |
| ❌ Banned as primary | Inter, Roboto, Arial, Helvetica, Open Sans, Lato, Montserrat, Poppins, Space Grotesk, system-ui | — | All convergent AI-generated tells. |

**Loading:** Google Fonts via `<link>` in document head. Self-host in v1.1 if performance demands it.

**Modular scale (px / line-height):**

| Token | Size | Line-height | Use |
|---|---|---|---|
| `text-xs` | 12 | 16 | Captions, table densities, footnotes |
| `text-sm` | 14 | 20 | **Body default**, labels, chat messages |
| `text-base` | 16 | 24 | Section headings (within views) |
| `text-lg` | 18 | 24 | Subheadings, emphasized content |
| `text-xl` | 22 | 28 | Page titles |
| `text-2xl` | 28 | 32 | Auth gate headline only |
| `text-3xl` | 36 | 40 | Reserved (not used in v1) |

**Tabular numerals always on** for tables, charts, agent grids, scenario columns. CSS: `font-feature-settings: "tnum"`.

**Monospace usage rules:**
- Anomaly IDs: `<code>a3f291d4</code>` inline in chat prose
- Timestamps: `<code>14:30</code>` in tables
- Exact percentages: `<code>92.4%</code>` in scenario summaries
- Never for general body text. Mono is a signal, not a style.

---

## Color

- **Approach:** Restrained. One accent + neutrals + semantic. Color is rare and meaningful.

| Token | Hex | Use |
|---|---|---|
| `accent` | `#0F766E` | Primary buttons, links, focus rings, active nav item |
| `accent-hover` | `#0D5F58` | Button hover state |
| `severity-low` | `#65A30D` | Anomaly severity dot — low |
| `severity-medium` | `#CA8A04` | Anomaly severity dot — medium |
| `severity-high` | `#DC2626` | Anomaly severity dot — high |
| `text-primary` | `#0A0A0A` | Body, headings |
| `text-secondary` | `#525252` | Subheadings, secondary content |
| `text-muted` | `#737373` | Timestamps, captions, disabled |
| `border` | `#E5E5E5` | Default 1px borders |
| `border-strong` | `#A3A3A3` | Emphasized borders, focused inputs |
| `surface` | `#FFFFFF` | Application background |
| `surface-subtle` | `#FAFAFA` | Skeleton fills, table-row alternation |

**Dark mode:** Not in v1. Dense numeric data is harder to read white-on-black. Revisit in v1.1 if user research warrants it.

**Anti-rules:**
- ❌ No purple, violet, or indigo anywhere
- ❌ No gradients (decorative or functional)
- ❌ Severity colors NEVER used for non-severity decoration
- ❌ Accent color used sparingly. If a screenshot has 30% accent color, it's wrong.

---

## Spacing

- **Base unit:** 4px.
- **Density:** Comfortable for the main canvas, compact for chat.
- **Scale:** `2xs(2) xs(4) sm(8) md(12) base(16) lg(24) xl(32) 2xl(48)`

**Container padding:**
- Chat panel content: `16px` horizontal, `12px` vertical between turns
- Main canvas views: `24px` horizontal, `24px` vertical between sections
- Auth gate card: `32px` all sides

**Vertical rhythm:** 16px between sections within a view. 24px between major sections.

---

## Layout

- **Approach:** Grid-disciplined. Predictable alignment. Workspace columns over decorative cards.
- **Page shell:**
  ```
  ┌──────────────────────────────────────┐
  │ Top nav (56px)                       │
  ├──────────────────────────┬───────────┤
  │                          │           │
  │ Main canvas              │ Chat      │
  │ (fluid width)            │ (420px)   │
  │                          │           │
  └──────────────────────────┴───────────┘
  ```
- **Top nav:** 56px fixed height, white background, 1px bottom border.
- **Main canvas:** Fluid width, max-content-width 1200px on extra-large screens.
- **Chat panel:** 420px fixed when open, collapsible to 0px (toggle in top nav).
- **Border radius:**
  - 4px on inputs, buttons, severity pills
  - 6px on chart containers, table containers, scenario columns
  - 0 on table cells
  - **No `rounded-full` anywhere except severity dots (4px circles).**

---

## Motion

- **Approach:** Minimal-functional. Motion only when it aids comprehension.
- **Easing:** `ease-out` (entrances), `ease-in` (exits), `ease-in-out` (state changes).
- **Duration:**
  - Streaming dots (chat indicator): 200ms loop, 3-state opacity
  - Skeleton shimmer: 1500ms loop, subtle gradient shift
  - Chart entrance fade: 200ms `ease-out` on first paint
  - Tab switch in top nav: instant, no transition
  - Modal/dialog (none in v1): N/A
- **Anti-rules:**
  - ❌ No decorative scroll-driven animation
  - ❌ No micro-interactions on hover (other than subtle accent color shift)
  - ❌ No "thinking" animations on the AI (the streaming dots are the only feedback)
  - ❌ No reveal animations as content scrolls into view

---

## Anti-Slop Guards (verified at every merge)

- ❌ No purple/violet/indigo
- ❌ No 3-column feature grid (Scenarios columns are workspace columns, not feature cards)
- ❌ No icons in colored circles as decoration (severity dots only, encoding data)
- ❌ No centered-everything (default left-aligned)
- ❌ No bubbly border-radius (4-6px max, 4px circles only on severity dots)
- ❌ No decorative blobs / floating shapes / wavy SVG dividers
- ❌ No emojis as design elements
- ❌ No colored left-border on cards
- ❌ No `system-ui` / Inter / Roboto as primary type
- ❌ No avatar circles / bubble tails / "AI Assistant" branding in chat
- ❌ No "Built for X" or "Designed for Y" copy patterns
- ❌ No hero section with stock-photo background

---

## Accessibility

- **Tab order:** Top nav → main canvas controls → chat panel input
- **Keyboard shortcut:** `Cmd/Ctrl+K` opens chat panel input from anywhere
- **Headings:** Each view has a single `<h1>` matching the page title
- **Charts:** `aria-label` describes the data ("Forecast vs actual call volume for [queue], last 30 days")
- **Severity:** dot SHAPE + visually-hidden text ("severity: high"), never color alone
- **Chat messages:** announced via `aria-live="polite"` ("User said:", "Assistant said:")
- **Tap targets:** All interactive elements ≥ 44px tap area (use padding to meet, not visual size)
- **Contrast:**
  - Body text: ≥ 4.5:1 (Geist 14px on white passes at #525252 and darker)
  - Chart axes / labels: ≥ 3:1
  - Severity dots: shape + label, color is supporting evidence only
- **Mobile (`<768px`):** Auth gate works fully. Dashboard shows "Best on desktop" notice. Real mobile UI is v1.1 territory.

---

## Copy & Voice

- **Voice:** Direct, useful, no hype. Speaks to ops people who already know WFM jargon.
- **Banned phrases:** "Welcome to WFM Copilot," "Unlock the power of...", "Your all-in-one solution," "Built for [X]"
- **Empty states:** Always include a path forward. "No forecast yet — pick a queue."
- **Error states:** Always include a retry. "Couldn't reach the API. [Try again]"
- **Auth gate copy:** "WFM Copilot. Demo password required." That's it.

---

## Decisions Log

| Date | Decision | Rationale |
|---|---|---|
| 2026-04-29 | Initial design system created | Authored via `/design-consultation` after `/plan-ceo-review`, `/plan-eng-review`, `/plan-design-review` chain |
| 2026-04-29 | Memorable thing locked: "The AI shows its math" | Differentiates from generic AI-chat-on-SaaS pattern; aligns with chat-first architecture |
| 2026-04-29 | Industrial/Utilitarian aesthetic | Fits ops-people user; resists AI-slop convergence |
| 2026-04-29 | Geist + IBM Plex Mono | Free, distinctive, pairs cleanly; mono surfaces citations as data |
| 2026-04-29 | Single accent (#0F766E teal-green) | One color forces hierarchy through type and spacing |
| 2026-04-29 | Dark mode deferred to v1.1 | Dense numeric data harder to read white-on-black |
| 2026-04-29 | Three deliberate risks accepted | Mono inline in chat, no avatar, no shadows — all serve "math is the hero" |

---

## See Also

- `~/.gstack/projects/wfm-copilot-vault/ceo-plans/2026-04-29-wfm-copilot-roadmap.md` — full CEO + eng + design plan
- Vault: `~/Desktop/Projects/wfm-copilot-vault/` — Obsidian thinking archive
