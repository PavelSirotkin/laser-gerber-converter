"""Генерация растрового G-кода для GRBL по геометрии прожига."""
import math
from dataclasses import dataclass, field

from core.geometry import scanline_intervals


@dataclass
class GcodeParams:
    power: int = 1000           # мощность прожига S
    feedrate: int = 1500        # скорость гравировки, мм/мин
    step: float = 0.1           # шаг строки, мм
    overscan: float = 2.0       # вылет каретки за плату для разгона/торможения, мм
    snake: bool = True          # сканирование змейкой
    laser_mode: str = "M4"      # "M4" (динамическая мощность) или "M3"
    contour_power: int = 10     # мощность тестового обхода контура S


@dataclass
class Toolpath:
    gcode: str
    lines_count: int
    burn_segments: list = field(default_factory=list)    # [(y, x_start, x_end)] — лазер жжет
    travel_segments: list = field(default_factory=list)  # [(y, x_from, x_to)] — холостые ходы и overscan


def generate_gcode(burn_geom, bounds, params):
    """Растровый G-код по геометрии прожига в координатах станка. bounds — габариты платы (xmin, ymin, xmax, ymax)."""
    xmin, ymin, xmax, ymax = (float(v) for v in bounds)
    p = params

    gcode = []
    gcode.append("; Gerber -> LaserGRBL GCode (Real-Space OOP-Engine)")
    gcode.append(f"G21 ;\nG90 ;")

    # --- ТЕСТОВЫЙ ОБХОД КОНТУРА ПЛАТЫ СТАНОЧНЫМ ЛУЧОМ ---
    gcode.append(f"M3 S0;")
    # Замкнутый прямоугольник по габаритам платы (без overscan — это зона разгона, а не плата)
    gcode.append(f"G1 X{xmin:.4f} Y{ymin:.4f} F1000 S0")
    gcode.append(f"G1 X{xmax:.4f} Y{ymin:.4f} S{p.contour_power}")
    gcode.append(f"G1 X{xmax:.4f} Y{ymax:.4f}")
    gcode.append(f"G1 X{xmin:.4f} Y{ymax:.4f}")
    gcode.append(f"G1 X{xmin:.4f} Y{ymin:.4f}")
    gcode.append("M5\nG4 P0.5\nM0 ;")

    gcode.append(f"{p.laser_mode} S0\nG1 F{p.feedrate}")

    lines_count = int(math.ceil((ymax - ymin) / p.step))
    if lines_count <= 0: lines_count = 1

    scan_ys = [min(ymin + (i * p.step) + (p.step / 2.0), ymax) for i in range(lines_count)]
    all_segments = scanline_intervals(burn_geom, scan_ys, xmin - 0.5, xmax + 0.5)

    # Каретка выходит влево за пределы платы на overscan, и вправо на overscan
    line_start_x = xmin - p.overscan
    line_end_x = xmax + p.overscan

    burn, travel = [], []
    direction_right = True
    for current_y, segments_coords in zip(scan_ys, all_segments):
        for s_x, e_x in segments_coords:
            burn.append((current_y, s_x, e_x))

        if direction_right or not p.snake:
            # Движение слева направо: стартуем из левой точки разгона
            gcode.append(f"G1 X{line_start_x:.4f} Y{current_y:.4f} S0")
            last_x = line_start_x
            for start_x, end_x in segments_coords:
                if start_x > last_x:
                    gcode.append(f"G1 X{start_x:.4f} S0")
                    travel.append((current_y, last_x, start_x))
                gcode.append(f"G1 X{end_x:.4f} S{p.power}")
                last_x = end_x

            # Доезжаем до крайней правой точки торможения
            if last_x < line_end_x:
                gcode.append(f"G1 X{line_end_x:.4f} S0")
                travel.append((current_y, last_x, line_end_x))
        else:
            # Движение справа налево (режим змейки): стартуем из правой точки разгона
            gcode.append(f"G1 X{line_end_x:.4f} Y{current_y:.4f} S0")
            last_x = line_end_x
            for start_x, end_x in segments_coords[::-1]:  # Переворачиваем массив сегментов для хода назад
                if end_x < last_x:
                    gcode.append(f"G1 X{end_x:.4f} S0")
                    travel.append((current_y, last_x, end_x))
                gcode.append(f"G1 X{start_x:.4f} S{p.power}")
                last_x = start_x

            # Доезжаем до крайней левой точки торможения
            if last_x > line_start_x:
                gcode.append(f"G1 X{line_start_x:.4f} S0")
                travel.append((current_y, last_x, line_start_x))

        if p.snake:
            direction_right = not direction_right

    gcode.append(f"M5\nG1 X0.000 Y0.000 S0\nM2")
    return Toolpath("\n".join(gcode), lines_count, burn, travel)
