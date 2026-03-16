# Site Roadmap

How this page should improve over time.

## Current state (v0 — starter)

Static single-page site in `docs/` served via GitHub Pages. Catppuccin Mocha
theme, no build step, no JS. Hotlinks the self-demo video from issue #11.

## Near-term improvements

### Content

- **Screenshots / GIFs of real sessions.** The tool grid is text-only right now.
  Each tool card could show a brief inline clip or screenshot of it in action
  (e.g. the attach viewer rendering syntax-highlighted code, the ingest modal,
  the SSH command output).
- **A proper hero demo.** The self-demo video is a stress-test artifact, not a
  polished product demo. A screen recording of a real user session — upload CSV,
  get analysis, preview a chart — would be more compelling than the Manim-style
  narrated slides.
- **Recipes as expandable sections.** The current recipe list is flat text.
  Clicking a recipe could expand to show a transcript or annotated screenshot
  of the interaction.
- **FAQ / troubleshooting section.** Common issues: "my sandbox won't start",
  "I get a timeout", "how do I access my files from last time".

### Design

- **Favicon.** Currently none. A small lathe/turning icon or the `/` glyph from
  the hero would work.
- **og:image.** No social card yet. Needed for link previews on Twitter, Discord,
  Slack.
- **Anchor links on section headings.** For sharing deep links to specific sections.
- **~~Responsive polish.~~** ✅ Architecture diagram uses `clamp()` font sizing.
- **Dark/light toggle.** The Catppuccin Mocha theme is dark-only. A Latte variant
  toggle would be nice but is low priority (the audience skews dark-mode).

### Technical

- **Prefers-reduced-motion.** The video autoplays and loops. Should respect
  `prefers-reduced-motion: reduce` by pausing or showing a static poster frame.
- **Lighthouse audit.** Haven't run one yet. Likely issues: missing `alt` on
  the video, missing `lang` region specificity, contrast ratio on dim text.
- **Structured data.** JSON-LD for the project (SoftwareApplication schema) would
  improve search appearance.

## Medium-term

- **Subpages.** The single page is fine now but will outgrow itself. Candidates
  for dedicated pages:
  - `/recipes` — full recipe walkthroughs with annotated transcripts
  - `/admin` — deployment guide extracted from the README
  - `/changelog` — release notes
- **Search.** If subpages happen, a client-side search (Pagefind, Lunr) becomes
  useful.
- **Analytics.** Something minimal and privacy-respecting (Plausible, Umami, or
  just GitHub Pages traffic stats).
- **~~Custom domain.~~** ✅ Live at [lathe.tools](https://lathe.tools).

## Long-term / aspirational

- **Interactive demo.** An embedded sandbox session (like StackBlitz or Replit
  embeds) where visitors can try a Lathe-enabled model without creating an
  account. Major infrastructure lift, but the most compelling possible landing
  page.
- **Video gallery.** As more self-demo videos are produced (issue #11 pattern),
  curate them as a gallery of what agents have built inside Lathe.
- **Community recipes.** User-submitted recipes with a lightweight contribution
  flow (PRs to a `recipes/` dir, rendered as subpages).
