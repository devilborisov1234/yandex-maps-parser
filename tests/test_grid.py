"""Тесты сетки обхода.

Сетка — основа покрытия: если она кривая, дырки в данных гарантированы.
"""

from ymcontacts.grid import Cell, build_grid, grid_bbox, km_to_deg_lon


class TestGrid:
    def test_cell_count_is_sane(self):
        """Радиус 7 км и клетка 2.2 км — десятки клеток, не сотни и не две."""
        cells = build_grid(54.2277, 28.4850, radius_km=7.0, cell_km=2.2)
        assert 20 < len(cells) < 200

    def test_center_cell_exists(self):
        """В сетке должна быть клетка почти в центре города."""
        cells = build_grid(54.2277, 28.4850, radius_km=7.0, cell_km=2.2)
        assert min(abs(c.lat - 54.2277) for c in cells) < 0.01

    def test_sorted_from_center_out(self):
        """Обход идёт от центра: при обрыве успеем покрыть густое."""
        cells = build_grid(54.2277, 28.4850, radius_km=7.0, cell_km=2.2)
        first = cells[0]
        last = cells[-1]
        assert abs(first.lat - 54.2277) <= abs(last.lat - 54.2277)

    def test_ids_unique(self):
        cells = build_grid(54.2277, 28.4850)
        assert len({c.id for c in cells}) == len(cells)

    def test_bbox_format(self):
        """Формат Яндекса: долгота,широта~долгота,широта."""
        cell = Cell(id="c00_00", lat=54.2277, lon=28.485, bbox="28.47,54.21~28.50,54.24")
        params = cell.query_params()
        assert params["bbox"] == "28.47,54.21~28.50,54.24"
        assert params["ll"] == "28.485,54.2277"
        # spn = размер прямоугольника в градусах: долгота,широта
        assert params["spn"] == "0.030000,0.030000"

    def test_lon_shrinks_with_latitude(self):
        """На 54° градус долготы короче, чем на экваторе."""
        equator = km_to_deg_lon(10, 0.0)
        north = km_to_deg_lon(10, 54.0)
        assert north > equator


class TestCityBbox:
    def test_contains_center(self):
        bbox = grid_bbox(54.2277, 28.4850, 5.0)
        west, south, east, north = (float(v) for v in bbox.replace("~", ",").split(","))
        assert west < 28.485 < east
        assert south < 54.2277 < north