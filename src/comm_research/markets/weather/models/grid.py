"""Geographic selection on ERA5's global 0.25-degree latitude/longitude lattice."""

from __future__ import annotations

import math
from dataclasses import dataclass
from numbers import Real


@dataclass(frozen=True)
class WeatherGrid:
    """A bounding box, including native ERA5 cell centers inside its bounds.

    Coordinates are degrees, with signed longitudes. ``west > east`` means
    the box crosses the antimeridian. The grid is always aligned to ERA5's
    global quarter-degree lattice, so overlapping regions share cell keys.
    """

    north: float
    west: float
    south: float
    east: float
    name: str = "region"

    def __post_init__(self):
        for field in ("north", "west", "south", "east"):
            value = getattr(self, field)
            if isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(value):
                raise ValueError(f"{field} must be a finite number")
            object.__setattr__(self, field, float(value))
        if not -90 <= self.south <= self.north <= 90:
            raise ValueError("Require -90 <= south <= north <= 90")
        if not (-180 <= self.west <= 180 and -180 <= self.east <= 180):
            raise ValueError("Longitudes must be in [-180, 180]")
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("name must be a nonempty string")
        if not self.latitudes or not self.longitudes:
            raise ValueError("Bounds contain no native 0.25-degree ERA5 cell centers")

    @property
    def latitudes(self) -> tuple[float, ...]:
        return tuple(i / 4 for i in range(math.ceil(self.south * 4), math.floor(self.north * 4) + 1))

    @property
    def cds_areas(self) -> tuple[tuple[float, float, float, float], ...]:
        """Aligned CDS areas in north/west/south/east order."""
        intervals = ((self.west, self.east),) if self.west <= self.east else (
            (self.west, 180), (-180, self.east)
        )
        north, south = math.floor(self.north * 4) / 4, math.ceil(self.south * 4) / 4
        return tuple((north, math.ceil(west * 4) / 4, south, math.floor(east * 4) / 4)
                     for west, east in intervals if math.ceil(west * 4) <= math.floor(east * 4))

    @property
    def longitudes(self) -> tuple[float, ...]:
        values = set()
        for _, west, _, east in self.cds_areas:
            values.update(((i / 4 + 180) % 360) - 180
                          for i in range(round(west * 4), round(east * 4) + 1))
        return tuple(sorted(values))

    @property
    def point_count(self) -> int:
        return len(self.latitudes) * len(self.longitudes)
