import hashlib
import json
import os
import re

import pytest
from conftest import CONFIGS, SAMPLE_FILES, config, make_context, make_params
from shapely.geometry import box

from core.gcode import GcodeParams, generate_gcode

GOLDEN = json.load(open(os.path.join(os.path.dirname(__file__), "data", "golden_gcode.json"), encoding="utf-8"))


def build(sample, name):
    cfg = config(name)
    burn_geom, bounds = make_context(sample, cfg).get_burn_geometry(invert=cfg["invert"])
    return generate_gcode(burn_geom, bounds, make_params(cfg)), bounds


@pytest.mark.parametrize("sample", SAMPLE_FILES)
@pytest.mark.parametrize("name", list(CONFIGS))
def test_gcode_matches_golden(sample, name):
    """G-код побайтно совпадает с эталоном. Если поведение меняется намеренно — эталон нужно перегенерировать"""
    toolpath, _ = build(sample, name)
    assert hashlib.sha256(toolpath.gcode.encode()).hexdigest() == GOLDEN[f"{sample}|{name}"]


def test_no_rapid_moves():
    """G0 не используется: у него нет регулировки скорости, станок летит на максимуме"""
    toolpath, _ = build("test.gbr", "base")
    assert not re.search(r"^G0\b", toolpath.gcode, re.MULTILINE)


def test_contour_is_closed_rectangle_of_board():
    toolpath, (xmin, ymin, xmax, ymax) = build("test.gbr", "base")
    lines = toolpath.gcode.splitlines()
    start = lines.index("M3 S0;") + 1
    contour = [tuple(map(float, re.findall(r"[XY](-?[\d.]+)", ln))) for ln in lines[start : start + 5]]
    corners = [
        (round(xmin, 4), round(ymin, 4)),
        (round(xmax, 4), round(ymin, 4)),
        (round(xmax, 4), round(ymax, 4)),
        (round(xmin, 4), round(ymax, 4)),
        (round(xmin, 4), round(ymin, 4)),
    ]
    assert contour == corners


def _line_starts(gcode):
    return [float(m) for m in re.findall(r"^G1 X(-?[\d.]+) Y-?[\d.]+ S0$", gcode, re.MULTILINE)][
        :-1
    ]  # без возврата в ноль


def test_snake_alternates_direction_and_respects_overscan():
    geom = box(0, 0, 10, 1)
    p = GcodeParams(step=0.25, overscan=3.0, snake=True)
    starts = _line_starts(generate_gcode(geom, geom.bounds, p).gcode)
    assert starts == [-3.0, 13.0, -3.0, 13.0]


def test_without_snake_every_line_starts_left():
    geom = box(0, 0, 10, 1)
    p = GcodeParams(step=0.25, overscan=3.0, snake=False)
    starts = _line_starts(generate_gcode(geom, geom.bounds, p).gcode)
    assert starts == [-3.0] * 4


def test_burn_and_travel_segments_cover_scan_line():
    """Прожиг + холостые ходы покрывают каждую строку от -overscan до +overscan без дыр"""
    geom = box(0, 0, 4, 1).union(box(6, 0, 10, 1))
    p = GcodeParams(step=0.5, overscan=2.0)
    tp = generate_gcode(geom, geom.bounds, p)
    for y in {s[0] for s in tp.burn_segments}:
        parts = sorted([(min(a, b), max(a, b)) for yy, a, b in tp.burn_segments + tp.travel_segments if yy == y])
        assert parts[0][0] == -2.0 and parts[-1][1] == 12.0
        assert all(a[1] == b[0] for a, b in zip(parts, parts[1:], strict=False))
    assert sum(b - a for _, a, b in tp.burn_segments) == pytest.approx(8.0 * 2)


def test_contour_and_raster_feed_rates():
    """Контур идет со «Скоростью контура», растр — со «Скоростью гравировки» (F модальный в GRBL)"""
    geom = box(0, 0, 10, 1)
    gcode = generate_gcode(geom, geom.bounds, GcodeParams(feedrate=1500, contour_feed=600)).gcode
    feeds = [(i, int(m)) for i, ln in enumerate(gcode.splitlines()) for m in re.findall(r"F(\d+)", ln)]
    lines = gcode.splitlines()
    pause = lines.index("M0 ;")
    assert [f for i, f in feeds if i < pause] == [600]
    assert [f for i, f in feeds if i > pause] == [1500]
