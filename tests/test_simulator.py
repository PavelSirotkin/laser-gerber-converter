"""Сквозная проверка без станка: виртуальный стол со скрытым положением платы."""
import pytest

from core.calibration import fit_affine
from core.gcode import GcodeParams, generate_gcode
from core.geometry import GerberGeometryContext
from core.simulator import MachineConfig, VirtualMachine, parse_gcode
from conftest import SAMPLE_FILES, sample_geometries

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
            ox, oy = vm.config.camera_offset   # галочка «Учитывать смещение камеры»: DRO + смещение
            mach_pts.append((vm.dro[0] + camera_sign * ox, vm.dro[1] + camera_sign * oy))
        else:
            assert vm.move_laser_to(*true), "репер вне поля станка"
            mach_pts.append(vm.dro)            # навелись лазером-указателем, смещение не нужно

    ctx.matrix_coeffs = fit_affine(file_pts, mach_pts)
    ctx.use_calibration = True
    geom, bounds = ctx.get_burn_geometry(invert=invert)
    gcode = generate_gcode(geom, bounds, GcodeParams(step=step, overscan=2.0)).gcode
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
    blind = [p for p in FIDUCIALS
             if not vm.move_camera_to(*vm.true_position(xmin + (xmax - xmin) * p[0], ymin + (ymax - ymin) * p[1]))]
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
    assert not vm.move_camera_to(1.0, 10.0)   # левее смещения камеры — мертвая зона


def test_parse_gcode_phases_and_laser_state():
    gcode = "\n".join([
        "G21 ;", "G90 ;", "M3 S0;",
        "G1 X0 Y0 F1000 S0", "G1 X10 Y0 S10",   # контур: жжет на малой мощности
        "M5", "M0 ;", "M4 S0", "G1 F1500",
        "G1 X0 Y1 S0", "G1 X5 S200", "G0 X8", "G1 X9 S0",
        "M5", "G1 X0 Y0 S0", "M2",
    ])
    moves, g0 = parse_gcode(gcode)
    burned = [(m[0], m[2], m[5]) for m in moves if m[4]]
    assert burned == [(0, 10, "contour"), (0, 5, "raster")]
    assert g0 == 1


def test_burn_reports_out_of_field_and_g0():
    vm = VirtualMachine(sample_geometries("test.gbr"), seed=1)
    report = vm.burn("G21\nM4 S0\nG1 X-3 Y1 F1000 S0\nG0 X170\nM5")
    assert report.out_of_field == pytest.approx(5.0)
    assert report.g0_count == 1
