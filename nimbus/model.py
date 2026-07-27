"""Domain types shared across the app.

Everything the UI renders comes from these dataclasses, so the widgets never
touch raw NWS JSON. Values are stored in the units we display (Fahrenheit,
mph, inHg, miles) with conversion done once at the parsing boundary.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime, tzinfo


class Condition(enum.Enum):
    """Canonical weather condition, distilled from the NWS icon vocabulary."""

    CLEAR = "clear"
    FEW_CLOUDS = "few-clouds"
    PARTLY_CLOUDY = "partly-cloudy"
    MOSTLY_CLOUDY = "mostly-cloudy"
    OVERCAST = "overcast"
    FOG = "fog"
    HAZE = "haze"
    SMOKE = "smoke"
    DUST = "dust"
    WIND = "wind"
    RAIN = "rain"
    SHOWERS = "showers"
    THUNDERSTORM = "thunderstorm"
    SNOW = "snow"
    SLEET = "sleet"
    FREEZING_RAIN = "freezing-rain"
    BLIZZARD = "blizzard"
    HOT = "hot"
    COLD = "cold"
    TORNADO = "tornado"
    HURRICANE = "hurricane"
    UNKNOWN = "unknown"

    @property
    def is_precipitating(self) -> bool:
        return self in _PRECIPITATING

    @property
    def is_frozen(self) -> bool:
        return self in _FROZEN

    @property
    def is_severe(self) -> bool:
        return self in _SEVERE


_PRECIPITATING = frozenset(
    {
        Condition.RAIN,
        Condition.SHOWERS,
        Condition.THUNDERSTORM,
        Condition.SNOW,
        Condition.SLEET,
        Condition.FREEZING_RAIN,
        Condition.BLIZZARD,
        Condition.HURRICANE,
    }
)

_FROZEN = frozenset({Condition.SNOW, Condition.SLEET, Condition.BLIZZARD})

_SEVERE = frozenset(
    {Condition.TORNADO, Condition.HURRICANE, Condition.BLIZZARD, Condition.THUNDERSTORM}
)


class Cloudiness(enum.IntEnum):
    """How much of the sky the scene should cover with cloud."""

    NONE = 0
    LIGHT = 1
    MEDIUM = 2
    HEAVY = 3
    TOTAL = 4


@dataclass(frozen=True)
class Location:
    """A place the user has searched for or pinned."""

    name: str
    state: str
    latitude: float
    longitude: float
    timezone: str = "America/New_York"
    #: NWS grid coordinates, filled in after the first /points lookup.
    grid_id: str = ""
    grid_x: int = 0
    grid_y: int = 0
    station: str = ""

    @property
    def key(self) -> str:
        """Stable identity for favourites, rounded to ~11 m of precision."""
        return f"{self.latitude:.4f},{self.longitude:.4f}"

    @property
    def label(self) -> str:
        return f"{self.name}, {self.state}" if self.state else self.name

    def tz(self) -> tzinfo:
        from zoneinfo import ZoneInfo

        try:
            return ZoneInfo(self.timezone)
        except Exception:
            from datetime import timezone

            return timezone.utc

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "state": self.state,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "timezone": self.timezone,
            "grid_id": self.grid_id,
            "grid_x": self.grid_x,
            "grid_y": self.grid_y,
            "station": self.station,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "Location":
        # Coordinates are what make a location usable, so an entry missing
        # them is rejected rather than silently becoming a point at 0, 0.
        if raw.get("latitude") is None or raw.get("longitude") is None:
            raise KeyError("location requires latitude and longitude")
        return cls(
            name=raw.get("name") or "Unknown",
            state=raw.get("state", ""),
            latitude=float(raw["latitude"]),
            longitude=float(raw["longitude"]),
            timezone=raw.get("timezone", "America/New_York"),
            grid_id=raw.get("grid_id", ""),
            grid_x=int(raw.get("grid_x", 0)),
            grid_y=int(raw.get("grid_y", 0)),
            station=raw.get("station", ""),
        )


@dataclass(frozen=True)
class Current:
    """Latest surface observation, already converted to display units."""

    temperature: float | None
    feels_like: float | None
    condition: Condition
    description: str
    humidity: float | None
    dewpoint: float | None
    wind_speed: float | None
    wind_direction: float | None
    wind_gust: float | None
    pressure: float | None
    visibility: float | None
    is_daytime: bool
    observed_at: datetime | None
    station_name: str = ""

    @property
    def wind_cardinal(self) -> str:
        return cardinal(self.wind_direction)


@dataclass(frozen=True)
class HourEntry:
    """One hour of the hourly forecast."""

    time: datetime
    temperature: float
    condition: Condition
    short_forecast: str
    precip_chance: float
    humidity: float | None
    dewpoint: float | None
    wind_speed: float | None
    wind_direction: str
    is_daytime: bool


@dataclass(frozen=True)
class DayEntry:
    """One calendar day, merged from the NWS day and night periods."""

    date: datetime
    name: str
    high: float | None
    low: float | None
    condition: Condition
    short_forecast: str
    detailed_forecast: str
    night_short_forecast: str = ""
    night_detailed_forecast: str = ""
    precip_chance: float = 0.0
    wind_speed: str = ""
    wind_direction: str = ""


@dataclass(frozen=True)
class Alert:
    """An active NWS watch, warning or advisory."""

    event: str
    severity: str
    urgency: str
    headline: str
    description: str
    instruction: str
    onset: datetime | None
    expires: datetime | None

    @property
    def rank(self) -> int:
        """Lower sorts first; drives both ordering and colour."""
        return {
            "Extreme": 0,
            "Severe": 1,
            "Moderate": 2,
            "Minor": 3,
        }.get(self.severity, 4)


@dataclass
class WeatherBundle:
    """Everything needed to render one location."""

    location: Location
    current: Current | None = None
    hourly: list[HourEntry] = field(default_factory=list)
    daily: list[DayEntry] = field(default_factory=list)
    alerts: list[Alert] = field(default_factory=list)
    fetched_at: datetime | None = None
    partial: bool = False
    """True when some sub-request failed but we still have usable data."""

    @property
    def today(self) -> DayEntry | None:
        return self.daily[0] if self.daily else None


# ---------------------------------------------------------------------------
# Unit helpers
# ---------------------------------------------------------------------------

_CARDINALS = (
    "N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
    "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW",
)


def cardinal(degrees: float | None) -> str:
    """Convert a bearing to a 16-point compass abbreviation."""
    if degrees is None:
        return "--"
    return _CARDINALS[int((degrees % 360) / 22.5 + 0.5) % 16]


def c_to_f(celsius: float | None) -> float | None:
    return None if celsius is None else celsius * 9.0 / 5.0 + 32.0


def kmh_to_mph(kmh: float | None) -> float | None:
    return None if kmh is None else kmh * 0.621371


def pa_to_inhg(pascals: float | None) -> float | None:
    return None if pascals is None else pascals * 0.0002953


def m_to_miles(metres: float | None) -> float | None:
    return None if metres is None else metres * 0.000621371


def heat_index(temp_f: float, humidity: float) -> float:
    """NWS Rothfusz heat index, valid above roughly 80 F."""
    t, r = temp_f, humidity
    hi = (
        -42.379
        + 2.04901523 * t
        + 10.14333127 * r
        - 0.22475541 * t * r
        - 0.00683783 * t * t
        - 0.05481717 * r * r
        + 0.00122874 * t * t * r
        + 0.00085282 * t * r * r
        - 0.00000199 * t * t * r * r
    )
    if r < 13 and 80 <= t <= 112:
        hi -= ((13 - r) / 4) * ((17 - abs(t - 95)) / 17) ** 0.5
    elif r > 85 and 80 <= t <= 87:
        hi += ((r - 85) / 10) * ((87 - t) / 5)
    return hi


def wind_chill(temp_f: float, wind_mph: float) -> float:
    """NWS wind chill, valid at or below 50 F with wind above 3 mph."""
    v = wind_mph**0.16
    return 35.74 + 0.6215 * temp_f - 35.75 * v + 0.4275 * temp_f * v


def apparent_temperature(
    temp_f: float | None, humidity: float | None, wind_mph: float | None
) -> float | None:
    """Best available "feels like" temperature for the given conditions.

    Falls back to the dry-bulb temperature in the range where neither the
    heat index nor wind chill is defined.
    """
    if temp_f is None:
        return None
    if temp_f >= 80 and humidity is not None:
        return heat_index(temp_f, humidity)
    if temp_f <= 50 and wind_mph is not None and wind_mph > 3:
        return wind_chill(temp_f, wind_mph)
    return temp_f
