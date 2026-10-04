"""Окно «Станок (симуляция)» для режима --test: виртуальный стол с платой в скрытом положении.

ЛКМ наводит камеру или лазер (голова станка едет следом и упирается в край поля), колесо — зум,
ПКМ — сдвиг вида, стрелки — шаг головы. После расчета траектории окно «прожигает» G-код
поверх настоящего положения платы и показывает, легли ли линии на медь.
"""

import random

from PyQt6 import QtCore, QtGui, QtWidgets

from core.simulator import MachineConfig, VirtualMachine
from ui.qt_paths import shapely_to_qt_paths
from ui.view import LaserGraphicsView


def _segments_path(segments):
    path = QtGui.QPainterPath()
    for x0, y0, x1, y1 in segments:
        path.moveTo(x0, -y0)
        path.lineTo(x1, -y1)
    return path


class SimulatorView(LaserGraphicsView):
    """Вид стола: ЛКМ — навести камеру/лазер, ПКМ — сдвиг вида, стрелки — шаг головы"""

    camera_requested = QtCore.pyqtSignal(float, float)
    jog_requested = QtCore.pyqtSignal(float, float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setDragMode(QtWidgets.QGraphicsView.DragMode.NoDrag)
        self.setFocusPolicy(QtCore.Qt.FocusPolicy.StrongFocus)
        self._pan_from = None

    def _request_camera(self, event):
        p = self.mapToScene(event.position().toPoint())
        self.camera_requested.emit(p.x(), -p.y())

    def mousePressEvent(self, event):
        if event.button() == QtCore.Qt.MouseButton.LeftButton:
            self._request_camera(event)
        elif event.button() == QtCore.Qt.MouseButton.RightButton:
            self._pan_from = event.position()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if event.buttons() & QtCore.Qt.MouseButton.LeftButton:
            self._request_camera(event)
        elif self._pan_from is not None and event.buttons() & QtCore.Qt.MouseButton.RightButton:
            delta = event.position() - self._pan_from
            self._pan_from = event.position()
            self.horizontalScrollBar().setValue(self.horizontalScrollBar().value() - int(delta.x()))
            self.verticalScrollBar().setValue(self.verticalScrollBar().value() - int(delta.y()))
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self._pan_from = None
        super().mouseReleaseEvent(event)

    def contextMenuEvent(self, event):
        event.accept()  # ПКМ занята сдвигом вида

    def keyPressEvent(self, event):
        mods = event.modifiers()
        step = (
            1.0
            if mods & QtCore.Qt.KeyboardModifier.ShiftModifier
            else 0.01
            if mods & QtCore.Qt.KeyboardModifier.ControlModifier
            else 0.1
        )
        moves = {
            QtCore.Qt.Key.Key_Left: (-step, 0),
            QtCore.Qt.Key.Key_Right: (step, 0),
            QtCore.Qt.Key.Key_Up: (0, step),
            QtCore.Qt.Key.Key_Down: (0, -step),
        }
        if event.key() in moves:
            self.jog_requested.emit(*moves[event.key()])
        else:
            super().keyPressEvent(event)


class SimulatorWindow(QtWidgets.QWidget):
    def __init__(self, camera_offset=(0.0, 0.0), seed=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Станок (симуляция) — плата лежит в скрытом положении")
        self.setMinimumSize(1000, 650)
        self.raw_geometries = None
        self.vm = None
        self.blocked = False  # голова уперлась в край поля и не дошла до точки наведения
        self.seed = seed if seed is not None else random.randrange(1, 10**6)
        self._burn_items = []

        layout = QtWidgets.QHBoxLayout(self)
        panel = QtWidgets.QWidget()
        panel.setFixedWidth(330)
        form = QtWidgets.QVBoxLayout(panel)
        layout.addWidget(panel)

        machine_group = QtWidgets.QGroupBox("Виртуальный станок")
        grid = QtWidgets.QGridLayout(machine_group)

        def spin(row, label, value, lo, hi, decimals=2, step=0.1):
            grid.addWidget(QtWidgets.QLabel(label), row, 0)
            box = QtWidgets.QDoubleSpinBox()
            box.setDecimals(decimals)
            box.setRange(lo, hi)
            box.setSingleStep(step)
            box.setValue(value)
            box.valueChanged.connect(self.relayout)
            grid.addWidget(box, row, 1)
            return box

        self.spin_field_w = spin(0, "Поле X (мм):", 165.0, 10, 2000, 1, 1)
        self.spin_field_h = spin(1, "Поле Y (мм):", 95.0, 10, 2000, 1, 1)
        self.spin_cam_x = spin(2, "Камера X (Лазер -> Камера):", camera_offset[0], -500, 500, 4)
        self.spin_cam_y = spin(3, "Камера Y (Лазер -> Камера):", camera_offset[1], -500, 500, 4)
        self.spin_rotation = spin(4, "Макс. поворот платы (°):", 5.0, 0, 45, 1, 0.5)

        self.cb_mirror = QtWidgets.QCheckBox("Плата нижним слоем (зеркально по X)")
        self.cb_mirror.stateChanged.connect(self.relayout)
        grid.addWidget(self.cb_mirror, 5, 0, 1, 2)

        self.seed_label = QtWidgets.QLabel()
        grid.addWidget(self.seed_label, 6, 0)
        btn_new = QtWidgets.QPushButton("Новая укладка")
        btn_new.clicked.connect(self.new_layout)
        grid.addWidget(btn_new, 6, 1)
        form.addWidget(machine_group)

        aim_group = QtWidgets.QGroupBox("ЛКМ наводит")
        aim_layout = QtWidgets.QHBoxLayout(aim_group)
        self.rb_aim_camera = QtWidgets.QRadioButton("камеру")
        self.rb_aim_laser = QtWidgets.QRadioButton("лазер (указатель)")
        self.rb_aim_camera.setChecked(True)
        aim_layout.addWidget(self.rb_aim_camera)
        aim_layout.addWidget(self.rb_aim_laser)
        form.addWidget(aim_group)

        self.dro_label = QtWidgets.QLabel()
        self.dro_label.setStyleSheet("font-family: monospace; font-size: 13px; font-weight: bold;")
        form.addWidget(self.dro_label)

        self.warning_label = QtWidgets.QLabel()
        self.warning_label.setWordWrap(True)
        self.warning_label.setStyleSheet("color: #c62828; font-weight: bold;")
        form.addWidget(self.warning_label)

        self.report_label = QtWidgets.QLabel("Прожиг: рассчитайте траекторию в основном окне.")
        self.report_label.setWordWrap(True)
        form.addWidget(self.report_label)

        hint = QtWidgets.QLabel(
            "ЛКМ — навести камеру или лазер (голова едет следом)\n"
            "Стрелки — шаг 0.1 мм, Shift — 1 мм, Ctrl — 0.01 мм\n"
            "Колесо — зум, ПКМ — сдвиг вида\n\n"
            "Розовая зона — мертвая зона камеры:\n"
            "туда камеру навести нельзя, реперы там\n"
            "наводите лазером (без смещения камеры).\n\n"
            "Положение платы скрыто от основного окна —\n"
            "оно узнает его только через калибровку."
        )
        hint.setStyleSheet("color: gray;")
        form.addWidget(hint)
        form.addStretch(1)

        self.view = SimulatorView()
        self.view.camera_requested.connect(self.aim)
        self.view.jog_requested.connect(self.jog)
        layout.addWidget(self.view)

        self._update_dro()

    # --- укладка платы ---

    def config(self):
        return MachineConfig(
            self.spin_field_w.value(), self.spin_field_h.value(), (self.spin_cam_x.value(), self.spin_cam_y.value())
        )

    def set_board(self, raw_geometries):
        """Новая плата из основного окна — кладем ее на стол"""
        self.raw_geometries = list(raw_geometries)
        self.relayout()
        self.view.fitInView(
            QtCore.QRectF(
                -10, -self.spin_field_h.value() - 10, self.spin_field_w.value() + 20, self.spin_field_h.value() + 20
            ),
            QtCore.Qt.AspectRatioMode.KeepAspectRatio,
        )

    def new_layout(self):
        self.seed = random.randrange(1, 10**6)
        self.relayout()

    def relayout(self):
        self.seed_label.setText(f"Укладка №{self.seed}")
        if not self.raw_geometries:
            return
        self.vm = VirtualMachine(
            self.raw_geometries,
            config=self.config(),
            seed=self.seed,
            max_rotation=self.spin_rotation.value(),
            mirror_x=self.cb_mirror.isChecked(),
        )
        self._build_scene()
        self.report_label.setText("Прожиг: рассчитайте траекторию в основном окне.")
        self._update_dro()

    def _build_scene(self):
        scene = self.view.scene
        scene.clear()
        self._burn_items = []
        w, h = self.vm.config.field_w, self.vm.config.field_h

        field_item = scene.addRect(
            QtCore.QRectF(0, -h, w, h),
            QtGui.QPen(QtGui.QColor("#555555"), 0),
            QtGui.QBrush(QtGui.QColor(255, 255, 255, 140)),
        )
        field_item.setZValue(-2)

        dead, _ = shapely_to_qt_paths(self.vm.camera_dead_zone())
        dead_item = scene.addPath(
            dead, QtGui.QPen(QtCore.Qt.PenStyle.NoPen), QtGui.QBrush(QtGui.QColor(255, 82, 82, 50))
        )
        dead_item.setZValue(-1)

        outline, _ = shapely_to_qt_paths(self.vm.board_outline)
        scene.addPath(outline, QtGui.QPen(QtGui.QColor("#33691e"), 0), QtGui.QBrush(QtGui.QColor("#c5e1a5")))
        copper, copper_lines = shapely_to_qt_paths(self.vm.copper)
        scene.addPath(copper, QtGui.QPen(QtCore.Qt.PenStyle.NoPen), QtGui.QBrush(QtGui.QColor("#d4883c")))
        if not copper_lines.isEmpty():
            scene.addPath(copper_lines, QtGui.QPen(QtGui.QColor("#d4883c"), 0))

        # Маркеры постоянного экранного размера: лазер (красный крест) и прицел камеры (синий)
        laser = QtGui.QPainterPath()
        laser.moveTo(-7, 0)
        laser.lineTo(7, 0)
        laser.moveTo(0, -7)
        laser.lineTo(0, 7)
        self.laser_item = scene.addPath(laser, QtGui.QPen(QtGui.QColor("#d50000"), 2))
        camera = QtGui.QPainterPath()
        camera.addEllipse(QtCore.QPointF(0, 0), 10, 10)
        camera.moveTo(-18, 0)
        camera.lineTo(18, 0)
        camera.moveTo(0, -18)
        camera.lineTo(0, 18)
        self.camera_item = scene.addPath(camera, QtGui.QPen(QtGui.QColor("#1565c0"), 1.5))
        for item in (self.laser_item, self.camera_item):
            item.setFlag(QtWidgets.QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations)
            item.setZValue(10)
        self.link_item = scene.addLine(0, 0, 0, 0, QtGui.QPen(QtGui.QColor("#1565c0"), 0, QtCore.Qt.PenStyle.DashLine))
        self.link_item.setZValue(9)

    # --- голова станка ---

    @property
    def aim_by_camera(self):
        """True — наведение камерой (DRO дополняется смещением камеры), False — лазером-указателем"""
        return self.rb_aim_camera.isChecked()

    def aim(self, x, y):
        """Навести на точку стола камеру или лазер — в зависимости от переключателя"""
        if self.vm:
            reached = self.vm.move_camera_to(x, y) if self.aim_by_camera else self.vm.move_laser_to(x, y)
            self.blocked = not reached
            self._update_dro()

    def move_camera(self, x, y):
        if self.vm:
            self.blocked = not self.vm.move_camera_to(x, y)
            self._update_dro()

    def jog(self, dx, dy):
        if self.vm:
            self.blocked = not self.vm.move_laser_to(self.vm.laser_x + dx, self.vm.laser_y + dy)
            self._update_dro()

    def warning(self):
        """Предупреждение о наведении или пустая строка"""
        if not self.blocked:
            return ""
        if self.aim_by_camera:
            return (
                "Камера не достает до точки: она в мертвой зоне, голова уперлась в край поля. "
                "Под прицелом не та точка — наведите этот репер лазером."
            )
        return "Голова уперлась в край поля: точка вне рабочего поля станка."

    def dro(self):
        """Показания станка (положение лазера) или None, если плата не загружена"""
        return self.vm.dro if self.vm else None

    def _update_dro(self):
        if not self.vm:
            self.dro_label.setText("Загрузите Gerber в основном окне")
            return
        lx, ly = self.vm.dro
        cx, cy = self.vm.camera_position
        self.laser_item.setPos(lx, -ly)
        self.camera_item.setPos(cx, -cy)
        self.link_item.setLine(lx, -ly, cx, -cy)
        self.dro_label.setText(f"DRO (лазер): X {lx:9.4f}  Y {ly:9.4f}\nКамера:      X {cx:9.4f}  Y {cy:9.4f}")
        self.warning_label.setText(self.warning())

    # --- прожиг ---

    def show_burn(self, gcode, invert, step):
        """Прожигает G-код на виртуальном столе и показывает результат"""
        if not self.vm:
            return
        for item in self._burn_items:
            self.view.scene.removeItem(item)
        report = self.vm.burn(gcode, invert=invert, step=step)

        contour = self.view.scene.addPath(
            _segments_path(report.contour_segments), QtGui.QPen(QtGui.QColor("#ff6d00"), 0, QtCore.Qt.PenStyle.DashLine)
        )
        burn = self.view.scene.addPath(
            _segments_path(report.burn_segments), QtGui.QPen(QtGui.QColor(13, 71, 161, 170), step)
        )
        self._burn_items = [contour, burn]
        for item in self._burn_items:
            item.setZValue(5)

        ok = report.max_miss < step / 2 and report.out_of_field == 0 and report.g0_count == 0
        color = "#2e7d32" if ok else "#c62828"
        verdict = "Прожиг лег на плату." if ok else "Есть проблемы — смотрите цифры."
        self.report_label.setText(
            f"<b style='color:{color}'>{verdict}</b><br>" + report.summary().replace("\n", "<br>")
        )
        return report
