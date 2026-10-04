import pytest
from conftest import SAMPLE_FILES, config, make_context
from shapely.geometry import LineString, Point, box

from core.geometry import GerberGeometryContext, scanline_intervals


def total(segs):
    return sum(b - a for a, b in segs)


def test_square_with_hole():
    geom = box(0, 0, 10, 10).difference(box(3, 3, 7, 7))
    assert scanline_intervals(geom, [5.0], -1, 11)[0] == pytest.approx([(0, 3), (7, 10)])
    assert scanline_intervals(geom, [1.0], -1, 11)[0] == pytest.approx([(0, 10)])


def test_line_on_horizontal_edge_is_burned():
    """Строка ровно по горизонтальной границе меди считается частью фигуры (как в shapely)"""
    geom = box(0, 0, 10, 2).union(box(4, 2, 6, 5))  # Т-образная фигура: ребро y=2 у верхней части
    assert scanline_intervals(geom, [2.0], -1, 11)[0] == pytest.approx([(0, 10)])
    assert scanline_intervals(geom, [5.0], -1, 11)[0] == pytest.approx([(4, 6)])


def test_line_outside_geometry_is_empty():
    assert scanline_intervals(box(0, 0, 1, 1), [2.0, -1.0], -1, 2) == [[], []]


def test_zero_width_parts_handled():
    geom = LineString([(1, 0), (1, 4)]).union(Point(5, 2))
    segs = scanline_intervals(geom, [2.0], 0, 10)[0]
    assert segs == pytest.approx([(0.995, 1.005), (4.995, 5.005)])


@pytest.mark.parametrize("sample", SAMPLE_FILES)
@pytest.mark.parametrize("y_frac", [0.13, 0.5, 0.77])
def test_scanline_matches_shapely_on_samples(sample, y_frac):
    geom, (xmin, ymin, xmax, ymax) = make_context(sample, config("base")).get_burn_geometry()
    y = ymin + (ymax - ymin) * y_frac + 1e-4  # не по ребру — тут shapely однозначен
    expected = LineString([(xmin - 1, y), (xmax + 1, y)]).intersection(geom).length
    # Срез берется на y ± 1e-7, на пологих ребрах это сдвигает X на микроны — допуск 0.1 мкм на ребро
    assert total(scanline_intervals(geom, [y], xmin - 1, xmax + 1)[0]) == pytest.approx(expected, abs=1e-4)


def test_invert_is_bbox_minus_copper():
    ctx = GerberGeometryContext([box(1, 1, 3, 3), box(5, 1, 6, 2)])
    copper, bounds = ctx.get_burn_geometry()
    mask, mask_bounds = ctx.get_burn_geometry(invert=True)
    assert mask_bounds == bounds
    assert mask.area == pytest.approx(box(*bounds).area - copper.area)


def test_flip_and_rotate_order():
    """Сначала зеркало, потом поворот (как в G-коде); плата прижата к 0,0"""
    ctx = GerberGeometryContext([box(0, 0, 4, 1)])  # «палка» вдоль X с маркером справа
    ctx.raw_geometries.append(box(3, 1, 4, 2))
    ctx.flip_x, ctx.rotate_angle = True, 90.0
    geom, bounds = ctx.get_burn_geometry()
    assert bounds == pytest.approx((0, 0, 2, 4))
    # После зеркала маркер слева; после поворота на +90° он внизу
    assert geom.intersects(box(0.1, 0.1, 0.9, 0.9))
    assert not geom.intersects(box(0.1, 3.1, 0.9, 3.9))
