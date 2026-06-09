<!-- SPDX-License-Identifier: MIT -->
# Roadmap and planned features

Planned, designed-but-not-yet-built work for BrandDocs. Each entry records the
problem, the root cause (with code references), and a design that is **compatible
with the architecture as it exists today** so the work can start without a rewrite.

The non-negotiable constraint for every item below: **off-brand output stays
impossible by construction.** Brand-specific values (style names, fonts, hex
colors) live only in the Brand Profile, only the resolver reads them, and `verify`
refuses a profile that points at anything the template does not actually contain
(`check_resolver_targets`, fail-closed). No design here may weaken that.

---

## 1. Brand typography capture (direct-formatting -> role `appearance`)

> **Status: shipped (font family + size + color).** The capture/verify/apply
> pipeline below is implemented for THREE independent axes - font family, font size,
> and run color. Extraction records the dominant direct run value (sampled over ALL
> runs, so a minority accent never wins) into `role.appearance` and the document
> defaults `theme.fonts.body` (font/size) + `theme.text.body` (color); `verify`
> re-validates each applied value against the shell (`appearance_targets_exist`:
> fonts vs fontTable+theme, sizes vs the template's `w:sz` set, colors vs the theme
> palette + the template's `w:color` set); and generation applies them as direct run
> formatting through the resolver, with the body size/color default gated off heading
> roles. Still future: **per-word accents**, **cover-layout reconstruction** (the
> separate `cover.kind = NONE` gap), and **heading typography** when a template fakes
> headings in the body style (route via `comprehension.role_annotations`). The design
> below documents the full feature.

### Problem
A generated document does not always match the template's **real visible
typography**. Observed on a real template (`Come funziona la Sotto-community.docx`):
the original renders its title in **Montserrat** and its body in **Roboto**, with a
brand-blue accent, but the generated document came out in **Arial**.

### Root cause (verified)
The brand's real fonts live **only as direct run-level formatting** on the
template's content, not in any reusable artifact the extractor reads:

- Extraction maps roles from **named paragraph/table styles only**. It never reads
  run-level `w:rFonts` / `w:color` / `w:sz` (`formats/docx/roles.py:infer_roles`,
  `formats/docx/extract.py`).
- `_extract_theme` does not even capture the theme's real major/minor latin fonts
  (it stores `latin: None` with hardcoded `Arial` / `Calibri` fallbacks) and does
  not read `docDefaults` (`formats/docx/extract.py:_extract_theme`).
- Generation applies a role as a **named style only** (`target_obj.style = ...`);
  there is no code path that applies font/color directly from the profile. Run
  toggles are semantic (bold/italic/underline) and explicitly never carry a brand
  font or color (`formats/docx/generate.py:_apply_style` / `_apply_run_toggles`).

So the only fonts the styles resolve to are `docDefaults = Arial`, and the
template's actual Roboto/Montserrat were never captured anywhere.

### Proposed design (compatible)
Use the **already-reserved, currently-empty `appearance: {}` field on each role**
(`formats/docx/roles.py`; present in the envelope, populated by nothing today) as
the home for captured typography. Three additive layers:

1. **Capture (extract).**
   - Fix `_extract_theme` to read the real major/minor latin typefaces from
     `theme1.xml` and the `docDefaults` `rFonts`, so `theme.fonts` is truthful.
   - Add a deterministic typography sampler: for each role, sample the **dominant**
     direct run formatting actually applied to that role's runs (font, run color,
     size). When a clear convention dominates (not noise), record it into the
     role's `appearance` (e.g. `appearance: {font: {latin: "Roboto"}, color:
     {hex: "..."}, size_pt: 11}`) with a confidence. This is the
     "deterministic disposes" half of the existing pattern.

2. **Verify (keep the guarantee).**
   - Extend the resolver-target check with an `appearance` validator: a captured
     font is legitimate only if the **shell itself uses/embeds it** (its
     `fontTable` / referenced fonts); a captured color must be a theme color or a
     hex actually present in the template. A profile naming a font the template
     never uses is an ERROR, exactly like a missing style today
     (`qa/checks_deterministic.py:check_resolver_targets`).

3. **Apply (generate).**
   - Add one new code path in `_apply_resolved_style` that, after setting the named
     style, applies the role's `appearance` as direct run/paragraph formatting,
     reading the values **only from the profile via the resolver** (no literal in
     the writer). Roles without `appearance` (every profile that exists today)
     behave exactly as now.

### Compatibility notes
- Schema is **additive** (`appearance` already exists): bump `schema_version`
  minor; old profiles keep working unchanged.
- The guarantee holds: fonts/colors are captured **facts about the template**,
  stored in the profile, applied only by the resolver, and re-validated against the
  shell by `verify`. You can only ever apply typography the template itself uses.
- Deterministic; no model required. The optional `comprehend` step could later
  confirm which captured appearance is intentional vs incidental.

### Scope / non-goals (first cut)
- Role-level **dominant** typography (font + dominant color + size). Per-word
  accents (e.g. only the word "Collabfor.it" in blue) are an `IntermediateDocument`
  authoring concern, out of scope.
- Cover-layout reconstruction (the separate `cover.kind = NONE` detection gap) is
  tracked independently; typography capture alone already makes a `heading.1` title
  render in the brand font.

---

## 2. Profile learns from generation QA findings (a feedback loop)

### Problem
If extracting a profile leads to repeatable problems at generation time (a role
that resolves to a weak/missing style, a fragment that degrades, a rejected
number format, a demo value that keeps reappearing), the **next** generation from
the same profile hits the same problems and needs the same repair loop. We want
those profile-driven problems to be learned once and not recur, so a subsequent
generation is faster (fewer repair/regenerate rounds).

### Root cause (verified)
Generation findings are **ephemeral**: collected into a `findings` out-param,
folded into the `QAReport`, and printed to stdout, but **not persisted per run**
and never fed back into the profile (`cli.py` generate subcommand;
`formats/docx/generate.py`). Only `visual_manifest.json` is persisted, and it
records L1/OCR findings, not the generation/L0 findings. Nothing closes the loop.

### Proposed design (compatible)
Reuse the existing seams rather than inventing a parallel system:

1. **Persist the signal.** Write a per-run `generation_report.json` next to the
   output (mirrors the existing `visual_manifest.json` side artifact): the
   `QAReport` findings (`check`, `severity`, `message`, `location`) plus the shell
   sha256, a content hash, and a timestamp. Additive; no behavior change.

2. **A shell-bound learned-corrections registry.** Record distilled corrections on
   the profile under the **already-present `rules.overrides`** seam (or a sibling
   `lessons` block), frozen to the shell exactly like comprehension
   (`source_shell_sha256 == provenance.shell.sha256`;
   `profile/store.py:comprehension_is_present`). A correction may only ever
   **re-point within artifacts the shell already defines** (e.g. "role X's weak
   style -> fall back to role Y", "fragment Z degrades repeatedly -> deprioritize",
   "this number_format mask was rejected -> use shell-backed mask M", "demo value V
   keeps surviving -> register it for demo-clearing"). It can never invent a style,
   font, or color.

3. **Who writes lessons.** Two phases:
   - Phase A (deterministic, no model): a `learn` step distills corrections from
     the **unambiguous** findings (`resolver_targets_exist`, `style_fallback`,
     number-format rejection, residual-demo hits). Deterministic finding ->
     deterministic correction.
   - Phase B (model-assisted, later): ambiguous findings are proposed by the model
     in a comprehend-like step ("model proposes, deterministic disposes",
     all-or-nothing fail-closed merge, frozen) using the existing comprehension
     writer mechanism.

4. **Freeze and invalidate** lessons on `source_shell_sha256`, mirroring
   comprehension: a re-extract (shell changes) resets them, since they were learned
   about a specific shell.

5. **Consume at generate time.** The generator consults the lessons registry after
   the base resolver and before writing, applying learned re-points. No lessons ->
   identical to today's behavior.

### Why it speeds up the next run
The first generation may need a repair loop; the distilled lessons are cached on
the profile and reused across sessions, so the second generation starts from the
already-corrected profile and tends to pass QA on the first attempt.

### Compatibility notes
- Reuses `rules.overrides`, the comprehension freeze pattern, the `Finding` /
  `QAReport` structures, and `write_profile_json` (sorted keys). The new
  `generation_report.json` mirrors `visual_manifest.json`.
- Fail-closed guarantee preserved: every lesson is re-validated against the shell
  by `verify` (a lesson pointing at a now-missing artifact is dropped); lessons
  reference only shell-backed artifacts.
- Fully backward compatible (no lessons -> today's behavior).

### Risk to manage
Auto-applied corrections could entrench a wrong fix or mask a real authoring
problem. Mitigation: lessons carry provenance (which runs/findings produced them)
and confidence; they stay advisory until a threshold or an explicit accept
(mirroring `verify --accept`).

---

## 3. Interactive profile refinement from user feedback (human-in-the-loop)

### Problem
The Brand Profile only ever learns from what the engine can *measure*. After a
generation, the engine cannot tell whether a heading that **should** be the brand
teal came out black, whether a section it preserved is actually boilerplate the
user wants dropped, or whether it misread *what an extracted element is for*. Those
are qualitative judgements only the **user** can make. Today there is no moment in
the workflow where the skill asks, and no channel for that answer to improve the
profile, so the same off-brand deviation recurs on every future generation.

### Proposed design (compatible)
Add an explicit **ask-and-refine** step to the agent workflow, complementary to
the machine-driven feedback loop in section 2 (which learns from QA findings):

1. **Ask (agent guidance, `SKILL.md`).** After returning the generated file and the
   QA summary, the agent asks the user a targeted question: *"Does this match your
   template? Is anything off-brand or deviating from the original - a heading that
   should be colored, a section that should not repeat, an element whose purpose I
   read wrong?"* The question is grounded in what was actually extracted (it can
   name the roles / palette entries / sections it used), so the user reacts to
   concrete choices rather than a blank prompt.

2. **Refine (model proposes).** The user's qualitative answer is turned by the model
   into structured refinements written **through the same fail-closed channels that
   already exist** - `comprehension.role_annotations` / `palette_annotations` /
   cover-slot and index conventions (the *purpose* of each extracted element), plus
   the shell-bound lessons/overrides registry from section 2 (a re-point within
   artifacts the shell already defines). The model can sharpen *what an element is
   for* and re-point within shell-backed artifacts; it can **never** invent a style,
   font, color, or layout the template does not contain.

3. **Confirm and freeze (deterministic disposes).** The proposed profile refinement
   is shown as a diff, the user confirms (mirroring `verify --accept`), and it is
   frozen to the shell (`source_shell_sha256`, like comprehension). `verify`
   re-validates every refinement against the shell, so a confirmed-but-impossible
   change is still refused fail-closed.

### Why it matters
This is how the skill **progressively learns to manage the extracted elements and
their scope**: each round enriches the comprehension's understanding of what each
role / color / section is *for*, so subsequent generations are more faithful with
less correction. Section 2 learns from what the machine can check; this learns from
what only the user can judge - together they close the loop from both sides.

### Compatibility notes
- Reuses the comprehension writer (the one model-writable, fail-closed, shell-frozen
  block), `role_annotations` / `palette_annotations`, and the section-2 lessons
  registry. No new brand-value channel: the model annotates purpose and re-points
  within shell-backed artifacts only.
- Fully optional and backward compatible: with no feedback the profile is unchanged
  and generation behaves exactly as today.
- Off-brand stays impossible by construction: the user's confirmation never lets a
  refinement reference an artifact the shell lacks - `verify` is still the floor.

### Risk to manage
A confident-but-wrong user answer (or an over-eager model interpretation) could
entrench a bad refinement. Mitigation: always present the change as a confirmable
diff, keep refinements advisory until accepted, and let a later re-extract reset
them (they are shell-bound).

---

## 4. Visual audit (salvaged future ideas)

Carried over from the now-removed `VISUAL_AUDIT_IMPROVEMENTS.md` (most of which
shipped: LibreOffice+Poppler+Pillow path, doctor preflight, PyMuPDF fallback,
optional Tesseract OCR, deep/strict modes, manifest diagnostics, the L2 repair
loop). Genuinely still-future items:

- **Renderer-disagreement cross-check.** Compare `pdftoppm` vs PyMuPDF renders
  (page count, dimensions, coarse ink maps) to flag rasterizer-specific artifacts.
- **Richer image analysis.** Add `numpy` + `opencv-python`/`scikit-image` to move
  L1 from pixel proxies to bounding-box / connected-component / overlap heuristics
  and template-vs-output diff heatmaps.
- **OCR confidence scoring** and deeper stale TOC/field-cache detection
  (nested/multi-column TOCs, page-number-aware static entries).
