"""Загрузка Gerber-файла в shapely-геометрию (миллиметры)."""
from gerbyx.tokenizer import tokenize_gerber
from gerbyx.parser import GerberParser
from gerbyx.processor import GerberProcessor

from shapely.affinity import scale


def load_gerber(gerber_path):
    """Парсит Gerber-файл и возвращает список непустых shapely-геометрий в миллиметрах"""
    with open(gerber_path, 'r', encoding='utf-8', errors='ignore') as f:
        gerber_source = f.read()

    processor = GerberProcessor()
    parser = GerberParser(processor)
    parser.parse(tokenize_gerber(gerber_source))

    geoms = [g for g in processor.geometries if not g.is_empty]

    # Если файл в дюймах, масштабируем все Shapely элементы в миллиметры (x25.4)
    if "%MOIN%" in gerber_source:
        geoms = [scale(g, xfact=25.4, yfact=25.4, origin=(0, 0)) for g in geoms]

    if not geoms:
        raise ValueError("Файл не содержит графических векторов.")
    return geoms
