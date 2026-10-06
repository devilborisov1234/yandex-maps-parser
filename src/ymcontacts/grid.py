"""Сетка обхода города.

Зачем сетка, а не один запрос: поиск Яндекса отдаёт максимум 50 результатов и
ранжирует их по релевантности запросу, а не по территории. Один запрос
"стоматологии" даст популярные клиники всего города. Чтобы покрыть город
равномерно, мы режем его на прямоугольники (bbox) и делаем запрос в каждом.

Также отсюда берётся ll/spn — параметры, которыми приложение карт описывает
текущий экран. Если шаблон запроса использует их вместо bbox, адаптер
подставит значения отсюда.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .logging_setup import get_logger

log = get_logger(__name__)

# Длина одного градуса широты в км
KM_PER_DEG_LAT = 110.574


def km_to_deg_lat(km: float) -> float:
    """Перевести километры в градусы широты."""
    return km / KM_PER_DEG_LAT


def km_to_deg_lon(km: float, lat: float) -> float:
    """Перевести километры в градусы долготы для заданной широты.

    Зависит от широты: на 54° (Борисов) один градус долготы — примерно 65 км,
    на экваторе — 111 км. Поэтому делим на cos(широты).
    """
    cos_lat = math.cos(math.radians(lat))
    # На полюсах cos стремится к нулю — защищаемся от деления на ноль
    return km / (KM_PER_DEG_LAT * max(cos_lat, 1e-6))


@dataclass(frozen=True)
class Cell:
    """Ячейка сетки: центр + прямоугольник поиска."""

    id: str  # стабильный идентификатор ячейки, например "c03_05"
    lat: float  # центр по широте
    lon: float  # центр по долготе
    bbox: str  # прямоугольник в формате Яндекса: "lon1,lat1~lon2,lat2"

    def query_params(self) -> dict[str, str]:
        """Параметры запроса, которые понимает поиск карт.

        bbox — точный прямоугольник;
        ll   — центр взгляда;
        spn  — масштаб (размер прямоугольника в градусах): "долгота,широта".
        """
        lon1, lat1, lon2, lat2 = self._bbox_tuple()
        return {
            "bbox": self.bbox,
            "ll": f"{self.lon},{self.lat}",
            # Округляем: разность float'ов даёт хвост вида 0.030000000000000113
            "spn": f"{abs(lon2 - lon1):.6f},{abs(lat2 - lat1):.6f}",
        }

    def _bbox_tuple(self) -> tuple[float, float, float, float]:
        """Разобрать bbox обратно в числа (lon1, lat1, lon2, lat2)."""
        south_west, north_east = self.bbox.split("~")
        west, south = south_west.split(",")
        east, north = north_east.split(",")
        return float(west), float(south), float(east), float(north)


def build_grid(
    center_lat: float,
    center_lon: float,
    radius_km: float = 7.0,
    cell_km: float = 2.2,
    skip_empty: bool = False,
) -> list[Cell]:
    """Построить сетку клеток вокруг точки.

    Параметры:
        center_lat, center_lon — центр города;
        radius_km   — до какой точки от центра строим сетку;
        cell_km     — сторона одной клетки в км.

    Возвращает список клеток, отсортированный так, чтобы обход шёл
    с центра наружу: так при обрыве прогона мы успеваем покрыть
    самые густонаселённые районы.
    """
    d_lat = km_to_deg_lat(cell_km)
    d_lon = km_to_deg_lon(cell_km, center_lat)

    cells: list[Cell] = []
    # Сколько клеток уходит в каждую сторону от центра
    steps_lat = max(1, round(radius_km / cell_km))
    steps_lon = max(1, round(radius_km / (cell_km * math.cos(math.radians(center_lat)))))

    for row in range(-steps_lat, steps_lat + 1):
        for col in range(-steps_lon, steps_lon + 1):
            lat = center_lat + row * d_lat
            lon = center_lon + col * d_lon

            # Отсекаем клетки, уходящие за радиус: они почти полностью пустые
            if km_to_deg_lat(radius_km) < abs(row) * d_lat or \
               km_to_deg_lon(radius_km, center_lat) < abs(col) * d_lon:
                if skip_empty:
                    continue

            south, north = lat - d_lat / 2, lat + d_lat / 2
            west, east = lon - d_lon / 2, lon + d_lon / 2

            cells.append(
                Cell(
                    id=f"c{row + steps_lat:02d}_{col + steps_lon:02d}",
                    lat=round(lat, 6),
                    lon=round(lon, 6),
                    bbox=f"{west:.6f},{south:.6f}~{east:.6f},{north:.6f}",
                )
            )

    # Сортировка от центра наружу: расстояние в градусах от центра города
    cells.sort(key=lambda c: math.hypot(c.lat - center_lat, c.lon - center_lon))
    log.info("сетка %s: %s клеток по %.1f км", center_lat, len(cells), cell_km)
    return cells


def grid_bbox(center_lat: float, center_lon: float, radius_km: float) -> str:
    """Общий прямоугольник на весь город — для отчётности и общих запросов."""
    d_lat = km_to_deg_lat(radius_km)
    d_lon = km_to_deg_lon(radius_km, center_lat)
    return (
        f"{center_lon - d_lon:.6f},{center_lat - d_lat:.6f}~"
        f"{center_lon + d_lon:.6f},{center_lat + d_lat:.6f}"
    )