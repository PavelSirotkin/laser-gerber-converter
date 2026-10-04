"""Преобразование геометрии и траекторий в пути QPainterPath для сцены (ось Y сцены направлена вниз)."""

from PyQt6 import QtCore, QtGui


def _ring_to_qpolygon(coords):
    """Кольцо Shapely -> QPolygonF (ось Y переворачивается: в сцене Qt Y направлен вниз)"""
    return QtGui.QPolygonF([QtCore.QPointF(x, -y) for x, y, *_ in coords])


def shapely_to_qt_paths(geom):
    """Shapely-геометрия в координатах станка -> (путь заливки полигонов, путь линий) для сцены Qt"""
    fill_path = QtGui.QPainterPath()
    fill_path.setFillRule(QtCore.Qt.FillRule.OddEvenFill)
    line_path = QtGui.QPainterPath()

    def walk(g):
        if g.is_empty:
            return
        if g.geom_type == "Polygon":
            # После unary_union дырки — настоящие interiors, поэтому OddEven здесь корректен
            fill_path.addPolygon(_ring_to_qpolygon(g.exterior.coords))
            for interior in g.interiors:
                fill_path.addPolygon(_ring_to_qpolygon(interior.coords))
        elif g.geom_type in ("LineString", "LinearRing"):
            line_path.addPolygon(_ring_to_qpolygon(g.coords))
        elif hasattr(g, "geoms"):
            for sub in g.geoms:
                walk(sub)

    walk(geom)
    return fill_path, line_path


def toolpath_to_qt_paths(toolpath):
    """Траектория -> (путь прожига, путь холостых ходов/overscan) для сцены Qt"""
    burn_path = QtGui.QPainterPath()
    for y, x0, x1 in toolpath.burn_segments:
        burn_path.moveTo(x0, -y)
        burn_path.lineTo(x1, -y)

    travel_path = QtGui.QPainterPath()
    for y, x0, x1 in toolpath.travel_segments:
        travel_path.moveTo(x0, -y)
        travel_path.lineTo(x1, -y)
    return burn_path, travel_path
