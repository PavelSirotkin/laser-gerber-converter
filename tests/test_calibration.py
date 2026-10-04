import math

import pytest

from core.calibration import CalibrationError, fit_affine
from core.geometry import GerberGeometryContext
from conftest import CALIB


def apply(coeffs, x, y):
    m11, m21, m12, m22, dx, dy = coeffs
    return m11 * x + m21 * y + dx, m12 * x + m22 * y + dy


@pytest.mark.parametrize("n_points", [3, 4])
def test_fit_affine_recovers_known_transform(n_points):
    file_pts = [(5, 5), (50, 8), (10, 45), (48, 44)][:n_points]
    mach_pts = [apply(CALIB, *p) for p in file_pts]
    assert fit_affine(file_pts, mach_pts) == pytest.approx(CALIB, abs=1e-9)


def test_fit_affine_least_squares_averages_error():
    """С 4 точками ошибка наведения одной точки распределяется, а не переносится целиком"""
    file_pts = [(0, 0), (100, 0), (0, 100), (100, 100)]
    mach_pts = [apply(CALIB, *p) for p in file_pts]
    mach_pts[3] = (mach_pts[3][0] + 0.2, mach_pts[3][1])
    coeffs = fit_affine(file_pts, mach_pts)
    residuals = [math.dist(apply(coeffs, *f), m) for f, m in zip(file_pts, mach_pts)]
    assert max(residuals) < 0.2


@pytest.mark.parametrize("file_pts", [
    [(0, 0), (10, 10), (20, 20)],   # на одной прямой
    [(5, 5), (5, 5), (5, 5)],       # совпадают
])
def test_fit_affine_rejects_degenerate_points(file_pts):
    with pytest.raises(CalibrationError):
        fit_affine(file_pts, [(1, 1), (2, 2), (3, 4)])


def test_fit_affine_needs_three_points():
    with pytest.raises(CalibrationError):
        fit_affine([(0, 0), (1, 0)], [(0, 0), (1, 0)])


def _context(mode):
    ctx = GerberGeometryContext([])
    if mode == "camera":
        ctx.use_camera_offset = True
        ctx.camera_offset_x, ctx.camera_offset_y = 42.9, -0.55
    elif mode == "calib":
        ctx.use_calibration = True
        ctx.matrix_coeffs = CALIB
    return ctx


@pytest.mark.parametrize("mode", ["plain", "camera", "calib"])
def test_display_local_roundtrip(mode):
    """Точка, снятая с экрана, возвращается в координаты платы в любом режиме отображения"""
    ctx = _context(mode)
    for p in [(0, 0), (12.3, -4.5), (150, 90)]:
        assert ctx.display_to_local(*ctx.local_to_display(*p)) == pytest.approx(p, abs=1e-9)


def test_recapture_with_active_calibration_keeps_matrix_exact():
    """Перезахват точки при уже примененной калибровке не портит матрицу"""
    a = math.radians(-7)
    truth = (math.cos(a), -math.sin(a), math.sin(a), math.cos(a), 100.0, 50.0)
    pts = [(5, 5), (50, 8), (10, 45)]
    ctx = _context("calib")  # на экране уже действует другая (старая) калибровка
    screen = [ctx.local_to_display(*p) for p in pts]
    captured = [ctx.display_to_local(*s) for s in screen]
    assert fit_affine(captured, [apply(truth, *p) for p in pts]) == pytest.approx(truth, abs=1e-9)


def test_display_to_local_rejects_singular_matrix():
    ctx = _context("calib")
    ctx.matrix_coeffs = (1, 2, 2, 4, 0, 0)
    with pytest.raises(ValueError):
        ctx.display_to_local(1, 1)
