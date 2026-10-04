"""Главное окно конвертера: параметры станка, калибровка по точкам, превью и сохранение G-кода."""

import os
import traceback

from PyQt6 import QtCore, QtGui, QtWidgets

from core.calibration import CalibrationError, fit_affine
from core.gcode import GcodeParams, generate_gcode
from core.geometry import GerberGeometryContext
from core.gerber import load_gerber
from ui.qt_paths import shapely_to_qt_paths, toolpath_to_qt_paths
from ui.view import LaserGraphicsView


class LaserConverterApp(QtWidgets.QWidget):
    def __init__(self, simulator=None):
        super().__init__()
        # Окно виртуального станка (режим --test) или None
        self.simulator = simulator
        ini_path = os.path.expanduser("~/.LaserConverterApp.ini")
        self.settings = QtCore.QSettings(ini_path, QtCore.QSettings.Format.IniFormat)

        # ООП-объект геометрического контекста печатной платы
        self.geo_context = None
        self.generated_gcode = None

        # Массивы для фиксации реперов (на 4 точки)
        self.manual_file_pts = [None, None, None, None]
        self.manual_mach_pts = [None, None, None, None]
        self.manual_markers = [None, None, None, None]

        self.use_calibration = False
        self.matrix_coeffs = None

        self.init_ui()
        self.load_saved_settings()

    def init_ui(self):
        self.setWindowTitle("LaserGRBL Raster Converter & Native Visualizer (OOP-Engine)")
        self.setMinimumWidth(1150)
        self.setMinimumHeight(760)

        self.layout_horizontal = QtWidgets.QHBoxLayout()
        self.setLayout(self.layout_horizontal)

        # Левая панель управления параметрами станка
        self.left_panel = QtWidgets.QWidget()
        self.left_layout = QtWidgets.QVBoxLayout()
        self.left_panel.setLayout(self.left_layout)
        self.left_panel.setFixedWidth(430)
        self.layout_horizontal.addWidget(self.left_panel)

        # БЛОК 1: Исходный файл Gerber
        self.file_group = QtWidgets.QGroupBox("Исходный файл Gerber")
        self.file_layout = QtWidgets.QHBoxLayout()
        self.file_group.setLayout(self.file_layout)
        self.entry_path = QtWidgets.QLineEdit()
        self.entry_path.setPlaceholderText("Выберите .gbr файл...")
        self.file_layout.addWidget(self.entry_path)
        self.btn_browse = QtWidgets.QPushButton("Обзор...")
        self.btn_browse.clicked.connect(self.browse_file)
        self.file_layout.addWidget(self.btn_browse)
        self.left_layout.addWidget(self.file_group)

        # БЛОК 2: Параметры лазера и станка ЧПУ
        self.param_group = QtWidgets.QGroupBox("Параметры лазера и станка")
        self.param_grid = QtWidgets.QGridLayout()
        self.param_group.setLayout(self.param_grid)

        self.param_grid.addWidget(QtWidgets.QLabel("Режим лазера GRBL:"), 0, 0)
        self.combo_laser_mode = QtWidgets.QComboBox()
        self.combo_laser_mode.addItems(["M4 (Динамическая мощность)", "M3 (Постоянная мощность)"])
        self.param_grid.addWidget(self.combo_laser_mode, 0, 1)

        self.param_grid.addWidget(QtWidgets.QLabel("Мощность для контура (S):"), 1, 0)
        self.spin_contour_power = QtWidgets.QSpinBox()
        self.spin_contour_power.setRange(0, 1000)
        self.spin_contour_power.setValue(10)
        self.spin_contour_power.installEventFilter(self)
        self.param_grid.addWidget(self.spin_contour_power, 1, 1)

        self.param_grid.addWidget(QtWidgets.QLabel("Макс. мощность лазера (S):"), 2, 0)
        self.spin_power = QtWidgets.QSpinBox()
        self.spin_power.setRange(1, 1000)
        self.spin_power.setValue(1000)
        self.spin_power.installEventFilter(self)
        self.param_grid.addWidget(self.spin_power, 2, 1)

        self.param_grid.addWidget(QtWidgets.QLabel("Скорость гравировки (мм/мин):"), 3, 0)
        self.spin_feed = QtWidgets.QSpinBox()
        self.spin_feed.setRange(1, 30000)
        self.spin_feed.setValue(1500)
        self.spin_feed.installEventFilter(self)
        self.param_grid.addWidget(self.spin_feed, 3, 1)

        self.param_grid.addWidget(QtWidgets.QLabel("Шаг строки / Луч (мм):"), 4, 0)
        self.spin_step = QtWidgets.QDoubleSpinBox()
        self.spin_step.setDecimals(4)
        self.spin_step.setRange(0.0010, 10.0000)
        self.spin_step.setSingleStep(0.01)
        self.spin_step.setValue(0.1000)
        self.spin_step.installEventFilter(self)
        self.param_grid.addWidget(self.spin_step, 4, 1)

        self.param_grid.addWidget(QtWidgets.QLabel("Вылет каретки Overscan (мм):"), 5, 0)
        self.spin_overscan = QtWidgets.QDoubleSpinBox()
        self.spin_overscan.setDecimals(1)
        self.spin_overscan.setRange(0.0, 50.0)
        self.spin_overscan.setValue(2.0)
        self.spin_overscan.setSingleStep(0.5)
        self.spin_overscan.installEventFilter(self)
        self.param_grid.addWidget(self.spin_overscan, 5, 1)

        self.param_grid.addWidget(QtWidgets.QLabel("Точный поворот стола (град):"), 6, 0)
        self.spin_rotate = QtWidgets.QDoubleSpinBox()
        self.spin_rotate.setDecimals(4)
        self.spin_rotate.setRange(-360.000, 360.000)
        self.spin_rotate.setSingleStep(0.01)
        self.spin_rotate.setValue(0.000)
        self.spin_rotate.installEventFilter(self)
        self.param_grid.addWidget(self.spin_rotate, 6, 1)
        self.left_layout.addWidget(self.param_group)

        # БЛОК 3: Режимы работы и зеркалирование
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
        self.left_layout.addWidget(self.modes_group)

        # БЛОК 4: Оптическое смещение (офсет) камеры
        self.camera_group = QtWidgets.QGroupBox("Оптическое смещение (офсет) камеры")
        camera_main_layout = QtWidgets.QVBoxLayout()
        self.camera_group.setLayout(camera_main_layout)

        self.cb_use_camera_offset = QtWidgets.QCheckBox("Включить компенсацию смещения камеры")
        self.cb_use_camera_offset.setStyleSheet("font-weight: bold; color: #43a047;")
        self.cb_use_camera_offset.stateChanged.connect(self.toggle_camera_fields_visibility)
        camera_main_layout.addWidget(self.cb_use_camera_offset)

        # Контейнер для полей ввода офсета камеры
        self.camera_fields_widget = QtWidgets.QWidget()
        camera_grid = QtWidgets.QGridLayout(self.camera_fields_widget)
        camera_grid.setContentsMargins(0, 5, 0, 0)

        camera_grid.addWidget(QtWidgets.QLabel("Сдвиг по X (Камера -> Лазер):"), 0, 0)
        self.spin_cam_offset_x = QtWidgets.QDoubleSpinBox()
        self.spin_cam_offset_x.setDecimals(4)
        self.spin_cam_offset_x.setRange(-5000.000, 5000.000)
        self.spin_cam_offset_x.setSingleStep(0.1)
        self.spin_cam_offset_x.setValue(0.000)
        self.spin_cam_offset_x.installEventFilter(self)
        self.spin_cam_offset_x.valueChanged.connect(self.update_interactive_preview)
        camera_grid.addWidget(self.spin_cam_offset_x, 0, 1)

        camera_grid.addWidget(QtWidgets.QLabel("Сдвиг по Y (Камера -> Лазер):"), 1, 0)
        self.spin_cam_offset_y = QtWidgets.QDoubleSpinBox()
        self.spin_cam_offset_y.setDecimals(4)
        self.spin_cam_offset_y.setRange(-5000.000, 5000.000)
        self.spin_cam_offset_y.setSingleStep(0.1)
        self.spin_cam_offset_y.setValue(0.000)
        self.spin_cam_offset_y.installEventFilter(self)
        self.spin_cam_offset_y.valueChanged.connect(self.update_interactive_preview)
        camera_grid.addWidget(self.spin_cam_offset_y, 1, 1)

        camera_main_layout.addWidget(self.camera_fields_widget)
        self.camera_fields_widget.setVisible(False)
        self.left_layout.addWidget(self.camera_group)
        # БЛОК 5: Включение ручной разметки платы по точкам
        self.cb_enable_calib = QtWidgets.QCheckBox("Включить ручную разметку платы по точкам")
        self.cb_enable_calib.setStyleSheet("font-weight: bold; color: #0288d1; margin-top: 5px;")
        self.cb_enable_calib.stateChanged.connect(self.toggle_manual_calibration)
        self.left_layout.addWidget(self.cb_enable_calib)

        self.calib_group = QtWidgets.QGroupBox("Базирование по центральному прицелу")
        self.calib_layout = QtWidgets.QVBoxLayout()
        self.calib_group.setLayout(self.calib_layout)

        self.cb_use_pt4 = QtWidgets.QCheckBox("Использовать 4-ю точку для коррекции деформаций")
        self.cb_use_pt4.stateChanged.connect(self.toggle_pt4_active)
        self.calib_layout.addWidget(self.cb_use_pt4)

        # Кнопки фиксации точек станка
        self.btn_pt1 = QtWidgets.QPushButton("Зафиксировать Точку 1")
        self.btn_pt2 = QtWidgets.QPushButton("Зафиксировать Точку 2")
        self.btn_pt3 = QtWidgets.QPushButton("Зафиксировать Точку 3")
        self.btn_pt4 = QtWidgets.QPushButton("Зафиксировать Точку 4")
        self.btn_pt4.setDisabled(True)

        self.btn_pt1.clicked.connect(lambda: self.capture_point_in_crosshair(0))
        self.btn_pt2.clicked.connect(lambda: self.capture_point_in_crosshair(1))
        self.btn_pt3.clicked.connect(lambda: self.capture_point_in_crosshair(2))
        self.btn_pt4.clicked.connect(lambda: self.capture_point_in_crosshair(3))

        self.calib_layout.addWidget(self.btn_pt1)
        self.calib_layout.addWidget(self.btn_pt2)
        self.calib_layout.addWidget(self.btn_pt3)
        self.calib_layout.addWidget(self.btn_pt4)
        self.left_layout.addWidget(self.calib_group)
        self.calib_group.setVisible(False)

        # Подключение сигналов автоматического сохранения настроек
        self.combo_laser_mode.currentIndexChanged.connect(self.save_current_settings)
        self.spin_contour_power.valueChanged.connect(self.save_current_settings)
        self.spin_power.valueChanged.connect(self.save_current_settings)
        self.spin_feed.valueChanged.connect(self.save_current_settings)
        self.spin_step.valueChanged.connect(self.save_current_settings)
        self.cb_use_camera_offset.stateChanged.connect(self.save_current_settings)
        self.spin_cam_offset_x.valueChanged.connect(self.save_current_settings)
        self.spin_cam_offset_y.valueChanged.connect(self.save_current_settings)

        self.spin_rotate.valueChanged.connect(self.update_interactive_preview)
        self.spin_overscan.valueChanged.connect(self.update_interactive_preview)
        self.cb_snake.stateChanged.connect(self.update_interactive_preview)
        self.cb_invert.stateChanged.connect(self.update_interactive_preview)
        self.cb_flip_x.stateChanged.connect(self.update_interactive_preview)
        self.cb_flip_y.stateChanged.connect(self.update_interactive_preview)

        # Статус-бар и пусковые кнопки ЧПУ
        self.status_label = QtWidgets.QLabel("Статус: Ожидание выбора файла...")
        self.status_label.setStyleSheet("color: gray; font-weight: bold;")
        self.left_layout.addWidget(self.status_label)

        self.btn_convert = QtWidgets.QPushButton("Рассчитать траекторию и превью")
        self.btn_convert.setStyleSheet(
            "font-weight: bold; font-size: 13px; padding: 6px; background-color: #0288d1; color: white;"
        )
        self.btn_convert.clicked.connect(self.process_conversion)
        self.left_layout.addWidget(self.btn_convert)

        self.btn_save = QtWidgets.QPushButton("Скачать / Сохранить G-Code")
        self.btn_save.setStyleSheet(
            "font-weight: bold; font-size: 14px; padding: 10px; background-color: #2e7d32; color: white;"
        )
        self.btn_save.setDisabled(True)
        self.btn_save.clicked.connect(self.save_gcode_dialog)
        self.left_layout.addWidget(self.btn_save)
        self.left_layout.addStretch(1)

        # Правый графический холст интерактивной визуализации
        self.plot_group = QtWidgets.QGroupBox("Экран интерактивной визуализации векторов")
        self.plot_layout = QtWidgets.QVBoxLayout()
        self.plot_group.setLayout(self.plot_layout)

        self.view = LaserGraphicsView()
        self.plot_layout.addWidget(self.view)
        self.layout_horizontal.addWidget(self.plot_group)

    def eventFilter(self, watched, event):
        """Блокирует случайное изменение числовых значений колесиком мыши"""
        if event.type() == QtCore.QEvent.Type.Wheel:
            # Проверяем, является ли объект полем ввода (SpinBox)
            if isinstance(watched, (QtWidgets.QSpinBox, QtWidgets.QDoubleSpinBox)):
                # Игнорируем событие, чтобы значение не менялось
                event.ignore()
                return True  # Возвращаем True, сообщая Qt, что событие обработано и дальше идти не нужно
        return super().eventFilter(watched, event)

    def sync_geometry_context(self):
        """Синхронизирует текущие параметры интерфейса в объект геометрического контекста платы"""
        if not self.geo_context:
            return

        self.geo_context.rotate_angle = self.spin_rotate.value()
        self.geo_context.flip_x = self.cb_flip_x.isChecked()
        self.geo_context.flip_y = self.cb_flip_y.isChecked()

        self.geo_context.use_camera_offset = self.cb_use_camera_offset.isChecked()
        self.geo_context.camera_offset_x = self.spin_cam_offset_x.value()
        self.geo_context.camera_offset_y = self.spin_cam_offset_y.value()
        self.geo_context.overscan = self.spin_overscan.value()

        self.geo_context.use_calibration = self.use_calibration
        self.geo_context.matrix_coeffs = self.matrix_coeffs

    def toggle_camera_fields_visibility(self, state):
        """Показывает или скрывает поля ввода офсета камеры и обновляет интерактивное превью"""
        is_active = state == 2
        if hasattr(self, "camera_fields_widget"):
            self.camera_fields_widget.setVisible(is_active)
        self.update_interactive_preview()

    def load_saved_settings(self):
        """Загружает последнюю сохраненную сессию конфигурации станка из INI-файла"""
        self.combo_laser_mode.blockSignals(True)
        self.spin_contour_power.blockSignals(True)
        self.spin_power.blockSignals(True)
        self.spin_feed.blockSignals(True)
        self.spin_step.blockSignals(True)
        self.spin_overscan.blockSignals(True)
        self.spin_rotate.blockSignals(True)
        self.cb_snake.blockSignals(True)
        self.cb_invert.blockSignals(True)
        self.cb_flip_x.blockSignals(True)
        self.cb_flip_y.blockSignals(True)
        if hasattr(self, "cb_use_camera_offset"):
            self.cb_use_camera_offset.blockSignals(True)
        if hasattr(self, "spin_cam_offset_x"):
            self.spin_cam_offset_x.blockSignals(True)
        if hasattr(self, "spin_cam_offset_y"):
            self.spin_cam_offset_y.blockSignals(True)

        try:
            self.combo_laser_mode.setCurrentIndex(int(self.settings.value("laser_mode_idx", 0)))
            self.spin_contour_power.setValue(int(self.settings.value("contour_power", 10)))
            self.spin_power.setValue(int(self.settings.value("laser_power", 200)))
            self.spin_feed.setValue(int(self.settings.value("feed_rate", 1500)))
            self.spin_step.setValue(float(self.settings.value("raster_step", 0.1000)))
            self.spin_overscan.setValue(float(self.settings.value("overscan_dist", 2.0)))
            self.spin_rotate.setValue(float(self.settings.value("rotate_angle", 0.000)))
            self.cb_snake.setChecked(self.settings.value("cb_snake", "true") == "true")
            self.cb_invert.setChecked(self.settings.value("cb_invert", "false") == "true")
            self.cb_flip_x.setChecked(self.settings.value("cb_flip_x", "false") == "true")
            self.cb_flip_y.setChecked(self.settings.value("cb_flip_y", "false") == "true")

            if hasattr(self, "cb_use_camera_offset"):
                self.cb_use_camera_offset.setChecked(self.settings.value("use_camera_offset", "false") == "true")
            if hasattr(self, "spin_cam_offset_x"):
                self.spin_cam_offset_x.setValue(float(self.settings.value("cam_offset_x", 0.000)))
            if hasattr(self, "spin_cam_offset_y"):
                self.spin_cam_offset_y.setValue(float(self.settings.value("cam_offset_y", 0.000)))

            if hasattr(self, "cb_use_camera_offset"):
                state = 2 if self.cb_use_camera_offset.isChecked() else 0
                self.toggle_camera_fields_visibility(state)

            # АВТОЗАГРУЗКА МАТРИЦЫ 3-Х ТОЧЕК:
            if self.settings.value("calib_matrix_active", "false") == "true":
                try:
                    m11 = float(self.settings.value("mat_m11", 1.0))
                    m21 = float(self.settings.value("mat_m21", 0.0))
                    m12 = float(self.settings.value("mat_m12", 0.0))
                    m22 = float(self.settings.value("mat_m22", 1.0))
                    dx = float(self.settings.value("mat_dx", 0.0))
                    dy = float(self.settings.value("mat_dy", 0.0))

                    self.matrix_coeffs = (m11, m21, m12, m22, dx, dy)
                    self.use_calibration = True

                    buttons = [self.btn_pt1, self.btn_pt2, self.btn_pt3, self.btn_pt4]
                    for idx in range(4):
                        fx = self.settings.value(f"pt_file_{idx}_x")
                        fy = self.settings.value(f"pt_file_{idx}_y")
                        mx = self.settings.value(f"pt_mach_{idx}_x")
                        my = self.settings.value(f"pt_mach_{idx}_y")
                        has_cam = self.settings.value(f"pt_mach_{idx}_cam", "false") == "true"

                        if fx is not None and mx is not None:
                            self.manual_file_pts[idx] = (float(fx), float(fy))
                            self.manual_mach_pts[idx] = (float(mx), float(my), has_cam)
                            cam_label = " (+Камера)" if has_cam else ""
                            buttons[idx].setText(
                                f"Т{idx + 1}: Загружено -> Ст({float(mx):.2f}, {float(my):.2f}){cam_label}"
                            )
                            buttons[idx].setStyleSheet("background-color: #c8e6c9; font-weight: bold;")

                    self.cb_enable_calib.setChecked(True)
                except:
                    pass

        except Exception as e:
            print(f"Ошибка инициализации INI: {str(e)}")
        finally:
            self.combo_laser_mode.blockSignals(False)
            self.spin_contour_power.blockSignals(False)
            self.spin_power.blockSignals(False)
            self.spin_feed.blockSignals(False)
            self.spin_step.blockSignals(False)
            self.spin_overscan.blockSignals(False)
            self.spin_rotate.blockSignals(False)
            self.cb_snake.blockSignals(False)
            self.cb_invert.blockSignals(False)
            self.cb_flip_x.blockSignals(False)
            self.cb_flip_y.blockSignals(False)
            if hasattr(self, "cb_use_camera_offset"):
                self.cb_use_camera_offset.blockSignals(False)
            if hasattr(self, "spin_cam_offset_x"):
                self.spin_cam_offset_x.blockSignals(False)
            if hasattr(self, "spin_cam_offset_y"):
                self.spin_cam_offset_y.blockSignals(False)

    def save_current_settings(self):
        """Мгновенно синхронизирует и перезаписывает параметры станка в INI-файл"""
        self.settings.setValue("laser_mode_idx", self.combo_laser_mode.currentIndex())
        self.settings.setValue("contour_power", self.spin_contour_power.value())
        self.settings.setValue("laser_power", self.spin_power.value())
        self.settings.setValue("feed_rate", self.spin_feed.value())
        self.settings.setValue("raster_step", self.spin_step.value())
        self.settings.setValue("overscan_dist", self.spin_overscan.value())
        self.settings.setValue("rotate_angle", self.spin_rotate.value())
        self.settings.setValue("cb_snake", "true" if self.cb_snake.isChecked() else "false")
        self.settings.setValue("cb_invert", "true" if self.cb_invert.isChecked() else "false")
        self.settings.setValue("cb_flip_x", "true" if self.cb_flip_x.isChecked() else "false")
        self.settings.setValue("cb_flip_y", "true" if self.cb_flip_y.isChecked() else "false")
        if hasattr(self, "cb_use_camera_offset"):
            self.settings.setValue("use_camera_offset", "true" if self.cb_use_camera_offset.isChecked() else "false")
        if hasattr(self, "spin_cam_offset_x"):
            self.settings.setValue("cam_offset_x", self.spin_cam_offset_x.value())
        if hasattr(self, "spin_cam_offset_y"):
            self.settings.setValue("cam_offset_y", self.spin_cam_offset_y.value())
        self.settings.sync()

    def closeEvent(self, event):
        """Гарантированное сохранение настроек при закрытии окна"""
        self.save_current_settings()
        event.accept()

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
            self.view.fitInView(self.view.scene.itemsBoundingRect(), QtCore.Qt.AspectRatioMode.KeepAspectRatio)
            if self.simulator:
                self.simulator.set_board(self.geo_context.raw_geometries)
        except Exception as e:
            print("\n" + "=" * 40 + " Ошибка загрузки Gerber " + "=" * 40)
            traceback.print_exc()
            print("=" * 105 + "\n")
            self.geo_context = None
            self.status_label.setText(f"Ошибка загрузки Gerber: {str(e)}")
            self.status_label.setStyleSheet("color: red;")

    def toggle_manual_calibration(self, state):
        """Включение/выключение режима центрального HUD-прицела станка и панели реперов"""
        is_active = state == 2
        if hasattr(self, "calib_group"):
            self.calib_group.setVisible(is_active)

        if hasattr(self, "view"):
            self.view.calibration_mode = is_active
            if is_active:
                self.view.scrollContentsBy(0, 0)
            self.view.viewport().update()

        if not is_active:
            self.use_calibration = False
            self.manual_file_pts = [None, None, None, None]
            self.manual_mach_pts = [None, None, None, None]
            self.redraw_calibration_markers()

            if hasattr(self, "btn_pt1"):
                self.btn_pt1.setText("Зафиксировать Точку 1")
            if hasattr(self, "btn_pt2"):
                self.btn_pt2.setText("Зафиксировать Точку 2")
            if hasattr(self, "btn_pt3"):
                self.btn_pt3.setText("Зафиксировать Точку 3")
            if hasattr(self, "btn_pt4"):
                self.btn_pt4.setText("Зафиксировать Точку 4")

            for btn in [self.btn_pt1, self.btn_pt2, self.btn_pt3, self.btn_pt4]:
                if hasattr(btn, "setStyleSheet"):
                    btn.setStyleSheet("")

            self.settings.setValue("calib_matrix_active", "false")
            self.settings.sync()

            if self.geo_context:
                self.update_interactive_preview()

    def toggle_pt4_active(self, state):
        """Включение/выключение использования опциональной 4-й точки"""
        is_active = state == 2
        self.btn_pt4.setEnabled(is_active)

        if not is_active:
            self.manual_file_pts[3] = None
            self.manual_mach_pts[3] = None
            self.redraw_calibration_markers()
            self.btn_pt4.setText("Зафиксировать Точку 4")
            self.btn_pt4.setStyleSheet("")

            # Проверяем готовность первых 3 точек перед автоматическим пересчетом матрицы
            if self.manual_file_pts and self.manual_mach_pts:
                if all(pt is not None for pt in self.manual_file_pts[:3]) and all(
                    pt is not None for pt in self.manual_mach_pts[:3]
                ):
                    self.calculate_manual_affine_matrix()

    def capture_point_in_crosshair(self, point_idx):
        """Фиксирует координаты векторов: визуально - сетка экрана, скрыто для МНК - файл"""
        if not self.geo_context:
            QtWidgets.QMessageBox.warning(self, "Внимание", "Сначала загрузите Gerber файл!")
            return

        buttons = [self.btn_pt1, self.btn_pt2, self.btn_pt3, self.btn_pt4]
        scene_x, scene_y = self.view.get_center_board_coordinates()

        # Считываем смещение камеры из интерфейса
        is_camera_active = hasattr(self, "cb_use_camera_offset") and self.cb_use_camera_offset.isChecked()
        cam_x = self.spin_cam_offset_x.value() if (is_camera_active and hasattr(self, "spin_cam_offset_x")) else 0.0
        cam_y = self.spin_cam_offset_y.value() if (is_camera_active and hasattr(self, "spin_cam_offset_y")) else 0.0

        # Локальные координаты точки на плате для МНК. Экран может показывать плату как со
        # смещением камеры, так и уже с примененной калибровкой — снимаем ровно то, что на экране.
        self.sync_geometry_context()
        try:
            self.manual_file_pts[point_idx] = self.geo_context.display_to_local(scene_x, -scene_y)
        except ValueError as e:
            QtWidgets.QMessageBox.warning(self, "Внимание", str(e))
            return
        self.redraw_calibration_markers()
        self.status_label.setText(f"Статус: Точка {point_idx + 1} зафиксирована в прицеле.")
        self.status_label.setStyleSheet("color: #0288d1;")

        try:
            dialog = QtWidgets.QDialog(self)
            dialog.setWindowTitle(f"Координаты станка для Точки {point_idx + 1}")
            dialog.setMinimumWidth(340)
            dialog_layout = QtWidgets.QVBoxLayout(dialog)

            info_text = (
                f"Вы навели прицел на репер платы.\n"
                f"Координата прицела на сетке (то, что видите):\n"
                f"   X = {scene_x:.4f} мм\n"
                f"   Y = {-scene_y:.4f} мм\n\n"
                f"Задайте точные координаты станка ЧПУ (DRO):"
            )
            dialog_layout.addWidget(QtWidgets.QLabel(info_text))

            grid = QtWidgets.QGridLayout()
            dialog_layout.addLayout(grid)

            grid.addWidget(QtWidgets.QLabel("Координата X станка (мм):"), 0, 0)
            spin_x = QtWidgets.QDoubleSpinBox()
            spin_x.setDecimals(4)
            spin_x.setRange(-9999.000, 9999.000)
            spin_x.setSingleStep(0.1)
            spin_x.installEventFilter(self)
            grid.addWidget(spin_x, 0, 1)

            grid.addWidget(QtWidgets.QLabel("Координата Y станка (мм):"), 1, 0)
            spin_y = QtWidgets.QDoubleSpinBox()
            spin_y.setDecimals(4)
            spin_y.setRange(-9999.000, 9999.000)
            spin_y.setSingleStep(0.1)
            spin_y.installEventFilter(self)
            grid.addWidget(spin_y, 1, 1)

            # Подставляем координаты прицела как стартовое значение
            spin_x.setValue(scene_x)
            spin_y.setValue(-scene_y)

            if self.simulator:
                # Режим --test: координаты станка берем у виртуального станка, как оператор с экрана DRO
                def take_simulator_dro():
                    dro = self.simulator.dro()
                    if dro:
                        spin_x.setValue(dro[0])
                        spin_y.setValue(dro[1])

                take_simulator_dro()
                btn_dro = QtWidgets.QPushButton("Взять DRO из симулятора")
                btn_dro.clicked.connect(take_simulator_dro)
                dialog_layout.addWidget(btn_dro)

            # Чекбокс автоматического добавления смещения камеры
            cb_add_cam = QtWidgets.QCheckBox("Учитывать смещение камеры при вводе")
            cb_add_cam.setChecked(is_camera_active)
            dialog_layout.addWidget(cb_add_cam)

            button_box = QtWidgets.QDialogButtonBox(
                QtWidgets.QDialogButtonBox.StandardButton.Ok | QtWidgets.QDialogButtonBox.StandardButton.Cancel, dialog
            )
            button_box.accepted.connect(dialog.accept)
            button_box.rejected.connect(dialog.reject)
            dialog_layout.addWidget(button_box)

            if dialog.exec() == QtWidgets.QDialog.DialogCode.Accepted:
                mach_x = spin_x.value()
                mach_y = spin_y.value()

                if cb_add_cam.isChecked():
                    mach_x += cam_x
                    mach_y += cam_y

                self.manual_mach_pts[point_idx] = (mach_x, mach_y, cb_add_cam.isChecked())

                cam_label = " (+Камера)" if cb_add_cam.isChecked() else ""
                buttons[point_idx].setText(
                    f"Т{point_idx + 1}: Сетка({scene_x:.4f}, {-scene_y:.4f}) -> Ст({mach_x:.4f}, {mach_y:.4f}){cam_label}"
                )
                buttons[point_idx].setStyleSheet("background-color: #c8e6c9; font-weight: bold;")

                p1_3_ready = all(pt is not None for pt in self.manual_file_pts[:3]) and all(
                    pt is not None for pt in self.manual_mach_pts[:3]
                )
                p4_enabled = self.cb_use_pt4.isChecked()
                p4_ready = (
                    self.manual_file_pts[3] is not None and self.manual_mach_pts[3] is not None if p4_enabled else True
                )

                if p1_3_ready and p4_ready:
                    self.calculate_manual_affine_matrix()
            else:
                self.manual_file_pts[point_idx] = None
                self.redraw_calibration_markers()
                buttons[point_idx].setText(f"Зафиксировать Точку {point_idx + 1}")
                buttons[point_idx].setStyleSheet("")
        except Exception as e:
            print("\n" + "=" * 40 + " Сбой работы окна ввода " + "=" * 40)
            traceback.print_exc()
            print("=" * 105 + "\n")
            QtWidgets.QMessageBox.critical(self, "Ошибка", f"Сбой работы окна ввода: {str(e)}")

    def redraw_calibration_markers(self):
        """Рисует отметки реперов по их локальным координатам платы в текущей системе экрана.
        После расчета матрицы отметки должны лечь точно на реперы — это визуальная проверка калибровки."""
        for marker in self.manual_markers:
            try:
                if marker is not None and marker.scene() is self.view.scene:
                    self.view.scene.removeItem(marker)
            except RuntimeError:
                pass  # объект уже удален вместе со scene.clear()
        self.manual_markers = [None, None, None, None]
        if not self.geo_context or not self.cb_enable_calib.isChecked():
            return

        self.sync_geometry_context()
        colors = ["#ff1744", "#2979ff", "#00e676", "#e040fb"]
        for idx, pt in enumerate(self.manual_file_pts):
            if pt is None:
                continue
            x, y = self.geo_context.local_to_display(*pt)
            marker = QtWidgets.QGraphicsEllipseItem(x - 0.4, -y - 0.4, 0.8, 0.8)
            marker.setBrush(QtGui.QBrush(QtGui.QColor(colors[idx])))
            marker.setPen(QtGui.QPen(QtGui.QColor("#ffffff"), 0.15))
            marker.setZValue(10)
            self.view.scene.addItem(marker)
            self.manual_markers[idx] = marker

    def calculate_manual_affine_matrix(self):
        """Расчет аффинной матрицы, автоматически адаптирующийся под 3 или 4 точки методом МНК"""
        try:
            valid_indices = [
                i for i in range(4) if self.manual_file_pts[i] is not None and self.manual_mach_pts[i] is not None
            ]
            if len(valid_indices) < 3:
                return

            try:
                coeffs = fit_affine(
                    [self.manual_file_pts[i] for i in valid_indices],
                    [self.manual_mach_pts[i][:2] for i in valid_indices],
                )
            except CalibrationError as e:
                QtWidgets.QMessageBox.warning(self, "Внимание", str(e))
                return
            m11, m21, m12, m22, dx, dy = coeffs

            self.matrix_coeffs = coeffs
            self.use_calibration = True

            pts_count = len(valid_indices)
            self.status_label.setText(f"Статус: Базирование выполнено успешно по {pts_count} точкам!")
            self.status_label.setStyleSheet("color: green; font-weight: bold;")

            self.update_interactive_preview()

            # АВТОСОХРАНЕНИЕ КАЛИБРОВКИ В INI:
            self.settings.setValue("calib_matrix_active", "true")
            self.settings.setValue("mat_m11", float(m11))
            self.settings.setValue("mat_m21", float(m21))
            self.settings.setValue("mat_m12", float(m12))
            self.settings.setValue("mat_m22", float(m22))
            self.settings.setValue("mat_dx", float(dx))
            self.settings.setValue("mat_dy", float(dy))

            # Сохраняем текстовые подписи кнопок
            for idx in valid_indices:
                if self.manual_file_pts[idx] and self.manual_mach_pts[idx]:
                    self.settings.setValue(f"pt_file_{idx}_x", float(self.manual_file_pts[idx][0]))
                    self.settings.setValue(f"pt_file_{idx}_y", float(self.manual_file_pts[idx][1]))
                    self.settings.setValue(f"pt_mach_{idx}_x", float(self.manual_mach_pts[idx][0]))
                    self.settings.setValue(f"pt_mach_{idx}_y", float(self.manual_mach_pts[idx][1]))
                    if len(self.manual_mach_pts[idx]) > 2:
                        self.settings.setValue(
                            f"pt_mach_{idx}_cam", "true" if self.manual_mach_pts[idx][2] else "false"
                        )
            self.settings.sync()

        except Exception as e:
            print("\n" + "=" * 40 + " ОШИБКА АФФИННОЙ МАТРИЦЫ " + "=" * 40)
            traceback.print_exc()
            print("=" * 105 + "\n")
            QtWidgets.QMessageBox.critical(
                self, "Ошибка расчета", f"Не удалось рассчитать коэффициенты трансформации: {str(e)}"
            )
            self.use_calibration = False

    def update_interactive_preview(self):
        """Интерактивное превью. Рисует ту же геометрию, что пойдет в G-код (единый конвейер трансформаций)"""
        if not self.geo_context:
            return
        self.sync_geometry_context()
        ovr = self.spin_overscan.value()
        inv_mode = self.cb_invert.isChecked()

        try:
            self.view.scene.clear()
            self.manual_markers = [None, None, None, None]

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

            # Выводим оператору физические координаты линеек станка
            self.status_label.setText(
                (
                    f"Статус: Геометрия готова.\n"
                    f"Размер из файла: {rx_max - rx_min:.2f} x {ry_max - ry_min:.2f} мм\n"
                    f"Размер маски платы (на станке): {min_x:.2f} ... {max_x:.2f} мм\n"
                    f"Габарит хода башки X (ЧПУ DRO): [ {min_x - ovr:.2f} ... {max_x + ovr:.2f} ] мм"
                )
            )
            self.status_label.setStyleSheet("color: #2e7d32; font-weight: bold;")

        except Exception as e:
            print("\n" + "=" * 40 + " ОШИБКА ПРЕВЬЮ " + "=" * 40)
            traceback.print_exc()
            self.status_label.setText(f"Ошибка визуализации: {str(e)}")
            self.status_label.setStyleSheet("color: red;")

    def process_conversion(self):
        """Расчет траекторий сканирования и генерация G-кода через ООП-контекст геометрии"""
        if not self.geo_context:
            return
        self.save_current_settings()

        self.view.calibration_mode = False
        self.view.overlay.update()

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
        )

        try:
            self.sync_geometry_context()

            # Геометрия прожига в координатах станка: в обычном режиме прижата к (0,0), при калибровке — по реперам
            burn_geom, bounds = self.geo_context.get_burn_geometry(invert=self.cb_invert.isChecked())
            if burn_geom is None:
                return

            toolpath = generate_gcode(burn_geom, bounds, params)
            self.generated_gcode = toolpath.gcode
            self.view.scene.clear()
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
            self.status_label.setText(f"Статус: Успешно! Траектория построена ({toolpath.lines_count} строк).")
            self.status_label.setStyleSheet("color: green;")
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
