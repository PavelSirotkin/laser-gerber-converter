"""Главное окно конвертера: параметры станка, привязка платы по точкам, превью и сохранение G-кода."""

import math
import os
import traceback

from PyQt6 import QtCore, QtGui, QtWidgets

from core.calibration import MIN_POINTS, MODEL_NAMES, CalibrationError, describe_affine, fit, short_description
from core.coords import parse_coordinates
from core.gcode import GcodeParams, generate_gcode
from core.geometry import GerberGeometryContext
from core.gerber import load_gerber
from core.machine import MachineConfig, camera_dead_zone, travel_overflow
from ui.qt_paths import shapely_to_qt_paths, toolpath_to_qt_paths
from ui.view import LaserGraphicsView

POINT_COUNT = 4
POINT_COLORS = ["#ff1744", "#2979ff", "#00e676", "#e040fb"]
DONE_STYLE = "background-color: #c8e6c9; font-weight: bold;"


class LaserConverterApp(QtWidgets.QWidget):
    def __init__(self, simulator=None):
        super().__init__()
        # Окно виртуального станка (режим --test) или None
        self.simulator = simulator
        ini_path = os.path.expanduser("~/.LaserConverterApp.ini")
        self.settings = QtCore.QSettings(ini_path, QtCore.QSettings.Format.IniFormat)

        # Геометрия печатной платы и последний рассчитанный G-код
        self.geo_context = None
        self.generated_gcode = None

        # Реперные точки: локальные координаты на плате и координаты станка (X, Y, наведено камерой)
        self.manual_file_pts = [None] * POINT_COUNT
        self.manual_mach_pts = [None] * POINT_COUNT
        self.manual_markers = [None] * POINT_COUNT

        # Результат привязки (core.calibration.CalibrationResult) и ее матрица
        self.calibration = None
        self.use_calibration = False
        self.matrix_coeffs = None

        # Чем наводилась последняя точка (камерой — к DRO прибавляется смещение камеры).
        # Окно ввода координат открывается с этим выбором; запоминается между сеансами
        self.last_aimed_by_camera = False

        self._loading_settings = False
        self.init_ui()
        self.load_saved_settings()

    # ------------------------------------------------------------------ интерфейс

    def _spin(self, grid, row, label, lo, hi, value, decimals=None, step=None):
        """Поле ввода числа в сетке параметров (колесико мыши его не меняет)"""
        grid.addWidget(QtWidgets.QLabel(label), row, 0)
        box = QtWidgets.QDoubleSpinBox() if decimals is not None else QtWidgets.QSpinBox()
        if decimals is not None:
            box.setDecimals(decimals)
        box.setRange(lo, hi)
        if step is not None:
            box.setSingleStep(step)
        box.setValue(value)
        box.installEventFilter(self)
        grid.addWidget(box, row, 1)
        return box

    @staticmethod
    def _scroll_page(layout):
        """Страница вкладки с прокруткой (растяжку в конец layout добавляет вызывающий, после содержимого)"""
        page = QtWidgets.QWidget()
        page.setLayout(layout)
        scroll = QtWidgets.QScrollArea()
        scroll.setWidget(page)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        return scroll

    def init_ui(self):
        self.setWindowTitle("LaserGRBL Raster Converter & Native Visualizer (OOP-Engine)")
        self.setMinimumWidth(1150)
        self.setMinimumHeight(760)

        self.layout_horizontal = QtWidgets.QHBoxLayout()
        self.setLayout(self.layout_horizontal)

        # Левая панель: вкладки «Печать» (работа с платой) и «Настройки» (лазер, станок и камера).
        # Приложение всегда стартует на вкладке «Печать»
        self.tabs = QtWidgets.QTabWidget()
        self.tabs.setFixedWidth(455)
        self.layout_horizontal.addWidget(self.tabs)

        print_layout = QtWidgets.QVBoxLayout()
        self.tab_print = self._scroll_page(print_layout)
        self.tabs.addTab(self.tab_print, "Печать")

        self.settings_tabs = QtWidgets.QTabWidget()
        self.tabs.addTab(self.settings_tabs, "Настройки")
        laser_layout = QtWidgets.QVBoxLayout()
        machine_layout = QtWidgets.QVBoxLayout()
        self.settings_tabs.addTab(self._scroll_page(laser_layout), "Лазер")
        self.settings_tabs.addTab(self._scroll_page(machine_layout), "Станок и камера")

        # ===== Настройки → Лазер =====
        self.param_group = QtWidgets.QGroupBox("Параметры лазера")
        grid = QtWidgets.QGridLayout()
        self.param_group.setLayout(grid)

        grid.addWidget(QtWidgets.QLabel("Режим лазера GRBL:"), 0, 0)
        self.combo_laser_mode = QtWidgets.QComboBox()
        self.combo_laser_mode.addItems(["M4 (Динамическая мощность)", "M3 (Постоянная мощность)"])
        grid.addWidget(self.combo_laser_mode, 0, 1)

        self.spin_contour_power = self._spin(grid, 1, "Мощность для контура (S):", 0, 1000, 10)
        self.spin_contour_feed = self._spin(grid, 2, "Скорость контура (мм/мин):", 1, 30000, 1000)
        self.spin_contour_feed.setToolTip("Скорость тестового обхода контура платы перед паузой M0")
        self.spin_power = self._spin(grid, 3, "Макс. мощность лазера (S):", 1, 1000, 1000)
        self.spin_feed = self._spin(grid, 4, "Скорость гравировки (мм/мин):", 1, 30000, 1500)
        self.spin_step = self._spin(grid, 5, "Шаг строки / Луч (мм):", 0.001, 10.0, 0.1, 4, 0.01)
        self.spin_overscan = self._spin(grid, 6, "Вылет каретки Overscan (мм):", 0.0, 50.0, 2.0, 1, 0.5)
        laser_layout.addWidget(self.param_group)

        # ===== Настройки → Станок и камера =====
        self.machine_group = QtWidgets.QGroupBox("Станок и камера")
        grid = QtWidgets.QGridLayout()
        self.machine_group.setLayout(grid)
        self.spin_field_w = self._spin(grid, 0, "Рабочее поле X (мм):", 1.0, 5000.0, 165.0, 1, 1.0)
        self.spin_field_h = self._spin(grid, 1, "Рабочее поле Y (мм):", 1.0, 5000.0, 95.0, 1, 1.0)
        self.spin_cam_offset_x = self._spin(grid, 2, "Камера X (Лазер -> Камера):", -5000, 5000, 0.0, 4, 0.1)
        self.spin_cam_offset_y = self._spin(grid, 3, "Камера Y (Лазер -> Камера):", -5000, 5000, 0.0, 4, 0.1)
        for box in (self.spin_cam_offset_x, self.spin_cam_offset_y):
            box.setToolTip("Положение камеры относительно лазера, мм. Может быть отрицательным.")
        self.spin_rotate = self._spin(grid, 4, "Точный поворот стола (град):", -360.0, 360.0, 0.0, 4, 0.01)
        machine_layout.addWidget(self.machine_group)

        # ===== Печать: файл =====
        self.file_group = QtWidgets.QGroupBox("Исходный файл Gerber")
        self.file_layout = QtWidgets.QHBoxLayout()
        self.file_group.setLayout(self.file_layout)
        self.entry_path = QtWidgets.QLineEdit()
        self.entry_path.setPlaceholderText("Выберите .gbr файл...")
        self.file_layout.addWidget(self.entry_path)
        self.btn_browse = QtWidgets.QPushButton("Обзор...")
        self.btn_browse.clicked.connect(self.browse_file)
        self.file_layout.addWidget(self.btn_browse)
        print_layout.addWidget(self.file_group)

        # ===== Печать: режимы и зеркала =====
        self.modes_group = QtWidgets.QGroupBox("Режимы работы и зеркалирование")
        self.modes_layout = QtWidgets.QGridLayout()
        self.modes_group.setLayout(self.modes_layout)
        self.cb_snake = QtWidgets.QCheckBox("Сканирование змейкой")
        self.cb_snake.setChecked(True)
        self.modes_layout.addWidget(self.cb_snake, 0, 0)
        self.cb_invert = QtWidgets.QCheckBox("ИНВЕРСИЯ / НЕГАТИВ (маска)")
        self.modes_layout.addWidget(self.cb_invert, 0, 1)
        self.cb_flip_x = QtWidgets.QCheckBox("Отзеркалить по X")
        self.modes_layout.addWidget(self.cb_flip_x, 1, 0)
        self.cb_flip_y = QtWidgets.QCheckBox("Отзеркалить по Y")
        self.modes_layout.addWidget(self.cb_flip_y, 1, 1)
        print_layout.addWidget(self.modes_group)

        # ===== Печать: привязка платы по точкам (режим всегда активен) =====
        self.calib_group = QtWidgets.QGroupBox("Привязка по центральному прицелу")
        self.calib_layout = QtWidgets.QVBoxLayout()
        self.calib_group.setLayout(self.calib_layout)

        model_row = QtWidgets.QHBoxLayout()
        model_row.addWidget(QtWidgets.QLabel("Модель:"))
        self.combo_calib_model = QtWidgets.QComboBox()
        for key in ("auto", "translation", "rigid", "similarity", "affine"):
            self.combo_calib_model.addItem(MODEL_NAMES[key], key)
        self.combo_calib_model.setToolTip(
            "Авто: 1 точка — сдвиг, 2 — сдвиг + поворот, 3 и больше — аффинная.\n"
            "Сдвиг — от 1 точки; сдвиг + поворот — от 2 точек (текстолит не растягивается — обычно лучший выбор);\n"
            "+ масштаб — от 2 точек; аффинная — от 3 точек (растяжение и перекос заготовки)."
        )
        self.combo_calib_model.setSizeAdjustPolicy(
            QtWidgets.QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.combo_calib_model.currentIndexChanged.connect(self.recalculate_calibration)
        model_row.addWidget(self.combo_calib_model, 1)
        self.calib_layout.addLayout(model_row)

        self.point_buttons = []
        for idx in range(POINT_COUNT):
            btn = QtWidgets.QPushButton(f"Зафиксировать Точку {idx + 1}")
            btn.clicked.connect(lambda _=False, i=idx: self.capture_point_in_crosshair(i))
            # Длинный текст с координатами не расширяет панель (полностью виден в подсказке)
            btn.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored, QtWidgets.QSizePolicy.Policy.Fixed)
            self.calib_layout.addWidget(btn)
            self.point_buttons.append(btn)
        self.btn_pt1, self.btn_pt2, self.btn_pt3, self.btn_pt4 = self.point_buttons

        self.btn_reset_points = QtWidgets.QPushButton("Сбросить точки")
        self.btn_reset_points.clicked.connect(self.reset_points)
        self.calib_layout.addWidget(self.btn_reset_points)

        # Что вычислила привязка: модель, поворот, масштаб, перекос, сдвиг, невязка
        self.calib_info_label = QtWidgets.QLabel()
        self.calib_info_label.setWordWrap(True)
        self.calib_info_label.setStyleSheet("color: #2e7d32;")
        self.calib_layout.addWidget(self.calib_info_label)
        print_layout.addWidget(self.calib_group)

        # ===== Печать: статус и пусковые кнопки =====
        self.status_label = QtWidgets.QLabel("Статус: Ожидание выбора файла...")
        self.status_label.setWordWrap(True)
        self.status_label.setStyleSheet("color: gray; font-weight: bold;")
        print_layout.addWidget(self.status_label)

        self.btn_convert = QtWidgets.QPushButton("Рассчитать траекторию и превью")
        self.btn_convert.setStyleSheet(
            "font-weight: bold; font-size: 13px; padding: 6px; background-color: #0288d1; color: white;"
        )
        self.btn_convert.clicked.connect(self.process_conversion)
        print_layout.addWidget(self.btn_convert)

        self.btn_save = QtWidgets.QPushButton("Скачать / Сохранить G-Code")
        self.btn_save.setStyleSheet(
            "font-weight: bold; font-size: 14px; padding: 10px; background-color: #2e7d32; color: white;"
        )
        self.btn_save.setDisabled(True)
        self.btn_save.clicked.connect(self.save_gcode_dialog)
        print_layout.addWidget(self.btn_save)

        # Содержимое вкладок прижато к верху
        for layout in (print_layout, laser_layout, machine_layout):
            layout.addStretch(1)

        # Любое изменение параметра сразу сохраняется в INI; влияющие на рисунок — обновляют превью
        for widget in self._settings_widgets().values():
            self._changed_signal(widget[0]).connect(self.save_current_settings)
        for widget in (
            self.spin_rotate,
            self.spin_overscan,
            self.cb_snake,
            self.cb_invert,
            self.cb_flip_x,
            self.cb_flip_y,
            self.spin_field_w,
            self.spin_field_h,
            self.spin_cam_offset_x,
            self.spin_cam_offset_y,
        ):
            self._changed_signal(widget).connect(self.update_interactive_preview)

        # Правый графический холст интерактивной визуализации; прицел привязки виден всегда
        self.plot_group = QtWidgets.QGroupBox("Экран интерактивной визуализации векторов")
        self.plot_layout = QtWidgets.QVBoxLayout()
        self.plot_group.setLayout(self.plot_layout)

        self.view = LaserGraphicsView()
        self.view.calibration_mode = True
        self.plot_layout.addWidget(self.view)
        self.layout_horizontal.addWidget(self.plot_group, 1)

    def eventFilter(self, watched, event):
        """Блокирует случайное изменение числовых значений колесиком мыши"""
        if event.type() == QtCore.QEvent.Type.Wheel:
            if isinstance(watched, (QtWidgets.QSpinBox, QtWidgets.QDoubleSpinBox)):
                event.ignore()
                return True
        return super().eventFilter(watched, event)

    # ------------------------------------------------------------------ настройки

    def _settings_widgets(self):
        """Ключ INI -> (виджет, значение по умолчанию). Имена ключей совместимы с прежними версиями"""
        return {
            "laser_mode_idx": (self.combo_laser_mode, 0),
            "contour_power": (self.spin_contour_power, 10),
            "contour_feed": (self.spin_contour_feed, 1000),
            "laser_power": (self.spin_power, 200),
            "feed_rate": (self.spin_feed, 1500),
            "raster_step": (self.spin_step, 0.1),
            "overscan_dist": (self.spin_overscan, 2.0),
            "rotate_angle": (self.spin_rotate, 0.0),
            "cb_snake": (self.cb_snake, True),
            "cb_invert": (self.cb_invert, False),
            "cb_flip_x": (self.cb_flip_x, False),
            "cb_flip_y": (self.cb_flip_y, False),
            "cam_offset_x": (self.spin_cam_offset_x, 0.0),
            "cam_offset_y": (self.spin_cam_offset_y, 0.0),
            "field_w": (self.spin_field_w, 165.0),
            "field_h": (self.spin_field_h, 95.0),
            "calib_model": (self.combo_calib_model, "auto"),
        }

    @staticmethod
    def _changed_signal(widget):
        if isinstance(widget, QtWidgets.QComboBox):
            return widget.currentIndexChanged
        if isinstance(widget, QtWidgets.QCheckBox):
            return widget.stateChanged
        return widget.valueChanged

    def load_saved_settings(self):
        """Загружает последнюю сохраненную сессию конфигурации станка из INI-файла"""
        self._loading_settings = True
        widgets = self._settings_widgets()
        for widget, _ in widgets.values():
            widget.blockSignals(True)
        try:
            for key, (widget, default) in widgets.items():
                value = self.settings.value(key, default)
                try:
                    if widget is self.combo_calib_model:
                        idx = widget.findData(str(value))
                        widget.setCurrentIndex(max(idx, 0))
                    elif isinstance(widget, QtWidgets.QComboBox):
                        widget.setCurrentIndex(int(value))
                    elif isinstance(widget, QtWidgets.QCheckBox):
                        widget.setChecked(str(value).lower() == "true")
                    elif isinstance(widget, QtWidgets.QSpinBox):
                        widget.setValue(int(float(value)))
                    else:
                        widget.setValue(float(value))
                except (TypeError, ValueError) as e:
                    print(f"Ошибка чтения настройки {key}={value!r}: {e}")
        finally:
            for widget, _ in widgets.values():
                widget.blockSignals(False)

        # Ключ use_camera_offset — от прежней галочки «компенсация смещения камеры»
        self.last_aimed_by_camera = str(self.settings.value("use_camera_offset", "false")).lower() == "true"
        self._load_saved_calibration()
        self._loading_settings = False

    def _load_saved_calibration(self):
        """Восстанавливает точки привязки и матрицу прошлого сеанса"""
        if self.settings.value("calib_matrix_active", "false") != "true":
            return
        try:
            keys = ("mat_m11", "mat_m21", "mat_m12", "mat_m22", "mat_dx", "mat_dy")
            defaults = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
            coeffs = tuple(float(self.settings.value(k, d)) for k, d in zip(keys, defaults, strict=True))
            m11, m21, m12, m22, _, _ = coeffs
            if not all(math.isfinite(v) for v in coeffs) or abs(m11 * m22 - m21 * m12) < 1e-9:
                raise ValueError(f"матрица вырождена или содержит NaN: {coeffs}")

            for idx in range(POINT_COUNT):
                fx, fy = self.settings.value(f"pt_file_{idx}_x"), self.settings.value(f"pt_file_{idx}_y")
                mx, my = self.settings.value(f"pt_mach_{idx}_x"), self.settings.value(f"pt_mach_{idx}_y")
                if None in (fx, fy, mx, my):
                    continue
                has_cam = self.settings.value(f"pt_mach_{idx}_cam", "false") == "true"
                self.manual_file_pts[idx] = (float(fx), float(fy))
                self.manual_mach_pts[idx] = (float(mx), float(my), has_cam)
                cam_label = " (+Камера)" if has_cam else ""
                self.point_buttons[idx].setText(
                    f"Т{idx + 1}: Загружено -> Ст({float(mx):.2f}, {float(my):.2f}){cam_label}"
                )
                self.point_buttons[idx].setStyleSheet(DONE_STYLE)

            self.matrix_coeffs = coeffs
            self.use_calibration = True
            self.calib_info_label.setText(f"Привязка из прошлого сеанса: {describe_affine(coeffs)}")
        except (TypeError, ValueError) as e:
            print(f"Ошибка загрузки калибровки из INI: {e}")
            self.matrix_coeffs = None
            self.use_calibration = False

    def save_current_settings(self):
        """Мгновенно синхронизирует и перезаписывает параметры станка в INI-файл"""
        if self._loading_settings:
            return
        for key, (widget, _) in self._settings_widgets().items():
            if widget is self.combo_calib_model:
                value = widget.currentData()
            elif isinstance(widget, QtWidgets.QComboBox):
                value = widget.currentIndex()
            elif isinstance(widget, QtWidgets.QCheckBox):
                value = "true" if widget.isChecked() else "false"
            else:
                value = widget.value()
            self.settings.setValue(key, value)
        self.settings.setValue("use_camera_offset", "true" if self.last_aimed_by_camera else "false")
        self.settings.sync()

    def _save_calibration(self):
        """Точки и матрица привязки в INI"""
        active = bool(self.use_calibration and self.matrix_coeffs)
        self.settings.setValue("calib_matrix_active", "true" if active else "false")
        if active:
            for key, value in zip(
                ("mat_m11", "mat_m21", "mat_m12", "mat_m22", "mat_dx", "mat_dy"), self.matrix_coeffs, strict=True
            ):
                self.settings.setValue(key, float(value))
        for idx in range(POINT_COUNT):
            file_pt, mach_pt = self.manual_file_pts[idx], self.manual_mach_pts[idx]
            if file_pt is None or mach_pt is None:
                for key in ("pt_file_{}_x", "pt_file_{}_y", "pt_mach_{}_x", "pt_mach_{}_y", "pt_mach_{}_cam"):
                    self.settings.remove(key.format(idx))
                continue
            self.settings.setValue(f"pt_file_{idx}_x", float(file_pt[0]))
            self.settings.setValue(f"pt_file_{idx}_y", float(file_pt[1]))
            self.settings.setValue(f"pt_mach_{idx}_x", float(mach_pt[0]))
            self.settings.setValue(f"pt_mach_{idx}_y", float(mach_pt[1]))
            self.settings.setValue(f"pt_mach_{idx}_cam", "true" if mach_pt[2] else "false")
        self.settings.sync()

    def machine_config(self):
        return MachineConfig(
            self.spin_field_w.value(),
            self.spin_field_h.value(),
            (self.spin_cam_offset_x.value(), self.spin_cam_offset_y.value()),
        )

    def closeEvent(self, event):
        """Гарантированное сохранение настроек при закрытии окна"""
        self.save_current_settings()
        event.accept()

    # ------------------------------------------------------------------ файл

    def browse_file(self):
        """Вызывает проводник для выбора исходного Gerber-файла печатной платы"""
        file_path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Открыть Gerber файл", "", "Gerber Files (*.gbr *.pho);;All Files (*)"
        )
        if file_path:
            self.entry_path.setText(file_path)
            self.status_label.setText(f"Статус: Загрузка {os.path.basename(file_path)}...")
            self.status_label.setStyleSheet("color: blue;")
            self.btn_save.setDisabled(True)
            QtWidgets.QApplication.processEvents()
            self.load_gerber_geometry(file_path)

    def load_gerber_geometry(self, gerber_path):
        """Парсит Gerber-файл и оборачивает его геометрию в контекст платы"""
        try:
            self.geo_context = GerberGeometryContext(load_gerber(gerber_path))
            self.update_interactive_preview()
            self.sync_geometry_context()
            _, bounds = self.geo_context.get_burn_geometry()
            xmin, ymin, xmax, ymax = bounds
            self.view.fitInView(
                QtCore.QRectF(xmin, -ymax, xmax - xmin, ymax - ymin), QtCore.Qt.AspectRatioMode.KeepAspectRatio
            )
            if self.simulator:
                self.simulator.set_board(self.geo_context.raw_geometries)
        except Exception as e:
            print("\n" + "=" * 40 + " Ошибка загрузки Gerber " + "=" * 40)
            traceback.print_exc()
            print("=" * 105 + "\n")
            self.geo_context = None
            self.status_label.setText(f"Ошибка загрузки Gerber: {str(e)}")
            self.status_label.setStyleSheet("color: red;")

    def sync_geometry_context(self):
        """Синхронизирует текущие параметры интерфейса в объект геометрического контекста платы"""
        if not self.geo_context:
            return
        self.geo_context.rotate_angle = self.spin_rotate.value()
        self.geo_context.flip_x = self.cb_flip_x.isChecked()
        self.geo_context.flip_y = self.cb_flip_y.isChecked()
        self.geo_context.use_calibration = self.use_calibration
        self.geo_context.matrix_coeffs = self.matrix_coeffs

    # ------------------------------------------------------------------ привязка по точкам

    def reset_points(self):
        """Сбрасывает все точки и привязку"""
        self.manual_file_pts = [None] * POINT_COUNT
        self.manual_mach_pts = [None] * POINT_COUNT
        for idx, btn in enumerate(self.point_buttons):
            btn.setText(f"Зафиксировать Точку {idx + 1}")
            btn.setStyleSheet("")
        self.recalculate_calibration()

    def _model_key(self):
        return self.combo_calib_model.currentData() or "auto"

    def recalculate_calibration(self):
        """Пересчитывает привязку по зафиксированным точкам (после каждой точки, смены модели или сброса)"""
        valid = [i for i in range(POINT_COUNT) if self.manual_file_pts[i] is not None and self.manual_mach_pts[i]]
        model = self._model_key()
        need = 1 if model == "auto" else MIN_POINTS[model]

        new_result, message, error = None, "", False
        if len(valid) < need:
            if valid or model != "auto":
                message = f"«{MODEL_NAMES[model]}»: зафиксируйте еще {need - len(valid)} точ."
        else:
            try:
                new_result = fit(
                    [self.manual_file_pts[i] for i in valid],
                    [self.manual_mach_pts[i][:2] for i in valid],
                    model,
                )
            except CalibrationError as e:
                message, error = str(e), True
                QtWidgets.QMessageBox.warning(self, "Внимание", str(e))

        # Точка платы под прицелом до смены системы координат экрана — после пересчета вернем ее под прицел
        local_center = None
        if self.geo_context:
            self.sync_geometry_context()
            cx, cy = self.view.get_center_board_coordinates()
            try:
                local_center = self.geo_context.display_to_local(cx, -cy)
            except ValueError:
                local_center = None

        self.calibration = new_result
        self.matrix_coeffs = new_result.coeffs if new_result else None
        self.use_calibration = new_result is not None

        if new_result:
            text = f"{MODEL_NAMES[new_result.model]} по {len(valid)} точ.:\n{describe_affine(new_result.coeffs)}"
            if new_result.redundant:
                text += f"\nНевязка (точность наведения): до {new_result.max_residual:.3f} мм"
            self.calib_info_label.setText(text)
            self.calib_info_label.setStyleSheet("color: #2e7d32;")
        else:
            self.calib_info_label.setText(message)
            self.calib_info_label.setStyleSheet("color: #c62828;" if error else "color: #555555;")

        self._save_calibration()
        if self.geo_context:
            self.update_interactive_preview()
            if local_center is not None:
                new_x, new_y = self.geo_context.local_to_display(*local_center)
                self.view.centerOn(new_x, -new_y)

    def capture_point_in_crosshair(self, point_idx):
        """Фиксирует точку платы под прицелом и спрашивает ее координаты на станке"""
        if not self.geo_context:
            QtWidgets.QMessageBox.warning(self, "Внимание", "Сначала загрузите Gerber файл!")
            return

        scene_x, scene_y = self.view.get_center_board_coordinates()
        cam_x = self.spin_cam_offset_x.value()
        cam_y = self.spin_cam_offset_y.value()

        # Локальные координаты точки на плате. Экран показывает плату либо прижатой к нулю,
        # либо уже с привязкой — снимаем ровно то, что на экране
        self.sync_geometry_context()
        try:
            file_pt = self.geo_context.display_to_local(scene_x, -scene_y)
        except ValueError as e:
            QtWidgets.QMessageBox.warning(self, "Внимание", str(e))
            return
        previous = (self.manual_file_pts[point_idx], self.manual_mach_pts[point_idx])
        self.manual_file_pts[point_idx] = file_pt
        self.redraw_calibration_markers()

        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle(f"Координаты станка для Точки {point_idx + 1}")
        dialog.setMinimumWidth(380)
        dialog_layout = QtWidgets.QVBoxLayout(dialog)
        dialog_layout.addWidget(
            QtWidgets.QLabel(
                "Вы навели прицел на репер платы.\n"
                f"Координата прицела на сетке: X = {scene_x:.4f}, Y = {-scene_y:.4f} мм\n\n"
                "Задайте координаты станка (DRO), при которых на этот репер\n"
                "наведена камера или лазер:"
            )
        )

        grid = QtWidgets.QGridLayout()
        dialog_layout.addLayout(grid)
        spin_x = self._spin(grid, 0, "Координата X станка (мм):", -9999.0, 9999.0, scene_x, 4, 0.1)
        spin_y = self._spin(grid, 1, "Координата Y станка (мм):", -9999.0, 9999.0, -scene_y, 4, 0.1)

        cb_add_cam = QtWidgets.QCheckBox(f"Наведено камерой: учитывать смещение ({cam_x:+.4f}, {cam_y:+.4f} мм)")
        cb_add_cam.setChecked(self.last_aimed_by_camera)  # как у предыдущей точки
        dialog_layout.addWidget(cb_add_cam)

        source_label = QtWidgets.QLabel()
        source_label.setWordWrap(True)
        source_label.setStyleSheet("color: #555555;")

        def paste_clipboard(show_failure=True):
            text = QtWidgets.QApplication.clipboard().text()
            xy = parse_coordinates(text)
            if xy:
                spin_x.setValue(xy[0])
                spin_y.setValue(xy[1])
                source_label.setText(f"Подставлено из буфера обмена: X {xy[0]:.4f}, Y {xy[1]:.4f}")
            elif show_failure:
                shown = text.strip()[:60] or "пусто"
                source_label.setText(f"В буфере обмена нет координат ({shown}).")
            return xy is not None

        btn_paste = QtWidgets.QPushButton("Вставить из буфера обмена")
        btn_paste.setToolTip("Понимает статус GRBL (MPos:x,y,z), «X12.3 Y45.6» и «12.3, 45.6»")
        btn_paste.clicked.connect(lambda: paste_clipboard(True))
        dialog_layout.addWidget(btn_paste)

        if self.simulator:
            # Режим --test: координаты станка берем у виртуального станка, как оператор с экрана DRO
            def take_simulator_dro():
                dro = self.simulator.dro()
                if dro:
                    spin_x.setValue(dro[0])
                    spin_y.setValue(dro[1])
                    # Наведено камерой — DRO дополняется смещением камеры, лазером — нет
                    cb_add_cam.setChecked(self.simulator.aim_by_camera)
                how = "камерой" if self.simulator.aim_by_camera else "лазером"
                warn = self.simulator.warning()
                source_label.setText(
                    f"Симулятор: наведено {how}." + (f"<br><b style='color:#c62828'>{warn}</b>" if warn else "")
                )

            take_simulator_dro()
            btn_dro = QtWidgets.QPushButton("Взять DRO из симулятора")
            btn_dro.clicked.connect(take_simulator_dro)
            dialog_layout.addWidget(btn_dro)
        elif not paste_clipboard(False):
            source_label.setText("Подставлены координаты прицела — замените их показаниями станка.")
        dialog_layout.addWidget(source_label)

        button_box = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Ok | QtWidgets.QDialogButtonBox.StandardButton.Cancel, dialog
        )
        button_box.accepted.connect(dialog.accept)
        button_box.rejected.connect(dialog.reject)
        dialog_layout.addWidget(button_box)

        if dialog.exec() != QtWidgets.QDialog.DialogCode.Accepted:
            # Отмена — возвращаем прежнее состояние точки
            self.manual_file_pts[point_idx], self.manual_mach_pts[point_idx] = previous
            self.redraw_calibration_markers()
            return

        self.last_aimed_by_camera = cb_add_cam.isChecked()
        self.save_current_settings()
        mach_x, mach_y = spin_x.value(), spin_y.value()
        if cb_add_cam.isChecked():
            mach_x += cam_x
            mach_y += cam_y
        self.manual_mach_pts[point_idx] = (mach_x, mach_y, cb_add_cam.isChecked())

        cam_label = " (+Камера)" if cb_add_cam.isChecked() else ""
        self.point_buttons[point_idx].setText(
            f"Т{point_idx + 1}: Сетка({scene_x:.4f}, {-scene_y:.4f}) -> Ст({mach_x:.4f}, {mach_y:.4f}){cam_label}"
        )
        self.point_buttons[point_idx].setToolTip(self.point_buttons[point_idx].text())
        self.point_buttons[point_idx].setStyleSheet(DONE_STYLE)
        self.recalculate_calibration()

    def redraw_calibration_markers(self):
        """Рисует отметки реперов по их локальным координатам платы в текущей системе экрана.
        После привязки отметки должны лечь точно на реперы — это визуальная проверка."""
        for marker in self.manual_markers:
            try:
                if marker is not None and marker.scene() is self.view.scene:
                    self.view.scene.removeItem(marker)
            except RuntimeError:
                pass  # объект уже удален вместе со scene.clear()
        self.manual_markers = [None] * POINT_COUNT
        if not self.geo_context:
            return

        self.sync_geometry_context()
        for idx, pt in enumerate(self.manual_file_pts):
            if pt is None:
                continue
            x, y = self.geo_context.local_to_display(*pt)
            marker = QtWidgets.QGraphicsEllipseItem(x - 0.4, -y - 0.4, 0.8, 0.8)
            marker.setBrush(QtGui.QBrush(QtGui.QColor(POINT_COLORS[idx])))
            marker.setPen(QtGui.QPen(QtGui.QColor("#ffffff"), 0.15))
            marker.setZValue(10)
            self.view.scene.addItem(marker)
            self.manual_markers[idx] = marker

    # ------------------------------------------------------------------ превью и G-код

    def _add_machine_field(self):
        """Рабочее поле станка и мертвая зона камеры на сцене (под рисунком платы)"""
        config = self.machine_config()
        field_item = self.view.scene.addRect(
            QtCore.QRectF(0, -config.field_h, config.field_w, config.field_h),
            QtGui.QPen(QtGui.QColor("#607d8b"), 0, QtCore.Qt.PenStyle.DashLine),
        )
        field_item.setZValue(-2)
        dead, _ = shapely_to_qt_paths(camera_dead_zone(config))
        dead_item = self.view.scene.addPath(
            dead, QtGui.QPen(QtCore.Qt.PenStyle.NoPen), QtGui.QBrush(QtGui.QColor(255, 82, 82, 45))
        )
        dead_item.setToolTip("Мертвая зона камеры: сюда камеру навести нельзя, реперы здесь наводите лазером")
        dead_item.setZValue(-1)

    def _field_and_calibration_lines(self, bounds):
        """Строки статуса о привязке и о поле станка; второй элемент — True, если плата не помещается"""
        if not (self.use_calibration and self.matrix_coeffs):
            # Без привязки ноль — рабочий ноль, выставленный оператором: с полем станка не сравниваем
            return ["Без привязки: плата прижата к нулю станка."], False
        lines = [f"Привязка: {short_description(self.matrix_coeffs)}"]
        overflow = travel_overflow(bounds, self.spin_overscan.value(), self.machine_config())
        if overflow > 1e-9:
            lines.append(f"ВНИМАНИЕ: проход выходит за рабочее поле станка на {overflow:.2f} мм!")
        return lines, overflow > 1e-9

    def update_interactive_preview(self):
        """Интерактивное превью. Рисует ту же геометрию, что пойдет в G-код (единый конвейер трансформаций)"""
        if not self.geo_context:
            return
        self.sync_geometry_context()
        ovr = self.spin_overscan.value()
        inv_mode = self.cb_invert.isChecked()

        try:
            self.view.scene.clear()
            self.manual_markers = [None] * POINT_COUNT
            self._add_machine_field()

            rx_min, ry_min, rx_max, ry_max = self.geo_context.get_raw_bounds()
            if rx_max - rx_min <= 0 or ry_max - ry_min <= 0:
                return

            burn_geom, bounds = self.geo_context.get_burn_geometry(invert=inv_mode)
            if burn_geom is None:
                return
            min_x, _, max_x, _ = bounds

            fill_path, line_path = shapely_to_qt_paths(burn_geom)
            color = QtGui.QColor("#1565c0" if inv_mode else "#2e7d32")

            fill_item = QtWidgets.QGraphicsPathItem(fill_path)
            fill_item.setCacheMode(QtWidgets.QGraphicsItem.CacheMode.DeviceCoordinateCache)
            fill_item.setBrush(QtGui.QBrush(color))
            fill_item.setPen(QtGui.QPen(QtCore.Qt.PenStyle.NoPen))
            self.view.scene.addItem(fill_item)

            if not line_path.isEmpty():
                line_item = QtWidgets.QGraphicsPathItem(line_path)
                line_item.setPen(QtGui.QPen(color, 0))
                self.view.scene.addItem(line_item)

            home_marker = QtWidgets.QGraphicsEllipseItem(-0.6, -0.6, 1.2, 1.2)
            home_marker.setBrush(QtGui.QBrush(QtGui.QColor("#ff0000")))
            home_marker.setPen(QtGui.QPen(QtGui.QColor("#ffffff"), 0.2))
            self.view.scene.addItem(home_marker)
            self.redraw_calibration_markers()

            extra, overflow = self._field_and_calibration_lines(bounds)
            self.status_label.setText(
                "\n".join(
                    [
                        "Статус: Геометрия готова.",
                        f"Размер из файла: {rx_max - rx_min:.2f} x {ry_max - ry_min:.2f} мм",
                        f"Плата на станке по X: {min_x:.2f} ... {max_x:.2f} мм",
                        f"Габарит хода башки X (ЧПУ DRO): [ {min_x - ovr:.2f} ... {max_x + ovr:.2f} ] мм",
                        *extra,
                    ]
                )
            )
            self.status_label.setStyleSheet(f"color: {'#c62828' if overflow else '#2e7d32'}; font-weight: bold;")

        except Exception as e:
            print("\n" + "=" * 40 + " ОШИБКА ПРЕВЬЮ " + "=" * 40)
            traceback.print_exc()
            self.status_label.setText(f"Ошибка визуализации: {str(e)}")
            self.status_label.setStyleSheet("color: red;")

    def process_conversion(self):
        """Расчет траекторий сканирования и генерация G-кода"""
        if not self.geo_context:
            return
        self.save_current_settings()

        self.status_label.setText("Статус: Оптимизация слоев...")
        self.status_label.setStyleSheet("color: orange;")
        QtWidgets.QApplication.processEvents()

        params = GcodeParams(
            power=self.spin_power.value(),
            feedrate=self.spin_feed.value(),
            step=self.spin_step.value(),
            overscan=self.spin_overscan.value(),
            snake=self.cb_snake.isChecked(),
            laser_mode="M4" if self.combo_laser_mode.currentIndex() == 0 else "M3",
            contour_power=self.spin_contour_power.value(),
            contour_feed=self.spin_contour_feed.value(),
        )

        try:
            self.sync_geometry_context()

            # Геометрия прожига в координатах станка: без привязки прижата к (0,0), с привязкой — по реперам
            burn_geom, bounds = self.geo_context.get_burn_geometry(invert=self.cb_invert.isChecked())
            if burn_geom is None:
                return

            toolpath = generate_gcode(burn_geom, bounds, params, outline=self.geo_context.board_outline())
            self.generated_gcode = toolpath.gcode
            self.view.scene.clear()
            self._add_machine_field()
            self.view.setBackgroundBrush(QtGui.QColor("#f0f0f0"))

            burn_path, travel_path = toolpath_to_qt_paths(toolpath)

            # Отрисовка траектории разгона/торможения (Красная)
            red_item = QtWidgets.QGraphicsPathItem(travel_path)
            red_item.setCacheMode(QtWidgets.QGraphicsItem.CacheMode.DeviceCoordinateCache)
            red_item.setPen(QtGui.QPen(QtGui.QColor("#ff0000"), 0.03, QtCore.Qt.PenStyle.SolidLine))
            self.view.scene.addItem(red_item)

            # Отрисовка работы лазера (Синяя)
            blue_item = QtWidgets.QGraphicsPathItem(burn_path)
            blue_item.setCacheMode(QtWidgets.QGraphicsItem.CacheMode.DeviceCoordinateCache)
            blue_item.setPen(
                QtGui.QPen(
                    QtGui.QColor("#0000ff"), params.step, QtCore.Qt.PenStyle.SolidLine, QtCore.Qt.PenCapStyle.RoundCap
                )
            )
            self.view.scene.addItem(blue_item)

            home_marker = QtWidgets.QGraphicsEllipseItem(-0.5, -0.5, 1, 1)
            home_marker.setBrush(QtGui.QBrush(QtGui.QColor("red")))
            self.view.scene.addItem(home_marker)

            self.btn_save.setDisabled(False)
            if self.simulator:
                self.simulator.show_burn(toolpath.gcode, self.cb_invert.isChecked(), params.step)
            extra, overflow = self._field_and_calibration_lines(bounds)
            self.status_label.setText(
                "\n".join([f"Статус: Успешно! Траектория построена ({toolpath.lines_count} строк).", *extra])
            )
            self.status_label.setStyleSheet("color: #c62828;" if overflow else "color: green;")
        except Exception as e:
            print("\n" + "=" * 40 + " Ошибка вычислений " + "=" * 40)
            traceback.print_exc()
            print("=" * 105 + "\n")
            self.status_label.setText(f"Ошибка вычислений: {str(e)} (Подробности в консоли)")
            self.status_label.setStyleSheet("color: red;")
            self.btn_save.setDisabled(True)

    def save_gcode_dialog(self):
        """Вызывает системный диалог для записи сгенерированного лазерного G-кода на диск"""
        if self.generated_gcode is None:
            return
        gerber_path = self.entry_path.text().strip()
        default_output_name = os.path.splitext(gerber_path)[0] + ".gcode"
        output_path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Сохранить лазерный G-Code", default_output_name, "G-Code Files (*.gcode *.nc);;All Files (*)"
        )
        if output_path:
            try:
                with open(output_path, "w", encoding="utf-8") as f:
                    f.write(self.generated_gcode)
                self.status_label.setText(f"Файл сохранен: {os.path.basename(output_path)}")
            except Exception as e:
                self.status_label.setText(f"Ошибка записи: {str(e)}")
