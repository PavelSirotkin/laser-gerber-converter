import sys

from PyQt6 import QtWidgets

from ui.main_window import LaserConverterApp


if __name__ == "__main__":
    app = QtWidgets.QApplication(sys.argv)
    window = LaserConverterApp()
    window.show()
    sys.exit(app.exec())
