"""Модели привязки, разбор координат из буфера обмена, поле станка."""

import math

import pytest

from core.calibration import CalibrationError, apply_affine, auto_model, fit, fit_rigid, short_description
from core.coords import parse_coordinates
from core.machine import MachineConfig, camera_dead_zone, travel_overflow


def rigid(angle_deg, dx, dy, scale=1.0):
    a = math.radians(angle_deg)
    c, s = scale * math.cos(a), scale * math.sin(a)
    return (c, -s, s, c, dx, dy)


FILE_PTS = [(5, 5), (50, 8), (10, 45), (48, 44)]


@pytest.mark.parametrize(
    "model, truth, n",
    [
        ("translation", (1, 0, 0, 1, 12.5, -3.25), 1),
        ("rigid", rigid(-4.0, 30, 12), 2),
        ("similarity", rigid(2.5, 30, 12, scale=1.003), 2),
        ("affine", (1.001, 0.02, -0.01, 0.998, 7, 9), 3),
    ],
)
def test_models_recover_known_transform(model, truth, n):
    file_pts = FILE_PTS[:n]
    mach_pts = [apply_affine(truth, *p) for p in file_pts]
    result = fit(file_pts, mach_pts, model)
    assert result.model == model
    assert result.coeffs == pytest.approx(truth, abs=1e-9)
    assert result.max_residual < 1e-9


@pytest.mark.parametrize("n, model", [(1, "translation"), (2, "rigid"), (3, "affine"), (4, "affine")])
def test_auto_model_by_point_count(n, model):
    assert auto_model(n) == model
    truth = rigid(3.0, 10, 20)
    assert fit(FILE_PTS[:n], [apply_affine(truth, *p) for p in FILE_PTS[:n]], "auto").model == model


def test_rigid_least_squares_with_extra_points_reports_residual():
    truth = rigid(1.5, 40, 25)
    mach = [apply_affine(truth, *p) for p in FILE_PTS]
    mach[2] = (mach[2][0] + 0.1, mach[2][1])  # одна точка наведена с ошибкой 0.1 мм
    result = fit(FILE_PTS, mach, "rigid")
    assert result.redundant
    assert 0.0 < result.max_residual < 0.1
    assert math.degrees(math.atan2(result.coeffs[2], result.coeffs[0])) == pytest.approx(1.5, abs=0.2)


def test_rigid_ignores_scale_but_similarity_finds_it():
    truth = rigid(0.0, 0, 0, scale=1.01)
    mach = [apply_affine(truth, *p) for p in FILE_PTS[:2]]
    assert fit(FILE_PTS[:2], mach, "rigid").max_residual > 0.1
    assert fit(FILE_PTS[:2], mach, "similarity").max_residual < 1e-9


@pytest.mark.parametrize("model", ["rigid", "similarity"])
def test_rigid_rejects_coincident_points(model):
    with pytest.raises(CalibrationError):
        fit([(5, 5), (5, 5)], [(1, 1), (2, 2)], model)


def test_rigid_never_mirrors():
    """Жесткая модель не зеркалит: зеркальную плату нужно отразить галочкой «Отзеркалить», а не привязкой"""
    coeffs = fit_rigid(FILE_PTS[:3], [(-x, y) for x, y in FILE_PTS[:3]])
    m11, m21, m12, m22 = coeffs[:4]
    assert m11 * m22 - m21 * m12 > 0


@pytest.mark.parametrize("model, n", [("rigid", 1), ("affine", 2)])
def test_not_enough_points(model, n):
    with pytest.raises(CalibrationError):
        fit(FILE_PTS[:n], FILE_PTS[:n], model)


def test_short_description():
    assert short_description(rigid(-0.96, 0, 0, 1.0012)) == "поворот -0.960°, масштаб 1.0012"


@pytest.mark.parametrize(
    "text, xy",
    [
        ("<Idle|MPos:12.345,67.890,0.000|FS:0,0>", (12.345, 67.89)),
        ("<Idle|WPos:-1.5,2.25,0.000>", (-1.5, 2.25)),
        ("X12.345 Y67.890", (12.345, 67.89)),
        ("x: 1.5   y: -2", (1.5, -2.0)),
        ("X=10 Y=20 Z=0", (10.0, 20.0)),
        ("12.345, 67.890", (12.345, 67.89)),
        ("12,5; 30,25", (12.5, 30.25)),
        ("12.3\t45.6", (12.3, 45.6)),
    ],
)
def test_parse_coordinates(text, xy):
    assert parse_coordinates(text) == pytest.approx(xy)


@pytest.mark.parametrize("text", ["", "   ", "привет", "42", "some long text " * 10 + " 1 2"])
def test_parse_coordinates_rejects_garbage(text):
    assert parse_coordinates(text) is None


def test_travel_overflow():
    cfg = MachineConfig(165, 95, (42.9, 0.55))
    assert travel_overflow((10, 5, 150, 90), 2.0, cfg) == 0.0
    assert travel_overflow((10, 5, 164, 90), 2.0, cfg) == pytest.approx(1.0)  # справа: 164 + 2 > 165
    assert travel_overflow((1, 5, 100, 90), 2.0, cfg) == pytest.approx(1.0)  # слева: 1 - 2 < 0
    assert travel_overflow((10, -0.5, 100, 96), 2.0, cfg) == pytest.approx(1.0)  # по Y overscan не нужен


def test_camera_dead_zone_is_on_the_far_side_from_camera():
    cfg = MachineConfig(165, 95, (42.9, 0.0))
    dead = camera_dead_zone(cfg)
    assert dead.bounds == pytest.approx((0, 0, 42.9, 95))
    cfg_left = MachineConfig(165, 95, (-30.0, 0.0))
    assert camera_dead_zone(cfg_left).bounds == pytest.approx((135, 0, 165, 95))
