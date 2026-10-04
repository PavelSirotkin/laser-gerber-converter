"""Станок: рабочее поле, камера со смещением относительно лазера, мертвая зона камеры."""

from dataclasses import dataclass

from shapely.geometry import box


@dataclass
class MachineConfig:
    field_w: float = 165.0  # рабочее поле станка по X, мм
    field_h: float = 95.0  # рабочее поле станка по Y, мм
    camera_offset: tuple = (42.9, 0.55)  # положение камеры относительно лазера (Лазер -> Камера), мм


def field_rect(config):
    return box(0, 0, config.field_w, config.field_h)


def camera_dead_zone(config):
    """Часть поля, которую камера увидеть не может: лазер туда доезжает, а камере пришлось бы выехать за поле"""
    ox, oy = config.camera_offset
    seen = box(ox, oy, config.field_w + ox, config.field_h + oy)
    return field_rect(config).difference(seen)


def travel_overflow(bounds, overscan, config):
    """Насколько растровый проход по плате с габаритами bounds выходит за поле станка, мм (0 — помещается).
    По X каретка выезжает за плату на overscan с каждой стороны"""
    xmin, ymin, xmax, ymax = bounds
    return max(
        0.0,
        -(xmin - overscan),
        (xmax + overscan) - config.field_w,
        -ymin,
        ymax - config.field_h,
    )
