"""Режим --test через интерфейс: калибровка в окне по виртуальному станку и «прожиг» результата."""
import os

import pytest

pytest.importorskip("PyQt6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6 import QtWidgets  # noqa: E402

from conftest import SAMPLES  # noqa: E402
from test_simulator import FIDUCIALS  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.mark.parametrize("use_camera", [True, False])
def test_calibration_through_ui_burns_on_board(qapp, monkeypatch, use_camera):
    from ui.main_window import LaserConverterApp
    from ui.simulator_window import SimulatorWindow

    # Диалог ввода координат станка «нажимает OK» сам; DRO подставляется из симулятора
    monkeypatch.setattr(QtWidgets.QDialog, "exec", lambda self: QtWidgets.QDialog.DialogCode.Accepted)

    sim = SimulatorWindow(camera_offset=(42.9, 0.55), seed=5)
    w = LaserConverterApp(simulator=sim)
    w.cb_use_camera_offset.setChecked(use_camera)
    w.spin_cam_offset_x.setValue(42.9)
    w.spin_cam_offset_y.setValue(0.55)
    w.cb_invert.setChecked(False)
    w.cb_flip_x.setChecked(False)
    w.spin_rotate.setValue(0.0)
    w.spin_step.setValue(0.1)
    w.resize(1400, 800)
    w.show()
    w.load_gerber_geometry(os.path.join(SAMPLES, "test.gbr"))
    w.cb_enable_calib.setChecked(True)
    qapp.processEvents()
    w.view.scale(20, 20)  # оператор приближает вид для точного наведения

    ctx = w.geo_context
    xmin, ymin, xmax, ymax = ctx.get_raw_bounds()
    for idx, (fx, fy) in enumerate(FIDUCIALS[:3]):
        raw = (xmin + (xmax - xmin) * fx, ymin + (ymax - ymin) * fy)
        true = sim.vm.true_position(*raw)
        # На станке: навести на репер камеру (или лазер, если камера не используется)
        reached = sim.vm.move_camera_to(*true) if use_camera else sim.vm.move_laser_to(*true)
        assert reached
        # В программе: совместить прицел с тем же репером на рисунке
        w.sync_geometry_context()
        sx, sy = ctx.local_to_display(*ctx.raw_to_local(*raw))
        w.view.centerOn(sx, -sy)  # прокрутка вида, как при перетаскивании мышью
        qapp.processEvents()
        cx, cy = w.view.get_center_board_coordinates()
        assert (cx, -cy) == pytest.approx((sx, sy), abs=0.01), "репер не удалось подвести под прицел"
        w.capture_point_in_crosshair(idx)

    assert w.use_calibration
    w.process_conversion()
    report = sim.vm.burn(w.generated_gcode, invert=False, step=0.1)
    # Наведение прицела дискретно (пиксель экрана при зуме) — допускаем сотые доли мм
    assert report.max_miss < 0.05
    assert report.coverage > 0.9
    assert "Прожиг лег на плату" in sim.report_label.text()
    w.close()
    sim.close()
