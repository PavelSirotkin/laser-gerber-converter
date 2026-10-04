"""Виртуальный станок для проверки без железа.

Кладет плату на стол в скрытое «настоящее» положение (сдвиг, поворот, усадка, зеркало),
моделирует голову станка с камерой, смещенной относительно лазера, и «прожигает» G-код —
проверяет, легли ли линии на медь. Программа, которую проверяют, это положение не знает:
она узнает его только через калибровку, как на настоящем станке. Без зависимостей от Qt.
"""

import math
import random
import re
from dataclasses import dataclass, field

import shapely
from shapely.affinity import affine_transform
from shapely.geometry import MultiLineString, box
from shapely.ops import unary_union


@dataclass
class MachineConfig:
    field_w: float = 165.0  # рабочее поле станка по X, мм
    field_h: float = 95.0  # рабочее поле станка по Y, мм
    camera_offset: tuple = (42.9, 0.55)  # положение камеры относительно лазера (Лазер -> Камера), мм


@dataclass
class BurnReport:
    burn_segments: list = field(default_factory=list)  # [(x0, y0, x1, y1)] растр, лазер включен
    contour_segments: list = field(default_factory=list)  # [(x0, y0, x1, y1)] тестовый обход контура
    burn_length: float = 0.0  # длина растрового прожига, мм
    miss_length: float = 0.0  # длина прожига не туда (мимо меди / по меди в инверсии), мм
    max_miss: float = 0.0  # наибольший промах конца отрезка прожига, мм
    coverage: float = 0.0  # доля целевой площади, покрытая лучом (0..1)
    out_of_field: float = 0.0  # насколько траектория выходит за поле станка, мм (0 — не выходит)
    g0_count: int = 0  # число ускоренных перемещений G0
    off_board_length: float = 0.0  # длина прожига за пределами платы, мм
    off_board_max: float = 0.0  # наибольшее удаление прожига от края платы, мм
    tolerance: float = 0.05  # допуск — полшага растра, мм

    def verdict(self):
        """("ok" | "warning" | "error", текст вывода)"""
        if self.burn_length == 0:
            return "error", "Лазер ничего не прожег."
        if (
            self.max_miss >= self.tolerance
            or self.out_of_field > 1e-9
            or self.g0_count
            or self.off_board_length > 0.05 * self.burn_length
        ):
            return "error", "Есть проблемы — смотрите цифры."
        if self.off_board_max > self.tolerance:
            return "warning", "Прожиг лег на плату, но часть луча выходит за край платы."
        return "ok", "Прожиг лег на плату."

    def summary(self):
        lines = [
            f"Прожиг: {self.burn_length:.1f} мм, покрытие цели {self.coverage * 100:.1f} %",
            f"Мимо цели: {self.miss_length:.2f} мм, наибольший промах {self.max_miss:.3f} мм",
            f"Вне платы: {self.off_board_length:.1f} мм, до {self.off_board_max:.2f} мм от края",
            ("Выход за поле: нет" if self.out_of_field <= 1e-9 else f"Выход за поле: {self.out_of_field:.2f} мм !"),
        ]
        if self.g0_count:
            lines.append(f"G0 (без регулировки скорости): {self.g0_count} !")
        return "\n".join(lines)


_WORD = re.compile(r"([A-Z])\s*(-?\d*\.?\d+)")


def parse_gcode(gcode, start=(0.0, 0.0)):
    """Разбор G-кода GRBL в отрезки движения.

    Возвращает (moves, g0_count), где moves — [(x0, y0, x1, y1, laser_on, phase)],
    phase — "contour" до первой паузы M0 и "raster" после нее.
    Лазер жжет только на G1 при включенном M3/M4 и S > 0 (на G0 GRBL в лазерном режиме луч гасит).
    """
    x, y = start
    motion, spindle_on, s_value, phase = None, False, 0.0, "contour"
    moves, g0_count = [], 0
    for raw in gcode.splitlines():
        line = re.sub(r"\(.*?\)", "", raw.split(";")[0]).upper()
        words = _WORD.findall(line)
        if not words:
            continue
        nx, ny = x, y
        for letter, value in words:
            v = float(value)
            if letter == "G":
                if v in (0, 1):
                    motion = int(v)
                elif v in (2, 3):
                    raise ValueError("Дуги G2/G3 симулятор не поддерживает")
            elif letter == "M":
                if v in (3, 4):
                    spindle_on = True
                elif v in (5, 2, 30):
                    spindle_on = False
                elif v == 0:
                    phase = "raster"
            elif letter == "S":
                s_value = v
            elif letter == "X":
                nx = v
            elif letter == "Y":
                ny = v
        if (nx, ny) != (x, y):
            if motion is None:
                raise ValueError(f"Перемещение без G0/G1: {raw!r}")
            if motion == 0:
                g0_count += 1
            on = motion == 1 and spindle_on and s_value > 0
            moves.append((x, y, nx, ny, on, phase))
            x, y = nx, ny
    return moves, g0_count


class VirtualMachine:
    def __init__(
        self,
        raw_geometries,
        config=None,
        seed=None,
        max_rotation=5.0,
        max_scale_error=0.002,
        mirror_x=False,
        margin=5.0,
    ):
        """raw_geometries — геометрия Gerber-файла (мм); плата кладется на стол случайно, но целиком в поле"""
        self.config = config or MachineConfig()
        self.raw_geometries = [g for g in raw_geometries if not g.is_empty]
        self._rng = random.Random(seed)
        self.mirror_x = mirror_x
        self.board_to_table = self._random_pose(max_rotation, max_scale_error, margin)

        self.copper = affine_transform(unary_union(self.raw_geometries), self.board_to_table)
        raw_bounds = unary_union(self.raw_geometries).bounds
        self.board_outline = affine_transform(box(*raw_bounds), self.board_to_table)

        # Голова стоит в нуле станка
        self.laser_x, self.laser_y = 0.0, 0.0

    # --- скрытое положение платы ---

    def _random_pose(self, max_rotation, max_scale_error, margin):
        xmin, ymin, xmax, ymax = unary_union(self.raw_geometries).bounds
        cx, cy = (xmin + xmax) / 2.0, (ymin + ymax) / 2.0
        angle = math.radians(self._rng.uniform(-max_rotation, max_rotation))
        k = 1.0 + self._rng.uniform(-max_scale_error, max_scale_error)
        fx = -1.0 if self.mirror_x else 1.0
        c, s = math.cos(angle) * k, math.sin(angle) * k
        # Линейная часть: зеркало по X относительно центра, затем поворот с масштабом
        a, b, d, e = c * fx, -s, s * fx, c

        corners = [(px - cx, py - cy) for px, py in ((xmin, ymin), (xmax, ymin), (xmax, ymax), (xmin, ymax))]
        rx = [a * px + b * py for px, py in corners]
        ry = [d * px + e * py for px, py in corners]
        w, h = max(rx) - min(rx), max(ry) - min(ry)

        def place(size, field_size, lo):
            free = field_size - size - 2 * margin
            pos = margin + (self._rng.uniform(0, free) if free > 0 else free / 2.0)
            return pos - lo

        tx, ty = place(w, self.config.field_w, min(rx)), place(h, self.config.field_h, min(ry))
        # x' = a*(x-cx) + b*(y-cy) + tx  ->  коэффициенты shapely (a, b, d, e, xoff, yoff)
        return (a, b, d, e, tx - a * cx - b * cy, ty - d * cx - e * cy)

    def true_position(self, raw_x, raw_y):
        """Где на столе на самом деле лежит точка платы с координатами файла (raw_x, raw_y)"""
        a, b, d, e, xoff, yoff = self.board_to_table
        return a * raw_x + b * raw_y + xoff, d * raw_x + e * raw_y + yoff

    # --- голова станка ---

    @property
    def camera_position(self):
        ox, oy = self.config.camera_offset
        return self.laser_x + ox, self.laser_y + oy

    def move_laser_to(self, x, y):
        """Двигает голову; за пределы поля станок не выезжает. Возвращает True, если точка достигнута"""
        cx = min(max(x, 0.0), self.config.field_w)
        cy = min(max(y, 0.0), self.config.field_h)
        self.laser_x, self.laser_y = cx, cy
        return math.isclose(cx, x, abs_tol=1e-9) and math.isclose(cy, y, abs_tol=1e-9)

    def move_camera_to(self, x, y):
        """Наводит камеру на точку стола. False — точка в мертвой зоне камеры (голова уперлась в край поля)"""
        ox, oy = self.config.camera_offset
        return self.move_laser_to(x - ox, y - oy)

    @property
    def dro(self):
        """Показания станка (DRO) — положение лазера"""
        return self.laser_x, self.laser_y

    def camera_dead_zone(self):
        """Часть поля, которую камера увидеть не может"""
        ox, oy = self.config.camera_offset
        field_rect = box(0, 0, self.config.field_w, self.config.field_h)
        seen = box(ox, oy, self.config.field_w + ox, self.config.field_h + oy)
        return field_rect.difference(seen)

    # --- прожиг ---

    def burn(self, gcode, invert=False, step=0.1):
        """«Прожигает» G-код и сравнивает с настоящим положением платы"""
        moves, g0_count = parse_gcode(gcode, start=self.dro)
        report = BurnReport(g0_count=g0_count)
        for x0, y0, x1, y1, on, phase in moves:
            if on:
                (report.burn_segments if phase == "raster" else report.contour_segments).append((x0, y0, x1, y1))

        w, h = self.config.field_w, self.config.field_h
        for x0, y0, x1, y1, _, _ in moves:
            for px, py in ((x0, y0), (x1, y1)):
                out = max(-px, px - w, -py, py - h, 0.0)
                report.out_of_field = max(report.out_of_field, out)

        if not report.burn_segments:
            return report

        burn = MultiLineString([((x0, y0), (x1, y1)) for x0, y0, x1, y1 in report.burn_segments])
        report.burn_length = burn.length
        tol = step / 2.0
        report.tolerance = tol

        # Прожиг за пределами платы (в обоих режимах)
        off = burn.difference(self.board_outline.buffer(tol))
        report.off_board_length = off.length
        if not off.is_empty:
            report.off_board_max = float(
                max(shapely.distance(shapely.points(shapely.get_coordinates(off)), self.board_outline))
            )

        if invert:
            target = self.board_outline.difference(self.copper)
            forbidden = self.copper.buffer(-tol)  # жечь по меди нельзя (кромку в полшага прощаем)
            hits = burn.intersection(forbidden)
            report.miss_length = hits.length
            if not hits.is_empty:
                # Насколько глубоко луч зашел в медь: концы и середины прожженных по меди кусков
                coords = shapely.get_coordinates(hits)
                mids = (coords[:-1] + coords[1:]) / 2.0
                pts = shapely.points(list(coords) + list(mids))
                inside = shapely.contains_xy(self.copper, *shapely.get_coordinates(pts).T)
                depth = shapely.distance(pts, self.copper.boundary)
                report.max_miss = float(max(depth[inside], default=0.0))
        else:
            target = self.copper
            allowed = self.copper.buffer(tol)
            report.miss_length = burn.difference(allowed).length
            ends = shapely.points([(x, y) for s in report.burn_segments for x, y in (s[:2], s[2:])])
            report.max_miss = float(max(shapely.distance(ends, self.copper), default=0.0))

        if target.area > 0:
            # Растр с шагом step: каждая строка луча покрывает полосу высотой в шаг,
            # поэтому покрытая площадь цели ≈ длина прожига по цели × шаг
            report.coverage = min(1.0, burn.intersection(target).length * step / target.area)
        return report
