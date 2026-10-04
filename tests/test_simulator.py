"""Сквозная проверка без станка: виртуальный стол со скрытым положением платы."""

import pytest
from conftest import SAMPLE_FILES, sample_geometries

from core.calibration import fit_affine
from core.gcode import GcodeParams, generate_gcode
from core.geometry import GerberGeometryContext
from core.simulator import MachineConfig, VirtualMachine, parse_gcode

FIDUCIALS = [(0.15, 0.15), (0.85, 0.2), (0.2, 0.85), (0.8, 0.8)]  # доли габаритов файла


def calibrate_and_burn(vm, ctx, invert=False, step=0.1, camera_sign=+1):
    """Делает то же, что оператор: наводит камеру (или лазер, если репер в мертвой зоне) на реперы,
    вводит DRO, считает калибровку, генерирует G-код и прожигает его на виртуальном столе"""
    xmin, ymin, xmax, ymax = ctx.get_raw_bounds()
    file_pts, mach_pts = [], []
    for fx, fy in FIDUCIALS:
        raw = (xmin + (xmax - xmin) * fx, ymin + (ymax - ymin) * fy)
        true = vm.true_position(*raw)
        file_pts.append(ctx.raw_to_local(*raw))
        if vm.move_camera_to(*true):
            ox, oy = vm.config.camera_offset  # галочка «Учитывать смещение камеры»: DRO + смещение
            mach_pts.append((vm.dro[0] + camera_sign * ox, vm.dro[1] + camera_sign * oy))
        else:
            assert vm.move_laser_to(*true), "репер вне поля станка"
            mach_pts.append(vm.dro)  # навелись лазером-указателем, смещение не нужно

    ctx.matrix_coeffs = fit_affine(file_pts, mach_pts)
    ctx.use_calibration = True
    geom, bounds = ctx.get_burn_geometry(invert=invert)
    gcode = generate_gcode(geom, bounds, GcodeParams(step=step, overscan=2.0), outline=ctx.board_outline()).gcode
    return vm.burn(gcode, invert=invert, step=step)


# G-код пишет координаты с 4 знаками — промах до 0.00005 мм это округление
EXACT = 1e-3
# Плата 150 мм на поле 165 мм с поворотом 5° и overscan не помещается — ей даем поворот до 1°
MAX_ROTATION = {"test140x90-B_Cu.gbr": 1.0}


@pytest.mark.parametrize("sample", SAMPLE_FILES)
@pytest.mark.parametrize("seed", [1, 2, 3])
@pytest.mark.parametrize("invert", [False, True])
def test_calibrated_burn_lands_on_board(sample, seed, invert):
    vm = VirtualMachine(sample_geometries(sample), seed=seed, max_rotation=MAX_ROTATION.get(sample, 5.0))
    report = calibrate_and_burn(vm, GerberGeometryContext(list(sample_geometries(sample))), invert=invert)
    assert report.g0_count == 0
    assert report.out_of_field == 0
    assert report.max_miss < EXACT
    assert report.miss_length < EXACT
    assert report.coverage > 0.9


def test_wide_rotated_board_overscan_leaves_field():
    """Плата 150 мм, повернутая на столе, вместе с overscan выходит за поле 165 мм — симулятор сообщает"""
    vm = VirtualMachine(sample_geometries("test140x90-B_Cu.gbr"), seed=1, max_rotation=5.0)
    report = calibrate_and_burn(vm, GerberGeometryContext(list(sample_geometries("test140x90-B_Cu.gbr"))))
    assert report.out_of_field > 0


def test_mirrored_board_with_flip_enabled():
    """Нижний слой: плата лежит зеркально, в программе включено «Отзеркалить по X»"""
    vm = VirtualMachine(sample_geometries("test.gbr"), seed=7, mirror_x=True)
    ctx = GerberGeometryContext(list(sample_geometries("test.gbr")))
    ctx.flip_x = True
    report = calibrate_and_burn(vm, ctx)
    assert report.max_miss < EXACT and report.coverage > 0.9


def test_wrong_camera_offset_sign_is_detected():
    """Если перепутать знак смещения камеры, прожиг уезжает на 2 × смещение — симулятор это видит"""
    vm = VirtualMachine(sample_geometries("test.gbr"), seed=1)
    report = calibrate_and_burn(vm, GerberGeometryContext(list(sample_geometries("test.gbr"))), camera_sign=-1)
    assert report.max_miss > 10


def test_board_at_field_edge_uses_dead_zone():
    """Плата 150 мм на поле 165 мм: часть реперов камера не видит — их наводят лазером, прожиг точный"""
    vm = VirtualMachine(sample_geometries("test140x90-B_Cu.gbr"), seed=3, max_rotation=1.0)
    ctx = GerberGeometryContext(list(sample_geometries("test140x90-B_Cu.gbr")))
    xmin, ymin, xmax, ymax = ctx.get_raw_bounds()
    blind = [
        p
        for p in FIDUCIALS
        if not vm.move_camera_to(*vm.true_position(xmin + (xmax - xmin) * p[0], ymin + (ymax - ymin) * p[1]))
    ]
    assert blind, "ожидался хотя бы один репер в мертвой зоне камеры"
    report = calibrate_and_burn(vm, ctx)
    assert report.max_miss < EXACT and report.coverage > 0.9


def test_camera_dead_zone_area():
    vm = VirtualMachine(sample_geometries("test.gbr"), config=MachineConfig(165, 95, (42.9, 0.55)), seed=1)
    assert vm.camera_dead_zone().area == pytest.approx(165 * 95 - (165 - 42.9) * (95 - 0.55))


def test_head_stops_at_field_limits():
    vm = VirtualMachine(sample_geometries("test.gbr"), seed=1)
    assert not vm.move_laser_to(-5, 200)
    assert vm.dro == (0.0, vm.config.field_h)
    assert not vm.move_camera_to(1.0, 10.0)  # левее смещения камеры — мертвая зона


def test_parse_gcode_phases_and_laser_state():
    gcode = "\n".join(
        [
            "G21 ;",
            "G90 ;",
            "M3 S0;",
            "G1 X0 Y0 F1000 S0",
            "G1 X10 Y0 S10",  # контур: жжет на малой мощности
            "M5",
            "M0 ;",
            "M4 S0",
            "G1 F1500",
            "G1 X0 Y1 S0",
            "G1 X5 S200",
            "G0 X8",
            "G1 X9 S0",
            "M5",
            "G1 X0 Y0 S0",
            "M2",
        ]
    )
    moves, g0 = parse_gcode(gcode)
    burned = [(m[0], m[2], m[5]) for m in moves if m[4]]
    assert burned == [(0, 10, "contour"), (0, 5, "raster")]
    assert g0 == 1


def test_burn_reports_out_of_field_and_g0():
    vm = VirtualMachine(sample_geometries("test.gbr"), seed=1)
    report = vm.burn("G21\nM4 S0\nG1 X-3 Y1 F1000 S0\nG0 X170\nM5")
    assert report.out_of_field == pytest.approx(5.0)
    assert report.g0_count == 1


def test_shifted_burn_is_reported_as_error_in_invert_mode():
    """Плата, уехавшая на смещение камеры: в инверсии прожиг мимо меди не считается промахом,
    но прожиг вне платы и выход за поле должны дать вердикт «ошибка»"""
    g = sample_geometries("test140x90-B_Cu.gbr")
    vm = VirtualMachine(g, seed=3, max_rotation=0.5)
    ctx = GerberGeometryContext(list(g))
    calibrate_and_burn(vm, ctx, invert=True)  # правильная калибровка
    a, b, d, e, dx, dy = ctx.matrix_coeffs
    ctx.matrix_coeffs = (a, b, d, e, dx - 42.9, dy - 0.55)  # «забыли» смещение камеры
    geom, bounds = ctx.get_burn_geometry(invert=True)
    report = vm.burn(generate_gcode(geom, bounds, GcodeParams(step=0.1)).gcode, invert=True, step=0.1)
    assert report.off_board_length > 0.05 * report.burn_length
    assert report.verdict()[0] == "error"


@pytest.mark.parametrize("sample", ["test.gbr", "test70x70.gbr"])
@pytest.mark.parametrize("seed", [1, 2])
def test_invert_mask_follows_rotated_board(sample, seed):
    """Маска инверсии строится по контуру платы, повернутому вместе с ней. Раньше это был прямоугольник
    по осям станка: на плате, повернутой на 4-5°, до 40 % прожига уходило мимо платы"""
    g = sample_geometries(sample)
    vm = VirtualMachine(g, seed=seed, max_rotation=5.0)
    report = calibrate_and_burn(vm, GerberGeometryContext(list(g)), invert=True)
    assert report.off_board_length < 1e-3
    assert report.coverage > 0.99
    assert report.verdict()[0] == "ok"


@pytest.mark.parametrize("mirror", [False, True])
def test_contour_follows_true_board_outline(mirror):
    """Пробный обход контура идет по настоящему краю повернутой платы, а не по описанному прямоугольнику"""
    import shapely

    g = sample_geometries("test.gbr")
    vm = VirtualMachine(g, seed=2, max_rotation=5.0, mirror_x=mirror)
    ctx = GerberGeometryContext(list(g))
    ctx.flip_x = mirror
    report = calibrate_and_burn(vm, ctx)
    corners = [(x1, y1) for _, _, x1, y1 in report.contour_segments]
    assert len(corners) == 4
    edge = vm.board_outline.exterior
    assert max(edge.distance(shapely.Point(p)) for p in corners) < 1e-3
    # Контур замкнут и обходит все 4 угла платы
    assert corners[-1] == pytest.approx(report.contour_segments[0][:2], abs=1e-4)
    true_corners = list(vm.board_outline.exterior.coords)[:-1]
    for c in true_corners:
        assert min(shapely.Point(c).distance(shapely.Point(p)) for p in corners) < 1e-3
