"""Расчет аффинной матрицы привязки платы к станку по реперным точкам."""
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
            "Перезафиксируйте точки, разнеся их по площади платы.")

    X_m = [float(p[0]) for p in mach_pts]
    Y_m = [float(p[1]) for p in mach_pts]
    m11, m21, dx = np.linalg.lstsq(A, X_m, rcond=None)[0]
    m12, m22, dy = np.linalg.lstsq(A, Y_m, rcond=None)[0]
    return (float(m11), float(m21), float(m12), float(m22), float(dx), float(dy))
