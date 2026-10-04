"""Привязка по 1/2/3+ точкам через окно, поле станка, буфер обмена, автосохранение настроек."""

import os

import pytest

pytest.importorskip("PyQt6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from conftest import SAMPLES  # noqa: E402
from PyQt6 import QtCore, QtWidgets  # noqa: E402
from test_simulator import FIDUCIALS  # noqa: E402

CAMERA = (42.9, 0.55)


@pytest.fixture(scope="module")
def qapp():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def accept_dialogs(monkeypatch):
    """Окно ввода координат «нажимает OK» само, предупреждения не блокируют тест"""
    monkeypatch.setattr(QtWidgets.QDialog, "exec", lambda self: QtWidgets.QDialog.DialogCode.Accepted)
    warnings = []
    monkeypatch.setattr(QtWidgets.QMessageBox, "warning", lambda *a, **k: warnings.append(a[2]))
    return warnings


def make_window(qapp, simulator=None, sample="test.gbr"):
    from ui.main_window import LaserConverterApp

    w = LaserConverterApp(simulator=simulator)
    w.spin_cam_offset_x.setValue(CAMERA[0])
    w.spin_cam_offset_y.setValue(CAMERA[1])
    w.last_aimed_by_camera = True
    w.cb_invert.setChecked(False)
    w.cb_flip_x.setChecked(False)
    w.spin_rotate.setValue(0.0)
    w.spin_step.setValue(0.1)
    w.resize(1400, 800)
    w.show()
    w.load_gerber_geometry(os.path.join(SAMPLES, sample))
    qapp.processEvents()
    return w


def make_simulator(seed, rotation, scale_pct=0.0):
    from ui.simulator_window import SimulatorWindow

    sim = SimulatorWindow(camera_offset=CAMERA, seed=seed)
    sim.spin_rotation.setValue(rotation)
    sim.spin_scale.setValue(scale_pct)
    return sim


def capture_fiducials(qapp, w, sim, indices):
    """Как оператор: наводит камеру симулятора (или лазер в мертвой зоне) и прицел окна на реперы"""
    qapp.processEvents()
    w.view.scale(20, 20)
    ctx = w.geo_context
    xmin, ymin, xmax, ymax = ctx.get_raw_bounds()
    for slot, fid in enumerate(indices):
        fx, fy = FIDUCIALS[fid]
        raw = (xmin + (xmax - xmin) * fx, ymin + (ymax - ymin) * fy)
        true = sim.vm.true_position(*raw)
        sim.rb_aim_camera.setChecked(True)
        sim.aim(*true)
        if sim.blocked:
            sim.rb_aim_laser.setChecked(True)
            sim.aim(*true)
        assert not sim.blocked
        w.sync_geometry_context()
        sx, sy = ctx.local_to_display(*ctx.raw_to_local(*raw))
        w.view.centerOn(sx, -sy)
        qapp.processEvents()
        w.capture_point_in_crosshair(slot)


def burn_report(w, sim):
    w.process_conversion()
    return sim.vm.burn(w.generated_gcode, invert=w.cb_invert.isChecked(), step=w.spin_step.value())


def test_one_point_binding_is_translation(qapp, accept_dialogs):
    """Плата лежит ровно (без поворота): одной точки достаточно — привязка сдвигом"""
    sim = make_simulator(seed=7, rotation=0.0)
    w = make_window(qapp, sim)
    capture_fiducials(qapp, w, sim, [3])
    assert w.calibration.model == "translation"
    assert "Сдвиг по 1 точ." in w.calib_info_label.text()
    report = burn_report(w, sim)
    assert report.max_miss < 0.05 and report.verdict()[0] == "ok"
    assert "Привязка: поворот +0.000°, масштаб 1.0000" in w.status_label.text()


def test_two_point_binding_finds_rotation(qapp, accept_dialogs):
    """Плата повернута: двух точек достаточно — сдвиг + поворот"""
    sim = make_simulator(seed=2, rotation=5.0)
    w = make_window(qapp, sim)
    capture_fiducials(qapp, w, sim, [0, 3])
    assert w.calibration.model == "rigid"
    from core.calibration import decompose_affine

    true_angle = decompose_affine(sim.vm.board_to_table)["angle"]
    assert abs(true_angle) > 1.0  # плата действительно повернута
    assert decompose_affine(w.matrix_coeffs)["angle"] == pytest.approx(true_angle, abs=0.01)
    assert f"поворот {decompose_affine(w.matrix_coeffs)['angle']:+.3f}°" in w.status_label.text()
    report = burn_report(w, sim)
    assert report.max_miss < 0.05 and report.verdict()[0] == "ok"


def test_model_switch_recalculates_with_residual(qapp, accept_dialogs):
    sim = make_simulator(seed=5, rotation=3.0, scale_pct=0.0)
    w = make_window(qapp, sim)
    capture_fiducials(qapp, w, sim, [0, 1, 2])
    assert w.calibration.model == "affine"
    w.combo_calib_model.setCurrentIndex(w.combo_calib_model.findData("rigid"))
    assert w.calibration.model == "rigid"
    assert "Невязка" in w.calib_info_label.text()  # 3 точки при минимуме 2
    assert w.calibration.max_residual < 0.05
    report = burn_report(w, sim)
    assert report.verdict()[0] == "ok"


def test_not_enough_points_for_selected_model(qapp, accept_dialogs):
    sim = make_simulator(seed=5, rotation=0.0)
    w = make_window(qapp, sim)
    w.combo_calib_model.setCurrentIndex(w.combo_calib_model.findData("affine"))
    capture_fiducials(qapp, w, sim, [0, 1])
    assert not w.use_calibration
    assert "еще 1 точ." in w.calib_info_label.text()


def test_reset_points_clears_binding(qapp, accept_dialogs):
    sim = make_simulator(seed=5, rotation=2.0)
    w = make_window(qapp, sim)
    capture_fiducials(qapp, w, sim, [0, 3])
    assert w.use_calibration
    w.reset_points()
    assert not w.use_calibration and w.matrix_coeffs is None
    assert w.settings.value("calib_matrix_active") == "false"


def test_camera_offset_does_not_shift_drawing(qapp):
    """Без привязки рисунок прижат к нулю станка независимо от смещения камеры"""
    w = make_window(qapp)
    _, (xmin, ymin, _, _) = w.geo_context.get_burn_geometry()
    assert (xmin, ymin) == pytest.approx((0.0, 0.0))


def test_field_overflow_warning(qapp, accept_dialogs):
    """С привязкой координаты — это DRO станка: проход за пределами поля подсвечивается"""
    sim = make_simulator(seed=7, rotation=0.0)
    w = make_window(qapp, sim)
    capture_fiducials(qapp, w, sim, [3])
    _, (xmin, _, xmax, _) = w.geo_context.get_burn_geometry()
    w.spin_field_w.setValue(round(xmax + 1.0, 1))  # справа нужен overscan 2 мм, а места 1 мм
    assert "выходит за рабочее поле станка на" in w.status_label.text()
    w.spin_field_w.setValue(165.0)
    assert "выходит за рабочее поле" not in w.status_label.text()


def test_without_binding_field_is_not_checked(qapp):
    w = make_window(qapp)
    assert "Без привязки" in w.status_label.text()
    assert "выходит за рабочее поле" not in w.status_label.text()


def test_clipboard_coordinates_prefill_dialog(qapp, accept_dialogs):
    w = make_window(qapp)
    w.last_aimed_by_camera = False
    QtWidgets.QApplication.clipboard().setText("<Idle|MPos:12.500,30.250,0.000|FS:0,0>")
    w.capture_point_in_crosshair(0)
    assert w.manual_mach_pts[0] == pytest.approx((12.5, 30.25, False))
    QtWidgets.QApplication.clipboard().setText("")


def test_settings_autosave_and_restore(qapp):
    from ui.main_window import LaserConverterApp

    w = make_window(qapp)
    w.spin_overscan.setValue(3.5)
    w.cb_snake.setChecked(False)
    w.spin_field_w.setValue(300.0)
    w.combo_calib_model.setCurrentIndex(w.combo_calib_model.findData("similarity"))
    # Значения сохранены сразу, без закрытия окна
    ini = QtCore.QSettings(os.path.expanduser("~/.LaserConverterApp.ini"), QtCore.QSettings.Format.IniFormat)
    assert float(ini.value("overscan_dist")) == 3.5
    assert ini.value("cb_snake") == "false"

    w2 = LaserConverterApp()
    assert w2.spin_overscan.value() == 3.5
    assert not w2.cb_snake.isChecked()
    assert w2.spin_field_w.value() == 300.0
    assert w2.combo_calib_model.currentData() == "similarity"
    assert w2.spin_cam_offset_x.value() == pytest.approx(CAMERA[0])


def test_binding_restored_from_previous_session(qapp, accept_dialogs):
    from ui.main_window import LaserConverterApp

    sim = make_simulator(seed=11, rotation=4.0)
    w = make_window(qapp, sim)
    capture_fiducials(qapp, w, sim, [0, 3])
    coeffs = w.matrix_coeffs

    w2 = LaserConverterApp()
    assert w2.use_calibration
    assert w2.matrix_coeffs == pytest.approx(coeffs)
    assert w2.manual_mach_pts[1] is not None and w2.manual_mach_pts[2] is None


def test_dialog_remembers_last_aiming(qapp, accept_dialogs):
    """Окно ввода открывается с выбором «камерой / лазером» как у предыдущей точки, и между сеансами тоже"""
    from ui.main_window import LaserConverterApp

    sim = make_simulator(seed=99, rotation=0.0)  # первый репер в мертвой зоне — его наводят лазером
    w = make_window(qapp, sim)
    capture_fiducials(qapp, w, sim, [0])
    assert w.manual_mach_pts[0][2] is False and w.last_aimed_by_camera is False
    assert LaserConverterApp().last_aimed_by_camera is False

    capture_fiducials(qapp, w, sim, [0, 1])  # второй репер камера видит
    assert w.manual_mach_pts[1][2] is True and w.last_aimed_by_camera is True
    assert LaserConverterApp().last_aimed_by_camera is True
