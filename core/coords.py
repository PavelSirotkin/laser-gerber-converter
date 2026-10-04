"""Разбор координат станка из текста (буфер обмена): строка статуса GRBL, «X.. Y..», «12.3, 45.6»."""

import re

_NUMBER = r"[-+]?\d+(?:[.,]\d+)?"


def _num(text):
    return float(text.replace(",", "."))


def parse_coordinates(text):
    """Координаты (x, y) из текста или None, если разобрать не удалось.

    Понимает:
      - статус GRBL: «<Idle|MPos:12.345,67.890,0.000|...>» (берется MPos, иначе WPos);
      - оси с буквами: «X12.345 Y67.890», «X: 12.3  Y: 4.5», «x=1 y=2»;
      - два числа подряд: «12.345, 67.890», «12,5; 30,25», «12.3 45.6» (десятичная запятая допустима).
    """
    if not text:
        return None
    text = text.strip()

    for key in ("MPos", "WPos"):
        m = re.search(rf"{key}\s*:\s*({_NUMBER})\s*,\s*({_NUMBER})", text, re.IGNORECASE)
        if m:
            return _num(m.group(1)), _num(m.group(2))

    mx = re.search(rf"\bX\s*[:=]?\s*({_NUMBER})", text, re.IGNORECASE)
    my = re.search(rf"\bY\s*[:=]?\s*({_NUMBER})", text, re.IGNORECASE)
    if mx and my:
        return _num(mx.group(1)), _num(my.group(1))

    # Два числа: если разделитель «, » / «;» / пробел — запятая внутри числа считается десятичной
    numbers = re.findall(_NUMBER, text)
    if len(numbers) >= 2 and len(text) <= 80:
        return _num(numbers[0]), _num(numbers[1])
    return None
