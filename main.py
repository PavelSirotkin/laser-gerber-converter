import argparse
import sys

from PyQt6 import QtWidgets

from ui.main_window import LaserConverterApp


def parse_args():
    parser = argparse.ArgumentParser(description="Конвертер Gerber в растровый G-код для лазерных станков GRBL")
    parser.add_argument(
        "--test",
        action="store_true",
        help="проверка без станка: открыть окно виртуального станка с платой в скрытом положении",
    )
    parser.add_argument(
        "--seed", type=int, default=None, help="номер укладки платы в режиме --test (для повторяемой проверки)"
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    app = QtWidgets.QApplication(sys.argv)

    simulator = None
    if args.test:
        from ui.simulator_window import SimulatorWindow

        simulator = SimulatorWindow(seed=args.seed)

    window = LaserConverterApp(simulator=simulator)
    window.show()

    if simulator:
        # Поле и смещение камеры виртуального станка = заданным в программе (их можно поменять в окне симулятора)
        simulator.spin_cam_x.setValue(window.spin_cam_offset_x.value())
        simulator.spin_cam_y.setValue(window.spin_cam_offset_y.value())
        simulator.spin_field_w.setValue(window.spin_field_w.value())
        simulator.spin_field_h.setValue(window.spin_field_h.value())
        simulator.show()
    sys.exit(app.exec())
