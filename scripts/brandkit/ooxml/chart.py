# SPDX-License-Identifier: MIT
"""Build a DrawingML chart part (``c:chartSpace``) from IR chart data.

The format generators that author a NATIVE chart share this builder. The chart is
emitted with INLINE cached data (``c:numCache`` / ``c:strCache``) and NO embedded
workbook, so it is fully deterministic (byte-idempotent) and renders from the cache
in Word and LibreOffice; "Edit Data in Excel" is unavailable - the documented
trade-off for a generated, read-only chart. No literal colors are written, so the
chart inherits the document theme's accent colors: on-brand by construction.

``coerce_series`` / ``has_plottable_data`` are the single, format-agnostic data
gate both the docx writer and the pptx writer use, so the gate and the render can
never disagree about whether a chart has anything to plot.
"""

from __future__ import annotations

import math
from xml.sax.saxutils import escape

_C = "http://schemas.openxmlformats.org/drawingml/2006/chart"
_A = "http://schemas.openxmlformats.org/drawingml/2006/main"
_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"

# IR ``chart_type`` -> how this builder realizes it. ``bar`` is the common business
# vertical bar (a COLUMN chart); ``barh`` is the true horizontal bar. Pie/doughnut
# plot a SINGLE series. An unknown type falls back to a clustered column chart - the
# caller surfaces that as an INFO (see ``is_known_chart_type``), never silently.
_BAR_DIR = {"bar": "col", "column": "col", "barh": "bar"}
_LINE = {"line", "line_markers"}
_AREA = {"area"}
_PIE = {"pie": "pieChart", "doughnut": "doughnutChart"}
_KNOWN = set(_BAR_DIR) | _LINE | _AREA | set(_PIE)

_CAT_AX_ID = "111111111"
_VAL_AX_ID = "222222222"


def is_known_chart_type(chart_type: str | None) -> bool:
    """True when ``chart_type`` maps to a real chart kind (else the builder falls
    back to a clustered column chart and the caller should surface an INFO)."""
    return (chart_type or "").lower() in _KNOWN


def is_single_series_type(chart_type: str | None) -> bool:
    """True for pie/doughnut, which render only ONE series; authoring more is data
    loss the caller should surface as a WARNING (the builder plots the first)."""
    return (chart_type or "").lower() in _PIE


def _num(value):
    """Coerce one series value to a FINITE float; anything else is a gap (``None``).

    A non-numeric value, ``inf``/``-inf`` or ``nan`` all become ``None`` - a chart
    cannot plot a non-finite point, and ``int(inf)``/``int(nan)`` would otherwise
    crash the integer-cleanup in :func:`_fmt_num`."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def coerce_series(chart) -> list[tuple[str, list]]:
    """Return ``(name, values)`` for each series with at least one PLOTTABLE value.

    Values are coerced to finite float (anything else -> a gap ``None``). A series
    whose values are ALL non-plottable is dropped, so a chart never carries a
    phantom empty series that looks like data. Each surviving series is normalized
    to EXACTLY ``len(categories)`` points (short series padded with gaps, long ones
    truncated) so the value ``ptCount`` always matches the category axis - otherwise
    Word silently drops the trailing categories.
    """
    cat_count = len(chart.categories or [])
    out: list[tuple[str, list]] = []
    for series in chart.series or []:
        if not isinstance(series, dict):
            continue
        values = series.get("values")
        if not values:
            continue
        nums = [_num(v) for v in values]
        if not any(v is not None for v in nums):
            continue
        if cat_count:
            nums = (nums + [None] * cat_count)[:cat_count]
        out.append((str(series.get("name") or ""), nums))
    return out


def has_plottable_data(chart) -> bool:
    """True when the chart has a category axis AND at least one plottable series -
    exactly what :func:`build_chart_xml` can render."""
    return bool(chart.categories) and bool(coerce_series(chart))


def _fmt_num(value) -> str:
    """Render a FINITE float as a canonical OOXML number: a whole number with no
    trailing ``.0``, otherwise a ``%.12g`` form that drops IEEE-754 noise (e.g.
    ``0.1 + 0.2`` -> ``0.3``, not ``0.30000000000000004``) and trailing zeros.
    Callers pass only finite values (``coerce_series`` mapped inf/nan to gaps)."""
    if value == int(value):
        return str(int(value))
    return f"{value:.12g}"


def _str_pts(values: list[str]) -> str:
    return "".join(
        f'<c:pt idx="{i}"><c:v>{escape(str(v))}</c:v></c:pt>'
        for i, v in enumerate(values)
    )


def _num_pts(values: list) -> str:
    # Omit a gap (None) point so the chart shows a hole rather than a zero.
    return "".join(
        f'<c:pt idx="{i}"><c:v>{_fmt_num(v)}</c:v></c:pt>'
        for i, v in enumerate(values)
        if v is not None
    )


def _str_ref(formula: str, values: list[str]) -> str:
    return (
        f"<c:strRef><c:f>{escape(formula)}</c:f><c:strCache>"
        f'<c:ptCount val="{len(values)}"/>{_str_pts(values)}'
        f"</c:strCache></c:strRef>"
    )


def _num_ref(formula: str, values: list) -> str:
    # ``ptCount`` is the TOTAL number of slots (including gaps); ``_num_pts`` emits a
    # ``c:pt`` only for the non-gap positions and keys each by its original ``idx``,
    # so a gap is a missing index that ``dispBlanksAs="gap"`` renders as a hole. This
    # sparse, idx-keyed form is the OOXML-standard way to represent gaps.
    return (
        f"<c:numRef><c:f>{escape(formula)}</c:f><c:numCache>"
        f"<c:formatCode>General</c:formatCode>"
        f'<c:ptCount val="{len(values)}"/>{_num_pts(values)}'
        f"</c:numCache></c:numRef>"
    )


def _col_letter(idx: int) -> str:
    return chr(ord("B") + idx)


def _ser(idx: int, name: str, cats: list[str], vals: list) -> str:
    """One ``c:ser`` (idx/order/tx/cat/val) - valid for bar/line/area/pie/doughnut.

    The ``c:f`` formulas reference a phantom ``Sheet1`` (there is no embedded
    workbook); the caches are what render. No ``c:spPr`` is emitted, so the series
    inherits the theme accent color."""
    col = _col_letter(idx)
    last_row = len(cats) + 1
    return (
        f'<c:ser><c:idx val="{idx}"/><c:order val="{idx}"/>'
        f"<c:tx>{_str_ref(f'Sheet1!${col}$1', [name])}</c:tx>"
        f"<c:cat>{_str_ref(f'Sheet1!$A$2:$A${last_row}', cats)}</c:cat>"
        f"<c:val>{_num_ref(f'Sheet1!${col}$2:${col}${last_row}', vals)}</c:val>"
        f"</c:ser>"
    )


def _title_xml(title: str | None) -> str:
    if not title:
        return '<c:autoTitleDeleted val="1"/>'
    return (
        f"<c:title><c:tx><c:rich><a:bodyPr/><a:lstStyle/><a:p><a:r>"
        f"<a:t>{escape(title)}</a:t></a:r></a:p></c:rich></c:tx>"
        f'<c:overlay val="0"/></c:title><c:autoTitleDeleted val="0"/>'
    )


def _plot_body(chart_type: str, cats: list[str], series: list[tuple[str, list]]) -> str:
    """The chart-kind element (barChart/lineChart/areaChart/pieChart/doughnutChart)
    plus, for the axis kinds, the category and value axes."""
    kind = (chart_type or "").lower()
    if kind in _PIE:  # pie/doughnut: single series, no axes, vary slice colors
        name, vals = series[0]
        ring = '<c:holeSize val="50"/>' if kind == "doughnut" else ""
        return (
            f'<c:{_PIE[kind]}><c:varyColors val="1"/>'
            f'{_ser(0, name, cats, vals)}<c:firstSliceAng val="0"/>{ring}'
            f"</c:{_PIE[kind]}>"
        )

    sers = "".join(_ser(i, n, cats, v) for i, (n, v) in enumerate(series))
    # Tick-mark / label elements are emitted EXPLICITLY (in their ECMA-376 sequence
    # position, after axPos and before crossAx): omitting them is valid but lets Word
    # inject its own version-/locale-dependent defaults on save, which would break
    # the byte-idempotency the cache otherwise guarantees.
    _ticks = (
        '<c:majorTickMark val="out"/><c:minorTickMark val="none"/>'
        '<c:tickLblPos val="nextTo"/>'
    )
    axes = (
        f'<c:catAx><c:axId val="{_CAT_AX_ID}"/>'
        f'<c:scaling><c:orientation val="minMax"/></c:scaling>'
        f'<c:delete val="0"/><c:axPos val="b"/>{_ticks}'
        f'<c:crossAx val="{_VAL_AX_ID}"/></c:catAx>'
        f'<c:valAx><c:axId val="{_VAL_AX_ID}"/>'
        f'<c:scaling><c:orientation val="minMax"/></c:scaling>'
        f'<c:delete val="0"/><c:axPos val="l"/>{_ticks}'
        f'<c:crossAx val="{_CAT_AX_ID}"/></c:valAx>'
    )
    ax_ids = f'<c:axId val="{_CAT_AX_ID}"/><c:axId val="{_VAL_AX_ID}"/>'
    if kind in _LINE:
        body = (
            f'<c:lineChart><c:grouping val="standard"/><c:varyColors val="0"/>'
            f'{sers}<c:marker val="1"/>{ax_ids}</c:lineChart>'
        )
    elif kind in _AREA:
        body = (
            f'<c:areaChart><c:grouping val="standard"/><c:varyColors val="0"/>'
            f"{sers}{ax_ids}</c:areaChart>"
        )
    else:  # bar/column/barh + the unknown-type fallback (clustered column)
        bar_dir = _BAR_DIR.get(kind, "col")
        body = (
            f'<c:barChart><c:barDir val="{bar_dir}"/>'
            f'<c:grouping val="clustered"/><c:varyColors val="0"/>'
            f'{sers}<c:gapWidth val="150"/>{ax_ids}</c:barChart>'
        )
    return body + axes


def build_chart_xml(
    chart_type: str | None,
    series: list[tuple[str, list]],
    categories: list[str],
    title: str | None,
) -> bytes:
    """Return the ``c:chartSpace`` part bytes for an inline-data chart.

    ``series`` is the coerced ``[(name, [float|None, ...]), ...]`` from
    :func:`coerce_series`; ``categories`` the category labels. The caller is
    responsible for having validated non-empty data (:func:`has_plottable_data`).
    """
    cats = [str(c) for c in categories]
    plot = _plot_body(chart_type, cats, series)
    legend = '<c:legend><c:legendPos val="b"/></c:legend>'
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<c:chartSpace xmlns:c="{_C}" xmlns:a="{_A}" xmlns:r="{_R}">'
        f"<c:chart>{_title_xml(title)}<c:plotArea><c:layout/>{plot}</c:plotArea>"
        f'{legend}<c:plotVisOnly val="1"/><c:dispBlanksAs val="gap"/>'
        "</c:chart></c:chartSpace>"
    ).encode("utf-8")
