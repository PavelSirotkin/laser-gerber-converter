"""Привязка платы к станку по реперным точкам: сдвиг, жесткая (сдвиг + поворот), подобие и аффинная модели.

Коэффициенты везде в порядке shapely.affinity.affine_transform: (m11, m21, m12, m22, dx, dy),
X = m11*x + m21*y + dx, Y = m12*x + m22*y + dy.
"""

import math
from dataclasses import dataclass, field

import numpy as np


class CalibrationError(ValueError):
    """Точки не позволяют рассчитать калибровку"""


# Модель -> минимальное число точек
MIN_POINTS = {"translation": 1, "rigid": 2, "similarity": 2, "affine": 3}

MODEL_NAMES = {
    "auto": "Авто (по числу точек)",
    "translation": "Сдвиг",
    "rigid": "Сдвиг + поворот",
    "similarity": "Сдвиг + поворот + масштаб",
    "affine": "Аффинная",
}


def auto_model(n_points):
    """Модель по числу точек: 1 — сдвиг, 2 — сдвиг + поворот, 3 и больше — аффинная"""
    if n_points >= 3:
        return "affine"
    return "rigid" if n_points == 2 else "translation"


@dataclass
class CalibrationResult:
    coeffs: tuple
    model: str
    residuals: list = field(default_factory=list)  # расстояние «пересчитанная точка платы — точка станка», мм

    @property
    def max_residual(self):
        return max(self.residuals, default=0.0)

    @property
    def redundant(self):
        """Точек больше минимума модели — невязка показывает точность наведения"""
        return len(self.residuals) > MIN_POINTS[self.model]


def apply_affine(coeffs, x, y):
    m11, m21, m12, m22, dx, dy = coeffs
    return m11 * x + m21 * y + dx, m12 * x + m22 * y + dy


def _check_counts(file_pts, mach_pts, model):
    if len(file_pts) != len(mach_pts):
        raise CalibrationError("Число точек платы и станка не совпадает.")
    need = MIN_POINTS[model]
    if len(file_pts) < need:
        raise CalibrationError(f"Для модели «{MODEL_NAMES[model]}» нужно минимум {need} точ.")


def fit_translation(file_pts, mach_pts):
    """Только сдвиг: среднее смещение точек"""
    _check_counts(file_pts, mach_pts, "translation")
    d = np.mean(np.asarray(mach_pts, float)[:, :2] - np.asarray(file_pts, float)[:, :2], axis=0)
    return (1.0, 0.0, 0.0, 1.0, float(d[0]), float(d[1]))


def fit_rigid(file_pts, mach_pts, with_scale=False):
    """Сдвиг + поворот (+ общий масштаб) методом наименьших квадратов (Умеяма). Зеркала не допускает"""
    model = "similarity" if with_scale else "rigid"
    _check_counts(file_pts, mach_pts, model)
    p = np.asarray(file_pts, float)[:, :2]
    q = np.asarray(mach_pts, float)[:, :2]
    pc, qc = p.mean(axis=0), q.mean(axis=0)
    p0, q0 = p - pc, q - qc
    spread = float((p0**2).sum())
    if spread < 1e-12:
        raise CalibrationError("Точки совпадают — поворот определить нельзя. Разнесите точки по плате.")
    a = float((p0[:, 0] * q0[:, 0] + p0[:, 1] * q0[:, 1]).sum())
    b = float((p0[:, 0] * q0[:, 1] - p0[:, 1] * q0[:, 0]).sum())
    angle = math.atan2(b, a)
    s = math.hypot(a, b) / spread if with_scale else 1.0
    c, sn = s * math.cos(angle), s * math.sin(angle)
    dx = qc[0] - (c * pc[0] - sn * pc[1])
    dy = qc[1] - (sn * pc[0] + c * pc[1])
    return (c, -sn, sn, c, float(dx), float(dy))


def fit_affine(file_pts, mach_pts):
    """Аффинная матрица методом наименьших квадратов по 3 и более парам точек"""
    _check_counts(file_pts, mach_pts, "affine")
    A = np.array([[float(p[0]), float(p[1]), 1.0] for p in file_pts])
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


def fit(file_pts, mach_pts, model="auto"):
    """Калибровка по выбранной модели ("auto" — по числу точек). Возвращает CalibrationResult"""
    if model == "auto":
        model = auto_model(len(file_pts))
    if model == "translation":
        coeffs = fit_translation(file_pts, mach_pts)
    elif model == "rigid":
        coeffs = fit_rigid(file_pts, mach_pts)
    elif model == "similarity":
        coeffs = fit_rigid(file_pts, mach_pts, with_scale=True)
    elif model == "affine":
        coeffs = fit_affine(file_pts, mach_pts)
    else:
        raise ValueError(f"Неизвестная модель калибровки: {model}")
    residuals = [
        math.dist(apply_affine(coeffs, f[0], f[1]), (m[0], m[1])) for f, m in zip(file_pts, mach_pts, strict=True)
    ]
    return CalibrationResult(coeffs, model, residuals)


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


def short_description(coeffs):
    """Коротко для статуса: поворот и масштаб"""
    d = decompose_affine(coeffs)
    scale = (d["scale_x"] + d["scale_y"]) / 2.0
    return f"поворот {d['angle']:+.3f}°, масштаб {scale:.4f}" + (", зеркало" if d["mirrored"] else "")
