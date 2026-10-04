"""Геометрия платы: трансформации в координаты станка и растровое сканирование. Без зависимостей от Qt."""

import numpy as np
from shapely.affinity import affine_transform, rotate, scale, translate
from shapely.geometry import LineString, Point, box
from shapely.ops import unary_union


class GerberGeometryContext:
    """Класс-контейнер для инкапсуляции всей геометрии печатной платы.
    Управляет трансформациями: смещением камеры, оверсканом, углами, зеркалами
    и аффинным сопоставлением по реперным точкам станка.
    """

    def __init__(self, raw_geometries):
        self.raw_geometries = [g for g in raw_geometries if not g.is_empty]

        # Настройки ручной трансформации из UI-крутилок
        self.rotate_angle = 0.0
        self.flip_x = False
        self.flip_y = False

        # Коэффициенты аффинного базирования по точкам станка
        self.matrix_coeffs = None
        self.use_calibration = False

    def get_raw_bounds(self):
        """Возвращает чистые габариты исходного файла (xmin, ymin, xmax, ymax)"""
        if not self.raw_geometries:
            return 0.0, 0.0, 0.0, 0.0
        bounds = [g.bounds for g in self.raw_geometries]
        return (
            min(b[0] for b in bounds),
            min(b[1] for b in bounds),
            max(b[2] for b in bounds),
            max(b[3] for b in bounds),
        )

    def get_burn_geometry(self, invert=False):
        """Итоговая геометрия прожига в координатах станка — единый источник для превью и G-кода.
        Возвращает (геометрия, (xmin, ymin, xmax, ymax)) или (None, None)."""
        elements = self.get_transformed_elements()
        if not elements:
            return None, None
        merged = unary_union(elements)
        if invert:
            # Маска — контур платы минус медь. Контур трансформируется вместе с платой: на повернутой
            # плате это повернутый прямоугольник, а не прямоугольник по осям станка (его углы выходили
            # за плату, и лазер жег мимо нее)
            outline = self.board_outline()
            return outline.difference(merged), outline.bounds
        return merged, merged.bounds

    def board_outline(self):
        """Контур платы (габаритный прямоугольник Gerber-файла) в координатах экрана/станка"""
        _, _, rot_xmin, rot_ymin = self._local_frame()
        raw_xmin, raw_ymin, raw_xmax, raw_ymax = self.get_raw_bounds()
        center = (raw_xmin + (raw_xmax - raw_xmin) / 2.0, raw_ymin + (raw_ymax - raw_ymin) / 2.0)
        outline = self._flip_rotate(box(raw_xmin, raw_ymin, raw_xmax, raw_ymax), center)
        return self._place(outline, rot_xmin, rot_ymin)

    def local_to_display(self, x, y):
        """Точка в локальных координатах платы (после зеркал/поворота, прижата к 0,0) -> координаты экрана/станка"""
        if self.use_calibration and self.matrix_coeffs:
            m11, m21, m12, m22, dx, dy = self.matrix_coeffs
            return m11 * x + m21 * y + dx, m12 * x + m22 * y + dy
        return x, y

    def display_to_local(self, x, y):
        """Обратное преобразование: координаты экрана/станка -> локальные координаты платы"""
        if self.use_calibration and self.matrix_coeffs:
            m11, m21, m12, m22, dx, dy = self.matrix_coeffs
            det = m11 * m22 - m21 * m12
            if abs(det) < 1e-12:
                raise ValueError("Матрица калибровки вырождена — точки лежат на одной прямой?")
            px, py = x - dx, y - dy
            return (m22 * px - m21 * py) / det, (-m12 * px + m11 * py) / det
        return x, y

    def _flip_rotate(self, geom, center):
        """Ручные трансформации интерфейса: сначала зеркала, потом поворот вокруг центра файла"""
        if self.flip_x or self.flip_y:
            fx = -1.0 if self.flip_x else 1.0
            fy = -1.0 if self.flip_y else 1.0
            geom = scale(geom, xfact=fx, yfact=fy, origin=center)
        if self.rotate_angle != 0.0:
            geom = rotate(geom, self.rotate_angle, origin=center)
        return geom

    def _local_frame(self):
        """(центр файла, геометрии после зеркал/поворота, xmin, ymin их габаритов)"""
        raw_xmin, raw_ymin, raw_xmax, raw_ymax = self.get_raw_bounds()
        geom_center = (raw_xmin + (raw_xmax - raw_xmin) / 2.0, raw_ymin + (raw_ymax - raw_ymin) / 2.0)

        # Шаг А: Сначала ВСЕГДА применяем базовые ручные трансформации интерфейса (Зеркала и Поворот)
        # Это нужно и для обычного режима, и для калибровки по точкам
        temp_geoms = [self._flip_rotate(geom, geom_center) for geom in self.raw_geometries]

        # Находим новые минимальные границы повернутого облака векторов платы
        all_bounds = [g.bounds for g in temp_geoms]
        rot_xmin = min(b[0] for b in all_bounds)
        rot_ymin = min(b[1] for b in all_bounds)
        return geom_center, temp_geoms, rot_xmin, rot_ymin

    def raw_to_local(self, x, y):
        """Точка в координатах Gerber-файла -> локальные координаты платы (после зеркал/поворота, прижата к 0,0)"""
        geom_center, _, rot_xmin, rot_ymin = self._local_frame()
        p = translate(self._flip_rotate(Point(x, y), geom_center), xoff=-rot_xmin, yoff=-rot_ymin)
        return p.x, p.y

    def get_transformed_elements(self):
        """Возвращает массив геометрий со всеми примененными смещениями."""
        if not self.raw_geometries:
            return []

        _, temp_geoms, rot_xmin, rot_ymin = self._local_frame()

        # Шаг Б: Применяем позиционирование станка / камеры
        return [self._place(geom, rot_xmin, rot_ymin) for geom in temp_geoms]

    def _place(self, geom, rot_xmin, rot_ymin):
        """Геометрия после зеркал/поворота -> координаты экрана/станка"""
        # Сдвигаем повернутую плату к локальному нулю (0,0) ее новых повернутых габаритов
        geom = translate(geom, xoff=-rot_xmin, yoff=-rot_ymin)

        if self.use_calibration and self.matrix_coeffs:
            # Привязка к станку по реперным точкам
            return affine_transform(geom, self.matrix_coeffs)
        # Без привязки плата прижата к нулю станка. Смещение камеры здесь не участвует:
        # оно учитывается только у точек, наведенных камерой
        return geom


def _collect_edges(geom):
    """Все ребра полигонов (x1, y1, x2, y2) и прочие (неплощадные) части геометрии"""
    edges, others = [], []

    def walk(g):
        if g.is_empty:
            return
        if g.geom_type == "Polygon":
            for ring in (g.exterior, *g.interiors):
                c = np.asarray(ring.coords, dtype=float)[:, :2]
                edges.append(np.hstack([c[:-1], c[1:]]))
        elif hasattr(g, "geoms"):
            for sub in g.geoms:
                walk(sub)
        else:
            others.append(g)

    walk(geom)
    edges = np.vstack(edges) if edges else np.zeros((0, 4))
    return edges, others


def scanline_intervals(geom, ys, x_from, x_to):
    """Отрезки прожига [(x_start, x_end), ...] для каждой строки Y.
    Пересечения строки с ребрами полигонов считаются векторно (правило even-odd — корректно,
    т.к. после unary_union дырки являются настоящими interiors). Линии/точки нулевой ширины
    обрабатываются через shapely, как раньше."""
    edges, others = _collect_edges(geom)
    x1, y1, x2, y2 = edges.T if len(edges) else (np.zeros(0),) * 4
    lo, hi = np.minimum(y1, y2), np.maximum(y1, y2)
    order = np.argsort(lo)
    x1, y1, x2, y2, lo, hi = (a[order] for a in (x1, y1, x2, y2, lo, hi))

    def crossings(y):
        n = np.searchsorted(lo, y, side="right")  # ребра, начинающиеся не выше строки
        sel = hi[:n] > y  # полуоткрытый интервал [lo, hi) — вершины не считаются дважды
        if not sel.any():
            return []
        ex1, ey1, ex2, ey2 = x1[:n][sel], y1[:n][sel], x2[:n][sel], y2[:n][sel]
        xs = np.sort(ex1 + (y - ey1) * (ex2 - ex1) / (ey2 - ey1))
        return [(float(a), float(b)) for a, b in zip(xs[0::2], xs[1::2], strict=False) if b > a]

    # Строка может пройти ровно по горизонтальному ребру (координаты Gerber часто на той же сетке).
    # Граница считается частью фигуры, как в shapely: объединяем срезы чуть ниже и чуть выше строки.
    eps = 1e-7
    result = []
    for y in ys:
        segs = crossings(y - eps) + crossings(y + eps)

        if others:
            scan_line = LineString([(x_from, y), (x_to, y)])
            for g in others:
                hit = scan_line.intersection(g)
                for part in getattr(hit, "geoms", [hit]):
                    if part.is_empty:
                        continue
                    if part.geom_type == "Point":
                        segs.append((part.x - 0.005, part.x + 0.005))
                    elif part.geom_type == "LineString":
                        xa, xb = part.coords[0][0], part.coords[-1][0]
                        segs.append((min(xa, xb), max(xa, xb)))

        # Слияние перекрывающихся отрезков
        segs.sort()
        merged = []
        for a, b in segs:
            if merged and a <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], b))
            else:
                merged.append((a, b))
        result.append(merged)
    return result
