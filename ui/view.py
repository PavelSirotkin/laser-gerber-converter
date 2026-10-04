"""Графический вид платы: миллиметровая сетка, зум и HUD-прицел для базирования."""
import math

from PyQt6 import QtWidgets, QtCore, QtGui


class CrosshairOverlay(QtWidgets.QWidget):
    """Кастомное прозрачное 'стекло' поверх экрана. Рисует прицел во весь экран и HUD-координаты."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.active = False
        # Переменные для хранения текущих координат под прицелом
        self.current_x = 0.0
        self.current_y = 0.0

    def set_coordinates(self, x, y):
        """Обновляет координаты и вызывает перерисовку табло"""
        self.current_x = x
        self.current_y = y
        self.update()

    def paintEvent(self, event):
        if not self.active:
            return

        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)

        # 1. РИСУЕМ АВИАЦИОННЫЙ ПРИЦЕЛ (HUD)
        pen = QtGui.QPen(QtGui.QColor(255, 23, 68, 90), 2.0) # Полупрозрачный красный
        painter.setPen(pen)

        cx = self.width() // 2
        cy = self.height() // 2

        painter.drawLine(0, cy, self.width(), cy)
        painter.drawLine(cx, 0, cx, self.height())

        painter.drawEllipse(QtCore.QPoint(cx, cy), 8, 8)
        painter.drawEllipse(QtCore.QPoint(cx, cy), 24, 24)
        painter.drawEllipse(QtCore.QPoint(cx, cy), 48, 48)

        # 2. РИСУЕМ ПЛАВАЮЩЕЕ ТАБЛО С КООРДИНАТАМИ (В ЛЕВОМ НИЖНЕМ УГЛУ)
        hud_w, hud_h = 180, 68
        hud_x = 20
        hud_y = self.height() - hud_h - 20

        # Мягкая темная подложка
        painter.setPen(QtCore.Qt.PenStyle.NoPen)
        painter.setBrush(QtGui.QBrush(QtGui.QColor(0, 0, 0, 160)))
        painter.drawRoundedRect(QtCore.QRectF(hud_x, hud_y, hud_w, hud_h), 6.0, 6.0)

        # Пишем цифровые значения координат под прицелом
        painter.setPen(QtGui.QPen(QtGui.QColor("#00e676"))) # Ярко-зеленый люминофор
        font = painter.font()
        font.setFamily("Monospace")
        font.setPointSizeF(10.0)
        font.setBold(True)
        painter.setFont(font)

        # Текст для вывода
        hud_text = (
            f" ПРИЦЕЛ:\n"
            f" X: {self.current_x:>8.4f} мм\n"
            f" Y: {self.current_y:>8.4f} мм"
        )

        painter.drawText(
            QtCore.QRectF(hud_x + 12, hud_y + 6, hud_w - 24, hud_h - 12),
            QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignVCenter,
            hud_text
        )
        painter.end()

class LaserGraphicsView(QtWidgets.QGraphicsView):
    def __init__(self, parent=None):
        super().__init__(parent)

        self.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, False)
        self.setRenderHint(QtGui.QPainter.RenderHint.SmoothPixmapTransform, True)
        self.setViewportUpdateMode(QtWidgets.QGraphicsView.ViewportUpdateMode.SmartViewportUpdate)
        self.setCacheMode(QtWidgets.QGraphicsView.CacheModeFlag.CacheBackground)

        # Жесткое центрирование зума строго по центру экрана под перекрестием
        self.setTransformationAnchor(QtWidgets.QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self.setResizeAnchor(QtWidgets.QGraphicsView.ViewportAnchor.AnchorViewCenter)

        self.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setDragMode(QtWidgets.QGraphicsView.DragMode.ScrollHandDrag)

        self.scene = QtWidgets.QGraphicsScene(self)
        self.setScene(self.scene)

        # Инициализируем оверлей прицела и крепим его поверх вьюпорта
        self.overlay = CrosshairOverlay(self)
    def resizeEvent(self, event):
        """Растягиваем прозрачное стекло прицела вслед за изменением окна виджета"""
        super().resizeEvent(event)
        self.overlay.setGeometry(self.viewport().geometry())

    @property
    def calibration_mode(self):
        return self.overlay.active

    @calibration_mode.setter
    def calibration_mode(self, value):
        self.overlay.active = value
        self.overlay.update()

    def get_center_board_coordinates(self):
        """Вычисляет, какая точная координата Shapely-сцены сейчас находится строго по центру экрана"""
        view_center = self.viewport().rect().center()
        scene_pos = self.mapToScene(view_center)
        return scene_pos.x(), scene_pos.y()

    def wheelEvent(self, event: QtGui.QWheelEvent):
        zoom_factor = 1.05
        if event.angleDelta().y() > 0:
            self.scale(zoom_factor, zoom_factor)
        else:
            self.scale(1.0 / zoom_factor, 1.0 / zoom_factor)

    def scrollContentsBy(self, dx, dy):
        """Срабатывает при любом сдвиге сцены мышью или скроллбарами"""
        super().scrollContentsBy(dx, dy)

        if self.calibration_mode:
            main_win = self.window()
            if hasattr(main_win, 'geo_context') and main_win.geo_context:
                scene_x, scene_y = self.get_center_board_coordinates()
                self.overlay.set_coordinates(scene_x, -scene_y)

    def drawBackground(self, painter: QtGui.QPainter, rect: QtCore.QRectF):
        """Бесконечная миллиметровая сетка с аппаратной защитой от зависаний процессора"""
        painter.fillRect(rect, QtGui.QColor("#e8e8e8"))

        left = int(math.floor(rect.left()))
        right = int(math.ceil(rect.right()))
        top = int(math.floor(rect.top()))
        bottom = int(math.ceil(rect.bottom()))

        pen_grid_1mm = QtGui.QPen(QtGui.QColor("#dcdcdc"), 0, QtCore.Qt.PenStyle.SolidLine)
        pen_grid_10mm = QtGui.QPen(QtGui.QColor("#b8b8b8"), 0, QtCore.Qt.PenStyle.SolidLine)
        pen_axes = QtGui.QPen(QtGui.QColor("#808080"), 0, QtCore.Qt.PenStyle.SolidLine)

        width_mm = right - left
        show_1mm = width_mm < 150

        # ЗАЩИТА: Если экран слишком сильно отдален, увеличиваем шаг прорисовки, чтобы ЧПУ не лагало
        step_grid = 1 if width_mm < 1000 else (10 if width_mm < 5000 else 50)

        # Рисуем вертикальные линии
        for x in range(left - 10, right + 10, step_grid):
            if x % 10 == 0:
                painter.setPen(pen_grid_10mm)
                painter.drawLine(QtCore.QPointF(x, top - 10), QtCore.QPointF(x, bottom + 10))
            elif show_1mm and x % 1 == 0:
                painter.setPen(pen_grid_1mm)
                painter.drawLine(QtCore.QPointF(x, top - 10), QtCore.QPointF(x, bottom + 10))

        # Рисуем горизонтальные линии
        for y in range(top - 10, bottom + 10, step_grid):
            if y % 10 == 0:
                painter.setPen(pen_grid_10mm)
                painter.drawLine(QtCore.QPointF(left - 10, y), QtCore.QPointF(right + 10, y))
            elif show_1mm and y % 1 == 0:
                painter.setPen(pen_grid_1mm)
                painter.drawLine(QtCore.QPointF(left - 10, y), QtCore.QPointF(right + 10, y))
        # Железные оси координат (0,0) станка ЧПУ
        painter.setPen(pen_axes)
        painter.drawLine(QtCore.QPointF(0, top - 10), QtCore.QPointF(0, bottom + 10))
        painter.drawLine(QtCore.QPointF(left - 10, 0), QtCore.QPointF(right + 10, 0))

        # Рисуем шкалу линеек
        font = painter.font()
        font.setPointSizeF(2.0)
        painter.setFont(font)
        painter.setPen(QtGui.QColor("#555555"))

        # Подписи осей адаптируются под шаг сетки
        for x in range((left // 10) * 10, right + 10, 10 if step_grid == 1 else step_grid):
            if x != 0:
                painter.drawText(QtCore.QRectF(x - 5, 0.5, 10, 3), QtCore.Qt.AlignmentFlag.AlignCenter, str(x))

        for y in range((top // 10) * 10, bottom + 10, 10 if step_grid == 1 else step_grid):
            if y != 0:
                label_y = -y
                painter.drawText(QtCore.QRectF(-12.0, y - 1.5, 11.0, 3.0), QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter, str(label_y))
