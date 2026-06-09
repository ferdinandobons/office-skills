# SPDX-License-Identifier: MIT
"""DOCX brand typography capture (font family, size, and color).

The brand's REAL visible typography often lives as DIRECT run-level formatting
(``w:rPr/w:rFonts`` / ``w:sz`` / ``w:color``) on the template's content rather than
in the named styles or the theme: a designed template may put everything in
``Normal`` with a direct Roboto / Montserrat override at 22 half-points in accent1.
Role inference (``roles.py``) and theme extraction read only named styles and
``theme1.xml``, so those direct values are never captured and a generated document
falls back to the ``docDefaults`` font/size/color.

This module captures the DOMINANT direct run typography, deterministically, as
THREE INDEPENDENT axes (font family, size, color) sampled in a SINGLE pass:

  - per role: the dominant explicit value among the runs that use the role's style
    -> ``role['appearance']['font'] = {'latin': <name>}`` /
    ``role['appearance']['size_hp'] = <int>`` /
    ``role['appearance']['color'] = {'kind': ...}``;
  - the document's effective body typography: the dominant explicit value across all
    body runs -> ``theme['fonts']['body']['latin'/'size_hp']`` and
    ``theme['text']['body']['color']`` - the fallbacks the generator applies to a
    paragraph whose role carries no captured value.

Each axis is independent: a role may carry a captured size but no captured font
(or vice versa). Only a clear DOMINANT is recorded per axis (at least
:data:`_MIN_RUNS` explicit values and a winner covering at least
:data:`_MIN_DOMINANCE` of them), with its dominance stored as a per-axis confidence
(``confidence`` for font, ``size_confidence`` for size, ``color_confidence`` for
color). Capture is deterministic (model-free).

The brand guarantee is preserved: every captured value is a FACT observed in the
template, stored in the profile, applied only via the resolver, and re-validated
against what the shell proves it contains by ``check_appearance_targets``
(fail-closed). This module is purely additive - it only populates the already-
reserved ``appearance`` field and additive ``theme.fonts.body`` / ``theme.text``
keys; a template with no dominant direct value leaves all of them untouched, so
behavior is unchanged.
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Optional

from docx.enum.dml import MSO_COLOR_TYPE, MSO_THEME_COLOR

from brandkit.common import color as colorutil
from brandkit.ooxml import names
from brandkit.profile import schema

# The 12 canonical theme slots a palette theme-key may name (single registry).
_THEME_SLOTS: frozenset[str] = frozenset(colorutil.THEME_SLOTS)

# A capture is only trusted when it is a clear convention, not noise.
_MIN_RUNS = 3  # need at least this many explicit values to call a winner
_MIN_DOMINANCE = 0.6  # the winner must cover >= 60% of those values

# An accent color is SPARSE by design (a few runs of brand red on a body of black
# text), so the palette accent bucket uses ONLY a low count floor and NOT the
# _dominant _MIN_DOMINANCE gate: a color seen on at least this many runs but not the
# document-dominant body color is recorded as an "accent" entry.
_MIN_ACCENT_RUNS = 3

# The closed ``where`` vocabulary for a palette entry's provenance (LOCKED). Every
# provenance fact is one of these four observed sources; nothing else may be
# recorded. ``palette_role`` is the only NON-authoritative source (the hardcoded,
# template-invariant ``theme.palette_roles`` map), recorded for context but never
# trusted as brand evidence.
PALETTE_WHERE: frozenset[str] = frozenset(
    {"palette_role", "role.appearance", "run.color", "link.color"}
)

# python-docx's sentinel for a theme color that maps to no real slot; its
# ``xml_value`` is the truthy string ``"UNMAPPED"``. It is not a brand token (verify
# has no slot for it and apply cannot realize it), so it is never captured.
_UNMAPPED_THEME_TOKEN = MSO_THEME_COLOR.NOT_THEME_COLOR.xml_value

# WordprocessingML qualified-name builder for the link-color helper (it walks the
# raw ``w:hyperlink`` ancestor / ``w:rStyle`` to detect a link run).
_W = names.make_qn("w")

# The hyperlink character-style ids whose presence on a run marks it as link text
# even when it is NOT physically nested under a ``w:hyperlink`` element (a manually
# styled cross-reference). Closed, spec-fixed style ids (NOT brand literals).
_HYPERLINK_RSTYLES: frozenset[str] = frozenset({"Hyperlink", "FollowedHyperlink"})


def _dominant(counter: Counter) -> Optional[tuple[Any, float]]:
    """Return ``(value, dominance)`` for the most common EXPLICIT value when it is a
    clear convention over ALL sampled runs, else ``None``.

    A ``None`` key counts runs that carry NO explicit value on this axis (they inherit
    from the style/theme). Those "inherit" votes count toward the denominator but can
    never WIN: a value is a convention only when it dominates EVERY run, not just the
    explicit minority. This is what stops a 2%-of-runs accent color (the 98% inherit
    their color) from being mistaken for the document's body color, while a body that
    really is explicitly Roboto/16pt on (almost) every run still captures."""
    total = sum(counter.values())
    if total < _MIN_RUNS:
        return None
    candidates = [(value, n) for value, n in counter.items() if value is not None]
    if not candidates:
        return None
    value, n = max(candidates, key=lambda item: item[1])
    ratio = n / total
    if ratio < _MIN_DOMINANCE:
        return None
    return value, ratio


def _run_size_hp(run) -> Optional[int]:
    """The run's EXPLICIT size as half-points (``w:sz@w:val``), or ``None``.

    ``run.font.size`` is an explicit-only ``Length`` (``None`` when the size is
    inherited from the style/theme), so a run that inherits its size contributes
    nothing. The half-point bucket ``round(pt * 2)`` matches OOXML's ``w:sz`` unit.
    """
    try:
        size = run.font.size
        if size is None:
            return None
        return round(size.pt * 2)
    except Exception:
        # A malformed measure the OOXML layer refuses to parse contributes nothing
        # to this axis - capture must never crash the extraction.
        return None


def _run_color(run) -> Optional[tuple[str, ...]]:
    """The run's EXPLICIT color as a hashable bucket key, or ``None``.

    ``run.font.color`` is a ``ColorFormat`` whose ``.type`` is ``None`` when the
    color is inherited. An RGB color buckets as ``('hex', <RRGGBB>)``; a THEME color
    buckets as ``('theme', <wordprocessingml token>)`` from the slot's
    ``.theme_color.xml_value`` (e.g. ``'accent1'``, ``'text1'``). AUTO / None / an
    unmapped theme slot contributes nothing (it is not a captured brand value).
    """
    try:
        color = run.font.color
        ctype = color.type
        if ctype == MSO_COLOR_TYPE.RGB and color.rgb is not None:
            return ("hex", str(color.rgb))
        if ctype == MSO_COLOR_TYPE.THEME:
            token = getattr(color.theme_color, "xml_value", None)
            # Drop the UNMAPPED sentinel: it is not a verifiable/appliable brand
            # token, so it must never enter the profile (keeps apply/verify in sync).
            if token and token != _UNMAPPED_THEME_TOKEN:
                return ("theme", token)
    except Exception:
        # A spec-valid-but-unmappable themeColor (e.g. 'none'/'phClr') makes
        # python-docx raise on access; that run contributes nothing to this axis
        # rather than crashing the extraction.
        return None
    return None


def _color_obj(bucket: tuple[str, ...]) -> dict:
    """Turn a captured color bucket key back into its stored ``appearance`` object."""
    if bucket[0] == "hex":
        return {"kind": "hex", "hex": bucket[1]}
    return {"kind": "theme", "theme": bucket[1]}


def capture_fonts(doc, roles: dict, theme: dict) -> None:
    """Capture dominant direct run typography (font, size, color) into ``roles``
    (per role ``appearance``) and the document defaults (``theme['fonts']['body']``
    for font/size, ``theme['text']['body']`` for color), mutating both in place.

    Reads only the EXPLICIT run value per axis (``run.font.name`` / ``run.font.size``
    / ``run.font.color``); a run that inherits an axis from the style/theme
    contributes nothing to THAT axis (the three axes are sampled independently).
    python-docx resolves a paragraph's effective style (a paragraph with no explicit
    ``pStyle`` reports the document's default style), so runs are bucketed by their
    real style id/name.
    """
    per_style_font: dict[tuple[Optional[str], Optional[str]], Counter] = {}
    per_style_size: dict[tuple[Optional[str], Optional[str]], Counter] = {}
    per_style_color: dict[tuple[Optional[str], Optional[str]], Counter] = {}
    overall_font: Counter = Counter()
    overall_size: Counter = Counter()
    overall_color: Counter = Counter()

    for para in doc.paragraphs:
        try:
            style = para.style
            sid = getattr(style, "style_id", None) if style is not None else None
            sname = getattr(style, "name", None) if style is not None else None
        except Exception:
            sid = sname = None
        for run in para.runs:
            if not (run.text or "").strip():
                continue
            # Every run votes on every axis: an explicit value, or ``None`` meaning
            # "inherits this axis". The None votes count in the denominator so a value
            # is captured only when it dominates ALL runs (see _dominant) - e.g. an
            # accent color on a few runs never becomes the body color when most runs
            # inherit their color.
            font = run.font.name or None  # explicit ascii/hAnsi typeface, else None
            size_hp = _run_size_hp(run)
            color = _run_color(run)
            overall_font[font] += 1
            overall_size[size_hp] += 1
            overall_color[color] += 1
            if sid or sname:
                key = (sid, sname)
                per_style_font.setdefault(key, Counter())[font] += 1
                per_style_size.setdefault(key, Counter())[size_hp] += 1
                per_style_color.setdefault(key, Counter())[color] += 1

    body_font = _dominant(overall_font)
    body_size = _dominant(overall_size)
    if body_font is not None or body_size is not None:
        fonts = theme.setdefault("fonts", {})
        body = fonts.setdefault("body", {})
        if body_font is not None:
            body["latin"] = body_font[0]
            body["confidence"] = round(body_font[1], 3)
        if body_size is not None:
            body["size_hp"] = int(body_size[0])
            body["size_confidence"] = round(body_size[1], 3)
    body_color = _dominant(overall_color)
    if body_color is not None:
        text = theme.setdefault("text", {}).setdefault("body", {})
        text["color"] = _color_obj(body_color[0])
        text["color_confidence"] = round(body_color[1], 3)

    for rid, entry in roles.items():
        if rid == "_index" or not isinstance(entry, dict):
            continue
        resolver = entry.get("resolver") or {}
        if resolver.get("type") != schema.ResolverType.NAMED_STYLE.value:
            continue
        sid = resolver.get("style_id")
        sname = resolver.get("style_name")

        def _role_counter(per_style: dict) -> Counter:
            counter: Counter = Counter()
            for (k_sid, k_sname), c in per_style.items():
                if (sid and k_sid == sid) or (sname and k_sname == sname):
                    counter.update(c)
            return counter

        dom_font = _dominant(_role_counter(per_style_font))
        dom_size = _dominant(_role_counter(per_style_size))
        dom_color = _dominant(_role_counter(per_style_color))
        if dom_font is None and dom_size is None and dom_color is None:
            continue
        appearance = entry.setdefault("appearance", {})
        if dom_font is not None:
            appearance["font"] = {"latin": dom_font[0]}
            appearance["confidence"] = round(dom_font[1], 3)
        if dom_size is not None:
            appearance["size_hp"] = int(dom_size[0])
            appearance["size_confidence"] = round(dom_size[1], 3)
        if dom_color is not None:
            appearance["color"] = _color_obj(dom_color[0])
            appearance["color_confidence"] = round(dom_color[1], 3)


# ---------------------------------------------------------------------------
# theme.palette capture (model-free; the UNDERSTAND half of model-driven color)
# ---------------------------------------------------------------------------
def _palette_key(bucket: tuple[str, ...]) -> str:
    """The TEMPLATE-DERIVED palette key for a captured color bucket.

    A theme bucket keys by its WML theme token (``accent1`` / ``text1`` / ...);
    an off-theme RGB bucket keys by ``hex:RRGGBB``. The key is the stable id the
    comprehension annotates and the resolver/QA look up - never a brand name.
    """
    if bucket[0] == "hex":
        return f"hex:{bucket[1]}"
    return bucket[1]


def _iter_para_runs(para):
    """Yield every run in ``para``: its direct ``w:r`` runs AND the runs nested under
    its ``w:hyperlink`` elements.

    python-docx's ``para.runs`` exposes only the direct ``w:r`` children, so a link
    run (nested under ``w:hyperlink``) is otherwise invisible to capture. Newer
    python-docx (>= 1.x) surfaces ``para.hyperlinks[*].runs``; this widens the pass
    to include them, crash-safe (a degraded reader yields the direct runs only).
    """
    for run in para.runs:
        yield run
    try:
        for hyperlink in para.hyperlinks:
            for run in hyperlink.runs:
                yield run
    except Exception:
        # An older python-docx without ``para.hyperlinks`` simply contributes no
        # nested link runs - capture must never crash on a missing attribute.
        return


def _is_link_run(run) -> bool:
    """True if ``run`` is hyperlink text: nested under a ``w:hyperlink`` ancestor OR
    carrying a ``Hyperlink``/``FollowedHyperlink`` ``w:rStyle``.

    Wrapped fully crash-safe by the single caller; reads only structural OOXML
    (no brand literals). A run python-docx cannot introspect contributes nothing.
    """
    try:
        rpr = run._r.find(_W("rPr"))
        if rpr is not None:
            rstyle = rpr.find(_W("rStyle"))
            if rstyle is not None and rstyle.get(_W("val")) in _HYPERLINK_RSTYLES:
                return True
        node = run._r.getparent()
        while node is not None:
            if names.local_name(node.tag) == "hyperlink":
                return True
            node = node.getparent()
    except Exception:
        return False
    return False


def _add_provenance(entry: dict, where: str, detail: str) -> None:
    """Record one observed ``{where, detail}`` provenance fact on a palette entry,
    de-duplicated and kept sorted by ``(where, detail)`` (deterministic).

    ``where`` must be in the closed :data:`PALETTE_WHERE` vocabulary; an unknown
    ``where`` is dropped (capture only records observed facts in the frozen set).
    """
    if where not in PALETTE_WHERE:
        return
    provenance = entry.setdefault("provenance", [])
    fact = {"where": where, "detail": detail}
    if fact in provenance:
        return
    provenance.append(fact)
    provenance.sort(key=lambda p: (p["where"], p["detail"]))


def _palette_entry(palette: dict, bucket: tuple[str, ...]) -> dict:
    """Get-or-create the palette entry for a color bucket, keyed template-derived.

    A new entry carries the byte-identical :func:`_color_obj` ref, an empty
    provenance, a placeholder frequency (set by the caller), and the three
    model-only fields (``name`` / ``purpose`` / ``use_when``) explicitly ``null``
    in the deterministic path - ``comprehend`` is the only writer that fills them.
    """
    key = _palette_key(bucket)
    entry = palette.get(key)
    if entry is None:
        entry = {
            "ref": _color_obj(bucket),
            "provenance": [],
            "frequency": "rare",
            "name": None,
            "purpose": None,
            "use_when": None,
        }
        palette[key] = entry
    return entry


def capture_palette(doc, roles: dict, theme: dict) -> None:
    """Capture the template's brand PALETTE into ``theme['palette']`` (mutated in
    place), additively and deterministically.

    The palette is a map keyed by a TEMPLATE-DERIVED id - a theme slot token
    (``accent1`` / ``text1`` / ...) for a theme color, or ``hex:RRGGBB`` for an
    observed off-theme run color. Each entry carries:

      - ``ref``: the byte-identical :func:`_color_obj` (``{kind:theme,theme}`` |
        ``{kind:hex,hex}``);
      - ``provenance``: a list of observed ``{where, detail}`` facts from the
        closed :data:`PALETTE_WHERE` vocabulary, sorted ``(where, detail)``;
      - ``frequency``: a COARSE bucket (``dominant`` | ``accent`` | ``rare``),
        never raw counts;
      - ``name`` / ``purpose`` / ``use_when``: ``null`` in this deterministic path
        (``comprehend`` is the only writer that fills them).

    Provenance is built ONLY from observed facts:
      (a) the theme-color slots the template actually carries (seed theme-keyed
          entries; existence, not a where-fact);
      (b) explicit ``w:color`` on runs (a SINGLE pass via :func:`_run_color`),
          INCLUDING a low-floor accent aggregation - a color on at least
          :data:`_MIN_ACCENT_RUNS` runs that is NOT the document-dominant body
          color is an ``accent`` entry (no dominance gate, accents are sparse);
      (c) the per-role ``appearance.color`` already captured (``role.appearance``);
      (d) link-run colors (runs under a ``w:hyperlink`` ancestor / ``Hyperlink``
          style), wrapped crash-safe, falling back to the theme ``hlink`` /
          ``folHlink`` slot when no explicit link color is observed (``link.color``).

    The hardcoded, template-INVARIANT ``theme.palette_roles`` map is NOT trusted as
    brand evidence; it is recorded only as a non-authoritative ``palette_role``
    where-entry on the slot it names. Deterministic and byte-identical on
    re-extract; a template with no observed color leaves an empty ``{}`` palette.
    """
    palette: dict = theme.setdefault("palette", {})

    # (b) Observed w:color on runs, in a SINGLE pass. ``overall_color`` votes on
    # EVERY run (a ``None`` key for a run that inherits its color) so the dominance
    # gate is over ALL runs - identical to capture_fonts; ``run_color_counts`` keys
    # only the explicit-color buckets (the accent floor / provenance). ``link_buckets``
    # tracks which buckets were seen on a link run (source d).
    overall_color: Counter = Counter()
    run_color_counts: Counter = Counter()
    link_buckets: set[tuple[str, ...]] = set()
    for para in doc.paragraphs:
        for run in _iter_para_runs(para):
            if not (run.text or "").strip():
                continue
            bucket = _run_color(run)
            overall_color[bucket] += 1
            if bucket is None:
                continue
            run_color_counts[bucket] += 1
            if _is_link_run(run):
                link_buckets.add(bucket)

    # The single document-dominant body color (if any) gets the ``dominant`` coarse
    # bucket; everything else observed on runs is ``accent`` (>= floor) or ``rare``.
    dominant_color = _dominant(overall_color)
    dominant_bucket = dominant_color[0] if dominant_color is not None else None

    # (a) Seed theme-keyed entries for every slot the template's theme carries. This
    # is existence (so the slot has a stable palette key), not a where-fact.
    for slot in theme.get("colors") or {}:
        if slot in _THEME_SLOTS:
            _palette_entry(palette, ("theme", slot))

    # (b) record each observed run color, with its coarse frequency.
    for bucket, count in run_color_counts.items():
        entry = _palette_entry(palette, bucket)
        if bucket == dominant_bucket:
            entry["frequency"] = "dominant"
        elif count >= _MIN_ACCENT_RUNS:
            entry["frequency"] = "accent"
        else:
            entry["frequency"] = "rare"
        _add_provenance(entry, "run.color", _palette_key(bucket))

    # (d) link-color where-facts for buckets observed on link runs; plus a fallback
    # to the theme hlink/folHlink slots when the template declares them, so the link
    # palette is non-empty even when no run carries an explicit link color.
    for bucket in link_buckets:
        _add_provenance(
            _palette_entry(palette, bucket), "link.color", _palette_key(bucket)
        )
    for slot in ("hlink", "folHlink"):
        if slot in (theme.get("colors") or {}):
            entry = _palette_entry(palette, ("theme", slot))
            _add_provenance(entry, "link.color", slot)

    # (c) per-role appearance.color already captured (role.appearance where-fact).
    for rid, role_entry in roles.items():
        if rid == "_index" or not isinstance(role_entry, dict):
            continue
        color = (role_entry.get("appearance") or {}).get("color")
        if not isinstance(color, dict):
            continue
        bucket = _color_obj_to_bucket(color)
        if bucket is None:
            continue
        _add_provenance(_palette_entry(palette, bucket), "role.appearance", rid)

    # palette_role: the hardcoded, template-INVARIANT map - recorded NON-
    # authoritatively (it is not brand evidence), only on the slot it names.
    for prole, ref in (theme.get("palette_roles") or {}).items():
        slot = ref.get("theme") if isinstance(ref, dict) else None
        if slot and slot in _THEME_SLOTS:
            _add_provenance(
                _palette_entry(palette, ("theme", slot)), "palette_role", prole
            )


def _color_obj_to_bucket(color: dict) -> Optional[tuple[str, ...]]:
    """Invert :func:`_color_obj`: a stored color object -> its bucket key, or None.

    Used to fold an already-captured ``role.appearance.color`` (which is a
    ``_color_obj``) back into a palette bucket without re-reading the run.
    """
    kind = color.get("kind")
    if kind == "hex" and color.get("hex"):
        return ("hex", str(color["hex"]))
    if kind == "theme" and color.get("theme"):
        return ("theme", str(color["theme"]))
    return None
