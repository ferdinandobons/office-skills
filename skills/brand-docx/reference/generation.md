# DOCX Generation

Generation parses an IntermediateDocument, opens the saved shell, clears template
demo content, fills the cover title when provided, writes blocks in order, and
applies only styles resolved from the Brand Profile.

## Run color palette token

An inline run may carry an optional `color` field: a **palette token**, not a
literal color. It is a verbatim key of `theme.palette` - a **theme slot** like
`accent1` (or a dotted role/region-style palette id) - the same id the bundle's
`palette` inventory surfaces and `comprehension.palette_annotations` names. The
resolver maps the token to the captured color `ref` and applies it as the run's
color (a run with no `color` inherits the body/role default). The token is
**never** a literal: a hex-shaped or `#`-bearing value is structurally dropped
before it can reach the writer, so a literal color can never enter through a run.
An unknown token leaves the run inherited and records a graceful
`color_token_unresolved` INFO finding.

An **off-theme** `hex:RRGGBB` palette entry can be NAMED by the model (in
`palette_annotations`) but is **not yet addressable as a run `color` token**: the
`:`-bearing key is rejected by the same run-token validator that blocks bare hex, so
such a token is dropped and the run stays inherited. Only theme-slot / dotted palette
ids are applyable per run today; reference an off-theme color via a theme slot when
the template carries one.
