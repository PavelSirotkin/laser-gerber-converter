import math
import os
import sys
from functools import lru_cache

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAMPLES = os.path.join(ROOT, "samples")
sys.path.insert(0, ROOT)

from core.gcode import GcodeParams  # noqa: E402
from core.geometry import GerberGeometryContext  # noqa: E402
from core.gerber import load_gerber  # noqa: E402

SAMPLE_FILES = ["test.gbr", "test70x70.gbr", "test140x90-B_Cu.gbr"]

# Известная «истинная» привязка платы к станку: поворот 3°, масштаб 1.002, сдвиг
_a, _k = math.radians(3.0), 1.002
CALIB = (_k * math.cos(_a), -_k * math.sin(_a), _k * math.sin(_a), _k * math.cos(_a), 12.5, 7.25)

BASE = dict(
    laser_mode="M4",
    contour_power=10,
    power=1000,
    feed=1500,
    step=0.1,
    overscan=2.0,
    rotate=0.0,
    snake=True,
    invert=False,
    flip_x=False,
    flip_y=False,
    cam=None,
    calib=None,
)

# Конфигурации эталонного G-кода (tests/data/golden_gcode.json)
CONFIGS = {
    "base": {},
    "invert": dict(invert=True),
    "flipx_rot30_nosnake": dict(flip_x=True, rotate=30.0, snake=False),
    "flipy_rotm12_invert": dict(flip_y=True, rotate=-12.5, invert=True),
    "camera": dict(cam=(42.9, 0.55)),
    "calib": dict(calib=CALIB, rotate=90.0),
    "m3_params": dict(laser_mode="M3", contour_power=35, power=200, feed=12000, step=0.05, overscan=5.0),
}


def config(name):
    c = dict(BASE)
    c.update(CONFIGS[name])
    return c


@lru_cache(maxsize=None)
def sample_geometries(name):
    return tuple(load_gerber(os.path.join(SAMPLES, name)))


def make_context(sample, cfg):
    """Контекст платы, настроенный так же, как это делает окно по значениям интерфейса"""
    ctx = GerberGeometryContext(list(sample_geometries(sample)))
    ctx.rotate_angle = cfg["rotate"]
    ctx.flip_x = cfg["flip_x"]
    ctx.flip_y = cfg["flip_y"]
    if cfg["cam"]:
        ctx.use_camera_offset = True
        ctx.camera_offset_x, ctx.camera_offset_y = cfg["cam"]
    if cfg["calib"]:
        ctx.use_calibration = True
        ctx.matrix_coeffs = cfg["calib"]
    return ctx


def make_params(cfg):
    return GcodeParams(
        power=cfg["power"],
        feedrate=cfg["feed"],
        step=cfg["step"],
        overscan=cfg["overscan"],
        snake=cfg["snake"],
        laser_mode=cfg["laser_mode"],
        contour_power=cfg["contour_power"],
    )


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Окно хранит настройки в ~/.LaserConverterApp.ini — в тестах не трогаем реальный файл пользователя"""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
