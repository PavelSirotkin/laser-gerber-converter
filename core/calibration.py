"""Расчет аффинной матрицы привязки платы к станку по реперным точкам."""

import math

import numpy as np


class CalibrationError(ValueError):
    """Точки не позволяют рассчитать калибровку"""


def fit_affine(file_pts, mach_pts):
    """Аффинная матрица методом наименьших квадратов по 3 и более парам точек.

    file_pts — локальные координаты точек на плате [(x, y), ...],
    mach_pts — координаты тех же точек на станке [(X, Y), ...].
    Возвращает коэффициенты (m11, m21, m12, m22, dx, dy) в порядке shapely.affinity.affine_transform:
    X = m11*x + m21*y + dx, Y = m12*x + m22*y + dy.
    """
    if len(file_pts) != len(mach_pts):
        raise CalibrationError("Число точек платы и станка не совпадает.")
    if len(file_pts) < 3:
        raise CalibrationError("Для калибровки нужно минимум 3 точки.")

    A = np.array([[float(x), float(y), 1.0] for x, y in file_pts])
    if np.linalg.matrix_rank(A, tol=1e-6) < 3:
        raise CalibrationError(
            "Точки совпадают или лежат на одной прямой — калибровка невозможна.\n"
            "Перезафиксируйте точки, разнеся их по площади платы."
        )

    X_m = [float(p[0]) for p in mach_pts]
    Y_m = [float(p[1]) for p in mach_pts]
    m11, m21, dx = np.linalg.lstsq(A, X_m, rcond=None)[0]
    m12, m22, dy = np.linalg.lstsq(A, Y_m, rcond=None)[0]
    return (float(m11), float(m21), float(m12), float(m22), float(dx), float(dy))


def decompose_affine(coeffs):
    """Разложение матрицы на понятные величины: сдвиг, поворот (°), масштабы по осям, перекос (°), зеркало"""
    m11, m21, m12, m22, dx, dy = coeffs
    # Столбцы линейной части — образы осей X и Y платы
    sx = math.hypot(m11, m12)
    angle = math.degrees(math.atan2(m12, m11))
    det = m11 * m22 - m21 * m12
    mirrored = det < 0
    sy = abs(det) / sx if sx else 0.0
    # Перекос: отклонение угла между образами осей от 90°
    cos_xy = (m11 * m21 + m12 * m22) / (sx * math.hypot(m21, m22)) if sx and math.hypot(m21, m22) else 0.0
    skew = math.degrees(math.asin(max(-1.0, min(1.0, cos_xy))))
    return dict(dx=dx, dy=dy, angle=angle, scale_x=sx, scale_y=sy, skew=skew, mirrored=mirrored)


def describe_affine(coeffs):
    """Одна строка для оператора: поворот, масштаб, перекос, сдвиг"""
    d = decompose_affine(coeffs)
    text = (
        f"Поворот {d['angle']:+.3f}°, масштаб X {d['scale_x']:.4f} Y {d['scale_y']:.4f}, "
        f"перекос {d['skew']:+.3f}°, сдвиг ({d['dx']:.2f}, {d['dy']:.2f}) мм"
    )
    return text + (", ЗЕРКАЛО" if d["mirrored"] else "")
