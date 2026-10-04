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
    # Рисунок платы — элементы сцены над полем станка (поле и мертвая зона камеры лежат ниже, z < 0)
    board_items = [it for it in w.view.scene.items() if it.zValue() == 0]
    scene_rect = board_items[0].sceneBoundingRect()
    for it in board_items[1:]:
        scene_rect = scene_rect.united(it.sceneBoundingRect())
    ctx = make_context("test.gbr", cfg)
    burn_geom, (xmin, ymin, xmax, ymax) = ctx.get_burn_geometry()
    assert scene_rect.right() == pytest.approx(xmax, abs=1e-6)
    assert -scene_rect.top() == pytest.approx(ymax, abs=1e-6)

    w.process_conversion()
    expected = generate_gcode(burn_geom, (xmin, ymin, xmax, ymax), make_params(cfg), outline=ctx.board_outline()).gcode
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


def test_corrupt_calibration_in_ini_is_ignored(qapp, tmp_path):
    """Вырожденная матрица в INI (например, после неудачной калибровки) не ломает загрузку платы"""
    from PyQt6 import QtCore

    from ui.main_window import LaserConverterApp

    ini = QtCore.QSettings(os.path.expanduser("~/.LaserConverterApp.ini"), QtCore.QSettings.Format.IniFormat)
    ini.setValue("calib_matrix_active", "true")
    for key, value in dict(mat_m11=0.0, mat_m21=0.0, mat_m12=0.0, mat_m22=0.0, mat_dx=1.0, mat_dy=1.0).items():
        ini.setValue(key, value)
    ini.sync()

    w = LaserConverterApp()
    assert not w.use_calibration
    w.load_gerber_geometry(os.path.join(SAMPLES, "test.gbr"))
    assert w.geo_context is not None
    assert "Ошибка" not in w.status_label.text()
    w.close()


def test_tabs_layout_and_start_on_print(qapp):
    """Слева вкладки «Печать» / «Настройки» (с подвкладками «Лазер» и «Станок и камера»);
    приложение всегда стартует на «Печати», прицел привязки включен всегда"""
    from ui.main_window import LaserConverterApp

    w = LaserConverterApp()
    assert [w.tabs.tabText(i) for i in range(w.tabs.count())] == ["Печать", "Настройки"]
    assert [w.settings_tabs.tabText(i) for i in range(w.settings_tabs.count())] == ["Лазер", "Станок и камера"]
    assert w.tabs.currentIndex() == 0
    assert w.view.calibration_mode

    def tab_of(widget):
        while widget is not None and widget not in (w.tab_print, w.settings_tabs):
            widget = widget.parentWidget()
        return "Печать" if widget is w.tab_print else "Настройки"

    for widget in (
        w.entry_path,
        w.cb_invert,
        w.cb_flip_x,
        w.combo_calib_model,
        w.btn_pt1,
        w.btn_convert,
    ):
        assert tab_of(widget) == "Печать"
    for widget in (
        w.spin_power,
        w.spin_feed,
        w.spin_step,
        w.spin_overscan,
        w.spin_field_w,
        w.spin_cam_offset_x,
        w.spin_rotate,
    ):
        assert tab_of(widget) == "Настройки"

    w.tabs.setCurrentIndex(1)  # даже если в прошлый раз закрыли на «Настройках»
    w.close()
    assert LaserConverterApp().tabs.currentIndex() == 0
