"""Дымовой тест окна без экрана: загрузка, превью и расчет дают тот же G-код, что и core."""

import os

import pytest

pytest.importorskip("PyQt6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from conftest import SAMPLES, config, make_context, make_params  # noqa: E402
from PyQt6 import QtWidgets  # noqa: E402

from core.gcode import generate_gcode  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def test_window_preview_and_gcode(qapp):
    from ui.main_window import LaserConverterApp

    w = LaserConverterApp()
    w.load_gerber_geometry(os.path.join(SAMPLES, "test.gbr"))
    assert w.geo_context is not None

    cfg = config("base")
    w.cb_enable_calib.setChecked(False)
    w.cb_use_camera_offset.setChecked(False)
    w.cb_flip_x.setChecked(False)
    w.cb_flip_y.setChecked(False)
    w.cb_invert.setChecked(False)
    w.cb_snake.setChecked(True)
    w.spin_rotate.setValue(0.0)
    w.combo_laser_mode.setCurrentIndex(0)
    w.spin_contour_power.setValue(cfg["contour_power"])
    w.spin_power.setValue(cfg["power"])
    w.spin_feed.setValue(cfg["feed"])
    w.spin_step.setValue(cfg["step"])
    w.spin_overscan.setValue(cfg["overscan"])

    w.update_interactive_preview()
    scene_rect = w.view.scene.itemsBoundingRect()
    burn_geom, (xmin, ymin, xmax, ymax) = make_context("test.gbr", cfg).get_burn_geometry()
    assert scene_rect.right() == pytest.approx(xmax, abs=1e-6)
    assert -scene_rect.top() == pytest.approx(ymax, abs=1e-6)

    w.process_conversion()
    expected = generate_gcode(burn_geom, (xmin, ymin, xmax, ymax), make_params(cfg)).gcode
    assert w.generated_gcode == expected
    w.close()


@pytest.mark.parametrize("zoom", [1, 8])
def test_crosshair_reaches_board_corners(qapp, zoom):
    """Под центральный прицел можно подвести любую точку платы — и при «вписать в окно», и при зуме.
    Раньше вид упирался в границы сцены: все точки фиксировались в одном месте или со сдвигом"""
    from ui.main_window import LaserConverterApp

    w = LaserConverterApp()
    w.resize(1400, 800)
    w.show()
    w.cb_enable_calib.setChecked(False)
    w.cb_use_camera_offset.setChecked(False)
    w.spin_rotate.setValue(0.0)
    w.load_gerber_geometry(os.path.join(SAMPLES, "test.gbr"))
    qapp.processEvents()
    w.view.scale(zoom, zoom)
    _, (xmin, ymin, xmax, ymax) = w.geo_context.get_burn_geometry()

    pixel_mm = 1.0 / w.view.transform().m11()
    for x, y in [(xmin, ymin), (xmax, ymin), (xmin, ymax), (xmax, ymax)]:
        w.view.centerOn(x, -y)
        qapp.processEvents()
        cx, cy = w.view.get_center_board_coordinates()
        assert (cx, -cy) == pytest.approx((x, y), abs=2 * pixel_mm)
    w.close()
