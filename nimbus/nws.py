# SPDX-License-Identifier: GPL-3.0-or-later
#
# Nimbus -- a weather application for GNOME.
# Copyright (C) 2026  Walter Rice
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

"""National Weather Service API client and US location search.

All network work happens on a small thread pool; results are handed back to
the GTK main loop through :func:`GLib.idle_add` so callers never touch a
widget off-thread.

The NWS API is free and keyless but requires a descriptive ``User-Agent``
identifying the application, per its terms of service.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ElementTree
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from gi.repository import GLib

from . import conditions
from .model import (
    Alert,
    Condition,
    Current,
    DayEntry,
    HourEntry,
    Location,
    WeatherBundle,
    apparent_temperature,
    c_to_f,
    kmh_to_mph,
    m_to_miles,
    pa_to_inhg,
)

log = logging.getLogger(__name__)

APP_NAME = "Nimbus Weather"
APP_CONTACT = "https://github.com/nimbus-weather"
USER_AGENT = f"({APP_NAME}, {APP_CONTACT})"

NWS_BASE = "https://api.weather.gov"
GEOCODE_BASE = "https://geocoding-api.open-meteo.com/v1/search"

REQUEST_TIMEOUT = 20.0
MAX_RETRIES = 3

#: How long each kind of response stays fresh. Observations update roughly
#: hourly, gridpoint forecasts about that often, and the point-to-grid
#: mapping essentially never changes.
TTL_POINT = timedelta(days=30)
TTL_CURRENT = timedelta(minutes=10)
TTL_FORECAST = timedelta(minutes=30)
TTL_ALERTS = timedelta(minutes=5)
TTL_GEOCODE = timedelta(days=7)


class WeatherError(Exception):
    """A user-facing failure while retrieving weather data."""


# ---------------------------------------------------------------------------
# Caching
# ---------------------------------------------------------------------------


def _cache_dir() -> str:
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    path = os.path.join(base, "nimbus-weather")
    os.makedirs(path, exist_ok=True)
    return path


class JSONCache:
    """A tiny thread-safe disk cache keyed on request URL."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._memory: dict[str, tuple[float, Any]] = {}
        self._dir = _cache_dir()

    def _path(self, key: str) -> str:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]
        return os.path.join(self._dir, f"{digest}.json")

    def get(self, key: str, ttl: timedelta) -> Any | None:
        cutoff = time.time() - ttl.total_seconds()

        with self._lock:
            entry = self._memory.get(key)
            if entry and entry[0] >= cutoff:
                return entry[1]

        path = self._path(key)
        try:
            if os.path.getmtime(path) < cutoff:
                return None
            with open(path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, ValueError):
            return None

        with self._lock:
            self._memory[key] = (os.path.getmtime(path), payload)
        return payload

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self._memory[key] = (time.time(), value)
        path = self._path(key)
        try:
            # Write to a sibling then rename so a crash can't leave a
            # half-written file that later parses as valid-but-truncated.
            tmp = f"{path}.{os.getpid()}.tmp"
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(value, handle)
            os.replace(tmp, path)
        except OSError as exc:
            log.debug("cache write failed for %s: %s", key, exc)

    def get_stale(self, key: str) -> Any | None:
        """Return a cached value regardless of age, for offline fallback."""
        return self.get(key, timedelta(days=3650))


_cache = JSONCache()


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


def fetch_json(url: str, ttl: timedelta, allow_stale: bool = True) -> Any:
    """GET *url* and decode JSON, honouring the cache and retrying on failure."""
    cached = _cache.get(url, ttl)
    if cached is not None:
        return cached

    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/geo+json, application/json",
        },
    )

    last_error: Exception | None = None
    for attempt in range(MAX_RETRIES):
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
                payload = json.loads(response.read().decode("utf-8"))
            _cache.set(url, payload)
            return payload
        except urllib.error.HTTPError as exc:
            # 404 on a gridpoint is a real answer, not a transient fault.
            if exc.code in (404, 400):
                raise WeatherError(
                    f"The weather service has no data for this location ({exc.code})."
                ) from exc
            last_error = exc
        except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
            last_error = exc

        if attempt < MAX_RETRIES - 1:
            time.sleep(0.6 * (2**attempt))

    if allow_stale:
        stale = _cache.get_stale(url)
        if stale is not None:
            log.warning("serving stale cache for %s (%s)", url, last_error)
            return stale

    raise WeatherError(f"Could not reach the weather service: {last_error}")


def _binary_path(url: str) -> str:
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:32]
    return os.path.join(_cache_dir(), f"{digest}.bin")


def fetch_binary(url: str, ttl: timedelta, allow_stale: bool = True) -> bytes:
    """GET *url* expecting a PNG, with the same caching and retry policy.

    WMS servers report failures as an XML document served with a 200 status,
    so the payload is checked for a PNG signature rather than trusting the
    status code.
    """
    path = _binary_path(url)
    cutoff = time.time() - ttl.total_seconds()
    try:
        if os.path.getmtime(path) >= cutoff:
            with open(path, "rb") as handle:
                return handle.read()
    except OSError:
        pass

    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})

    last_error: Exception | None = None
    for attempt in range(MAX_RETRIES):
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
                payload = response.read()
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = exc
        else:
            if not payload.startswith(b"\x89PNG"):
                raise WeatherError("The radar service did not return an image.")
            try:
                tmp = f"{path}.{os.getpid()}.tmp"
                with open(tmp, "wb") as handle:
                    handle.write(payload)
                os.replace(tmp, path)
            except OSError as exc:
                log.debug("radar cache write failed: %s", exc)
            return payload

        if attempt < MAX_RETRIES - 1:
            time.sleep(0.6 * (2**attempt))

    if allow_stale and os.path.exists(path):
        log.warning("serving stale radar tile for %s (%s)", url, last_error)
        with open(path, "rb") as handle:
            return handle.read()

    raise WeatherError(f"Could not reach the radar service: {last_error}")


def fetch_text(url: str) -> str:
    """GET *url* and decode it as text, retrying transient failures.

    Used for the radar server's capabilities document, which is XML. Callers
    cache the parsed result rather than the document, which is large and only
    a few dozen bytes of it are ever wanted.
    """
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})

    last_error: Exception | None = None
    for attempt in range(MAX_RETRIES):
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
                return response.read().decode("utf-8", errors="replace")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = exc
            if attempt < MAX_RETRIES - 1:
                time.sleep(0.6 * (2**attempt))

    raise WeatherError(f"Could not reach the radar service: {last_error}")


#: Sweep the image cache at most this often, and discard tiles older than this.
_SWEEP_INTERVAL = 3600.0
_SWEEP_MAX_AGE = timedelta(hours=12)
_last_sweep = 0.0
_sweep_lock = threading.Lock()


def sweep_binary_cache() -> None:
    """Delete image tiles too old to be served, even as a stale fallback.

    An animation caches a dozen tiles per view, and every zoom level and
    window size produces its own set, so without this the cache would grow
    without bound across sessions.
    """
    global _last_sweep

    now = time.time()
    with _sweep_lock:
        if now - _last_sweep < _SWEEP_INTERVAL:
            return
        _last_sweep = now

    cutoff = now - _SWEEP_MAX_AGE.total_seconds()
    try:
        with os.scandir(_cache_dir()) as entries:
            for entry in entries:
                if entry.name.endswith(".bin") and entry.stat().st_mtime < cutoff:
                    os.unlink(entry.path)
    except OSError as exc:
        log.debug("image cache sweep failed: %s", exc)


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def _value(node: Any) -> float | None:
    """Pull ``.value`` out of an NWS quantitative-value object."""
    if isinstance(node, dict):
        value = node.get("value")
        return float(value) if isinstance(value, (int, float)) else None
    if isinstance(node, (int, float)):
        return float(node)
    return None


def _parse_time(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _speed_mph(raw: Any) -> float | None:
    """Parse a wind speed given either as a quantity or a string like '5 mph'."""
    if isinstance(raw, dict):
        value = _value(raw)
        unit = raw.get("unitCode", "")
        if value is None:
            return None
        return value if "mi_h" in unit else kmh_to_mph(value)
    if isinstance(raw, str):
        # Ranges such as "5 to 10 mph" report the upper bound.
        numbers = [int(part) for part in raw.split() if part.isdigit()]
        if numbers:
            return float(max(numbers))
    return None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


def resolve_location(latitude: float, longitude: float) -> Location:
    """Map a coordinate to an NWS grid cell and its nearest named place."""
    url = f"{NWS_BASE}/points/{latitude:.4f},{longitude:.4f}"
    props = fetch_json(url, TTL_POINT).get("properties", {})

    relative = props.get("relativeLocation", {}).get("properties", {})
    station = ""
    try:
        stations_url = props.get("observationStations")
        if stations_url:
            features = fetch_json(stations_url, TTL_POINT).get("features", [])
            if features:
                station = features[0]["properties"]["stationIdentifier"]
    except (WeatherError, KeyError, IndexError):
        log.debug("no observation station for %s,%s", latitude, longitude)

    return Location(
        name=relative.get("city") or "Unknown",
        state=relative.get("state") or "",
        latitude=latitude,
        longitude=longitude,
        timezone=props.get("timeZone") or "America/New_York",
        grid_id=props.get("gridId") or "",
        grid_x=int(props.get("gridX") or 0),
        grid_y=int(props.get("gridY") or 0),
        station=station,
    )


def fetch_current(location: Location) -> Current | None:
    """Latest observation from the location's nearest reporting station."""
    if not location.station:
        return None

    url = f"{NWS_BASE}/stations/{location.station}/observations/latest"
    props = fetch_json(url, TTL_CURRENT).get("properties", {})

    temp = c_to_f(_value(props.get("temperature")))
    humidity = _value(props.get("relativeHumidity"))
    wind = kmh_to_mph(_value(props.get("windSpeed")))

    # Prefer the station's own heat index or wind chill when it reports one.
    feels = c_to_f(_value(props.get("heatIndex")))
    if feels is None:
        feels = c_to_f(_value(props.get("windChill")))
    if feels is None:
        feels = apparent_temperature(temp, humidity, wind)

    icon = props.get("icon")
    text = props.get("textDescription") or ""
    condition = conditions.classify(icon, text)

    return Current(
        temperature=temp,
        feels_like=feels,
        condition=condition,
        description=text or conditions.display_name(condition),
        humidity=humidity,
        dewpoint=c_to_f(_value(props.get("dewpoint"))),
        wind_speed=wind,
        wind_direction=_value(props.get("windDirection")),
        wind_gust=kmh_to_mph(_value(props.get("windGust"))),
        pressure=pa_to_inhg(
            _value(props.get("barometricPressure"))
            or _value(props.get("seaLevelPressure"))
        ),
        visibility=m_to_miles(_value(props.get("visibility"))),
        is_daytime=not conditions.is_night_icon(icon),
        observed_at=_parse_time(props.get("timestamp")),
        station_name=location.station,
    )


def fetch_hourly(location: Location, hours: int = 48) -> list[HourEntry]:
    """The next *hours* hours of forecast, starting from the current hour."""
    url = (
        f"{NWS_BASE}/gridpoints/{location.grid_id}"
        f"/{location.grid_x},{location.grid_y}/forecast/hourly"
    )
    periods = fetch_json(url, TTL_FORECAST).get("properties", {}).get("periods", [])

    entries: list[HourEntry] = []
    now = datetime.now(timezone.utc)
    for period in periods:
        start = _parse_time(period.get("startTime"))
        if start is None or start < now - timedelta(hours=1):
            continue

        icon = period.get("icon")
        short = period.get("shortForecast") or ""
        entries.append(
            HourEntry(
                time=start,
                temperature=float(period.get("temperature", 0)),
                condition=conditions.classify(icon, short),
                short_forecast=short,
                precip_chance=_value(period.get("probabilityOfPrecipitation")) or 0.0,
                humidity=_value(period.get("relativeHumidity")),
                dewpoint=c_to_f(_value(period.get("dewpoint"))),
                wind_speed=_speed_mph(period.get("windSpeed")),
                wind_direction=period.get("windDirection") or "",
                is_daytime=bool(period.get("isDaytime", True)),
            )
        )
        if len(entries) >= hours:
            break
    return entries


def fetch_daily(location: Location) -> list[DayEntry]:
    """The multi-day forecast, with day and night periods merged per date."""
    url = (
        f"{NWS_BASE}/gridpoints/{location.grid_id}"
        f"/{location.grid_x},{location.grid_y}/forecast"
    )
    periods = fetch_json(url, TTL_FORECAST).get("properties", {}).get("periods", [])

    # NWS emits alternating day/night periods, but the first entry may be a
    # partial period ("This Afternoon") or a night when fetched after dark.
    # Grouping by local date rather than by index handles both.
    grouped: dict[str, dict[str, Any]] = {}
    order: list[str] = []

    for period in periods:
        start = _parse_time(period.get("startTime"))
        if start is None:
            continue
        is_day = bool(period.get("isDaytime", True))

        # A night period that begins before midnight belongs to the day it
        # started; one that begins after midnight belongs to the prior day.
        anchor = start
        if not is_day and start.hour < 12:
            anchor = start - timedelta(hours=12)
        key = anchor.strftime("%Y-%m-%d")

        if key not in grouped:
            grouped[key] = {"date": anchor}
            order.append(key)
        grouped[key]["day" if is_day else "night"] = period

    days: list[DayEntry] = []
    for key in order:
        bucket = grouped[key]
        day_period = bucket.get("day")
        night_period = bucket.get("night")
        primary = day_period or night_period
        if primary is None:
            continue

        icon = primary.get("icon")
        short = primary.get("shortForecast") or ""

        high = float(day_period["temperature"]) if day_period else None
        low = float(night_period["temperature"]) if night_period else None

        precip = max(
            _value((day_period or {}).get("probabilityOfPrecipitation")) or 0.0,
            _value((night_period or {}).get("probabilityOfPrecipitation")) or 0.0,
        )

        # The night fields carry the second half of a day whose first half
        # is also present. After sunset the day period drops out of the feed
        # and the night period IS the primary, so filling them again would
        # print the same "Tonight" narrative twice.
        follows_day = day_period is not None and night_period is not None

        days.append(
            DayEntry(
                date=bucket["date"],
                name=primary.get("name") or "",
                high=high,
                low=low,
                condition=conditions.classify(icon, short),
                short_forecast=short,
                detailed_forecast=primary.get("detailedForecast") or "",
                night_short_forecast=(
                    night_period.get("shortForecast") or "" if follows_day else ""
                ),
                night_detailed_forecast=(
                    night_period.get("detailedForecast") or "" if follows_day else ""
                ),
                precip_chance=precip,
                wind_speed=str(primary.get("windSpeed") or ""),
                wind_direction=str(primary.get("windDirection") or ""),
            )
        )
    return days


def fetch_alerts(location: Location) -> list[Alert]:
    """Active watches, warnings and advisories covering the location."""
    url = f"{NWS_BASE}/alerts/active?point={location.latitude:.4f},{location.longitude:.4f}"
    features = fetch_json(url, TTL_ALERTS).get("features", [])

    alerts: list[Alert] = []
    for feature in features:
        props = feature.get("properties", {})
        alerts.append(
            Alert(
                event=props.get("event") or "Weather Alert",
                severity=props.get("severity") or "Unknown",
                urgency=props.get("urgency") or "Unknown",
                headline=props.get("headline") or "",
                description=props.get("description") or "",
                instruction=props.get("instruction") or "",
                onset=_parse_time(props.get("onset") or props.get("effective")),
                expires=_parse_time(props.get("expires") or props.get("ends")),
            )
        )
    alerts.sort(key=lambda a: a.rank)
    return alerts


def fetch_bundle(location: Location) -> WeatherBundle:
    """Assemble a full weather bundle, tolerating partial failures.

    Only the hourly and daily forecasts are treated as essential; a missing
    observation station or an alerts outage degrades the display rather than
    failing the whole refresh.
    """
    if not location.grid_id:
        location = resolve_location(location.latitude, location.longitude)

    bundle = WeatherBundle(location=location, fetched_at=datetime.now(timezone.utc))

    try:
        bundle.hourly = fetch_hourly(location)
    except WeatherError as exc:
        log.warning("hourly forecast failed: %s", exc)
        bundle.partial = True

    try:
        bundle.daily = fetch_daily(location)
    except WeatherError as exc:
        log.warning("daily forecast failed: %s", exc)
        bundle.partial = True

    if not bundle.hourly and not bundle.daily:
        raise WeatherError("No forecast is available for this location right now.")

    try:
        bundle.current = fetch_current(location)
    except WeatherError as exc:
        log.warning("current conditions failed: %s", exc)
        bundle.partial = True

    # Fall back to the first forecast hour so the hero always has something
    # to show even when the nearest station is offline.
    if bundle.current is None and bundle.hourly:
        first = bundle.hourly[0]
        bundle.current = Current(
            temperature=first.temperature,
            feels_like=apparent_temperature(
                first.temperature, first.humidity, first.wind_speed
            ),
            condition=first.condition,
            description=first.short_forecast,
            humidity=first.humidity,
            dewpoint=first.dewpoint,
            wind_speed=first.wind_speed,
            wind_direction=None,
            wind_gust=None,
            pressure=None,
            visibility=None,
            is_daytime=first.is_daytime,
            observed_at=first.time,
            station_name="",
        )

    # Stations intermittently report a temperature with no sky condition at
    # all -- an empty textDescription and no icon. The forecast for the same
    # hour still knows what the sky is doing, so borrow from it rather than
    # showing the user "Unknown" beside a perfectly good temperature.
    if (
        bundle.current is not None
        and bundle.current.condition is Condition.UNKNOWN
        and bundle.hourly
        and bundle.hourly[0].condition is not Condition.UNKNOWN
    ):
        forecast_hour = bundle.hourly[0]
        bundle.current = replace(
            bundle.current,
            condition=forecast_hour.condition,
            description=forecast_hour.short_forecast
            or conditions.display_name(forecast_hour.condition),
        )

    try:
        bundle.alerts = fetch_alerts(location)
    except WeatherError as exc:
        log.warning("alerts failed: %s", exc)

    return bundle


# ---------------------------------------------------------------------------
# Location search
# ---------------------------------------------------------------------------


def search_locations(query: str, limit: int = 12) -> list[Location]:
    """Search US places by name, ZIP code or "City, ST".

    Open-Meteo's geocoder is used for the name-to-coordinate step because the
    NWS API has no search endpoint of its own. Results are filtered to the
    United States since that is the only region NWS covers.
    """
    query = query.strip()
    if len(query) < 2:
        return []

    # "Durham, NC" -> search "Durham", then prefer North Carolina results.
    state_hint = ""
    if "," in query:
        head, _, tail = query.rpartition(",")
        candidate = tail.strip()
        if head.strip() and len(candidate) <= 20:
            query, state_hint = head.strip(), candidate.lower()

    params = urllib.parse.urlencode(
        {"name": query, "count": 40, "language": "en", "format": "json"}
    )
    payload = fetch_json(f"{GEOCODE_BASE}?{params}", TTL_GEOCODE, allow_stale=False)

    scored: list[tuple[bool, int, Location]] = []
    for raw in payload.get("results") or []:
        if raw.get("country_code") != "US":
            continue

        state = raw.get("admin1") or ""
        location = Location(
            name=raw.get("name") or query,
            state=_state_abbrev(state),
            latitude=float(raw["latitude"]),
            longitude=float(raw["longitude"]),
            timezone=raw.get("timezone") or "America/New_York",
        )

        matches_hint = bool(state_hint) and state_hint in (
            location.state.lower(),
            state.lower(),
        )
        scored.append((matches_hint, int(raw.get("population") or 0), location))

    # When the query named a state, show only that state's matches -- but if
    # nothing matched, the hint was probably not a state, so keep everything.
    if any(hit for hit, _, _ in scored):
        scored = [item for item in scored if item[0]]

    # The geocoder also matches alternate and historical place names, which
    # surfaces confusing hits (searching "Portland" returning "Blue Island").
    # Keep only results whose own name matches, unless that leaves nothing.
    needle = query.casefold()
    direct = [item for item in scored if needle in item[2].name.casefold()]
    if direct:
        scored = direct

    # Largest places first; they are what people usually mean.
    scored.sort(key=lambda item: -item[1])

    # Drop near-duplicate coordinates that some datasets report twice.
    seen: set[str] = set()
    unique: list[Location] = []
    for _, _, location in scored:
        if location.key in seen:
            continue
        seen.add(location.key)
        unique.append(location)
        if len(unique) >= limit:
            break
    return unique


_STATES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "district of columbia": "DC", "florida": "FL", "georgia": "GA", "hawaii": "HI",
    "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA",
    "kansas": "KS", "kentucky": "KY", "louisiana": "LA", "maine": "ME",
    "maryland": "MD", "massachusetts": "MA", "michigan": "MI", "minnesota": "MN",
    "mississippi": "MS", "missouri": "MO", "montana": "MT", "nebraska": "NE",
    "nevada": "NV", "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM",
    "new york": "NY", "north carolina": "NC", "north dakota": "ND", "ohio": "OH",
    "oklahoma": "OK", "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI",
    "south carolina": "SC", "south dakota": "SD", "tennessee": "TN", "texas": "TX",
    "utah": "UT", "vermont": "VT", "virginia": "VA", "washington": "WA",
    "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
    "puerto rico": "PR", "guam": "GU", "american samoa": "AS",
    "u.s. virgin islands": "VI", "virgin islands": "VI",
    "northern mariana islands": "MP",
}


def _state_abbrev(name: str) -> str:
    if len(name) == 2:
        return name.upper()
    return _STATES.get(name.lower(), name[:2].upper() if name else "")


# ---------------------------------------------------------------------------
# Threaded facade
# ---------------------------------------------------------------------------


class WeatherService:
    """Runs blocking API work off the main loop and calls back on it."""

    def __init__(self, workers: int = 6) -> None:
        self._pool = ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix="nimbus-net"
        )
        self._generation = 0
        self._lock = threading.Lock()

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)

    def _submit(
        self,
        work: Callable[[], Any],
        on_success: Callable[[Any], None],
        on_error: Callable[[Exception], None] | None,
        token: int | None,
    ) -> None:
        def run() -> None:
            try:
                result = work()
            except Exception as exc:  # noqa: BLE001 - reported to the caller
                log.debug("background work failed", exc_info=True)
                if on_error is not None:
                    GLib.idle_add(self._deliver, on_error, exc, token, priority=GLib.PRIORITY_DEFAULT)
                return
            GLib.idle_add(self._deliver, on_success, result, token, priority=GLib.PRIORITY_DEFAULT)

        self._pool.submit(run)

    def _deliver(self, callback: Callable[[Any], None], value: Any, token: int | None) -> bool:
        # Drop results from superseded requests, e.g. an older search whose
        # response arrived after the user kept typing.
        if token is not None:
            with self._lock:
                if token != self._generation:
                    return GLib.SOURCE_REMOVE
        callback(value)
        return GLib.SOURCE_REMOVE

    def next_token(self) -> int:
        """Invalidate in-flight token-tagged requests and return a new token."""
        with self._lock:
            self._generation += 1
            return self._generation

    def load_weather(
        self,
        location: Location,
        on_success: Callable[[WeatherBundle], None],
        on_error: Callable[[Exception], None] | None = None,
    ) -> None:
        self._submit(lambda: fetch_bundle(location), on_success, on_error, None)

    def search(
        self,
        query: str,
        on_success: Callable[[list[Location]], None],
        on_error: Callable[[Exception], None] | None = None,
        token: int | None = None,
    ) -> None:
        self._submit(lambda: search_locations(query), on_success, on_error, token)

    def load_radar(
        self,
        location: Location,
        width_km: float,
        width: int,
        height: int,
        on_success: Callable[[Any], None],
        on_error: Callable[[Exception], None] | None = None,
        token: int | None = None,
        count: int = 1,
        include_clouds: bool = False,
    ) -> None:
        """Fetch a radar view, or with *count* above one, a whole animation.

        The animation is a dozen sequential requests, so a view asks for the
        latest frame first and paints it, then comes back for the history.
        """
        self._submit(
            lambda: fetch_radar(
                location.latitude, location.longitude, width_km, width, height,
                count, include_clouds,
            ),
            on_success,
            on_error,
            token,
        )

    def load_radar_legend(
        self,
        on_success: Callable[[Any], None],
        on_error: Callable[[Exception], None] | None = None,
    ) -> None:
        self._submit(fetch_radar_legend, on_success, on_error, None)

    def resolve(
        self,
        location: Location,
        on_success: Callable[[Location], None],
        on_error: Callable[[Exception], None] | None = None,
    ) -> None:
        self._submit(
            lambda: resolve_location(location.latitude, location.longitude),
            on_success,
            on_error,
            None,
        )

# ---------------------------------------------------------------------------
# Radar imagery
# ---------------------------------------------------------------------------

#: NOAA's public GeoServer, which serves the national radar mosaics and a
#: geopolitical boundary layer for context.
RADAR_SERVER = "https://opengeo.ncep.noaa.gov/geoserver"
RADAR_BASE = f"{RADAR_SERVER}/ows"
RADAR_BOUNDARY_LAYER = "geopolitical"

#: Reflectivity mosaics by coverage area, as (layer, lon0, lat0, lon1, lat1).
#: The CONUS mosaic stops at the border, so Alaska, Hawaii, the Caribbean and
#: Guam each need their own.
_RADAR_REGIONS: tuple[tuple[str, float, float, float, float], ...] = (
    ("alaska:alaska_bref_qcd", -180.0, 50.0, -128.0, 72.0),
    ("hawaii:hawaii_bref_qcd", -161.0, 17.5, -153.5, 23.5),
    ("carib:carib_bref_qcd", -68.5, 16.5, -64.0, 19.5),
    ("guam:guam_bref_qcd", 143.0, 12.0, 146.5, 15.5),
    ("conus:conus_bref_qcd", -127.5, 21.5, -64.5, 51.5),
)

#: NOAA's nowCOAST GeoServer, which serves the GOES East/West satellite
#: composites. Same WMS dialect and projection as the radar server, so its
#: imagery can be requested over the identical extent and overlaid exactly.
CLOUDS_SERVER = "https://nowcoast.noaa.gov/geoserver"
CLOUDS_BASE = f"{CLOUDS_SERVER}/ows"
#: Longwave infrared: the one channel that shows cloud day and night.
CLOUDS_LAYER = "satellite:goes_longwave_imagery"

#: The GOES East/West composite's advertised coverage, as lon0, lat0, lon1,
#: lat1. Most of Alaska and all of Guam fall outside it.
_CLOUDS_EXTENT = (-179.5, 10.9, -50.75, 50.56)

#: Radar mosaics refresh every few minutes.
TTL_RADAR = timedelta(minutes=3)
#: A frame requested at an explicit scan time is immutable, so it may be held
#: far longer than the live mosaic -- long enough to survive a zoom and come
#: back, but well inside the sweep that clears the image cache.
TTL_RADAR_FRAME = timedelta(hours=6)
TTL_RADAR_TIMES = timedelta(minutes=2)
TTL_LEGEND = timedelta(days=7)

#: How many frames make up an animation, and how far back the oldest reaches.
#: The mosaics are published about every two minutes and roughly two hours are
#: kept, so this samples one frame in every two or three.
RADAR_FRAME_COUNT = 11
RADAR_SPAN = timedelta(minutes=50)

#: Web Mercator metres per degree of longitude.
_MERC_PER_DEGREE = 20037508.34 / 180.0


def radar_layer(latitude: float, longitude: float) -> str:
    """The reflectivity layer covering a coordinate."""
    for layer, lon0, lat0, lon1, lat1 in _RADAR_REGIONS:
        if lon0 <= longitude <= lon1 and lat0 <= latitude <= lat1:
            return layer
    return _RADAR_REGIONS[-1][0]


def clouds_available(latitude: float, longitude: float) -> bool:
    """Whether the GOES satellite composite covers a coordinate."""
    lon0, lat0, lon1, lat1 = _CLOUDS_EXTENT
    return lon0 <= longitude <= lon1 and lat0 <= latitude <= lat1


def _to_mercator(longitude: float, latitude: float) -> tuple[float, float]:
    latitude = max(-85.05, min(85.05, latitude))
    x = longitude * _MERC_PER_DEGREE
    y = math.log(math.tan((90.0 + latitude) * math.pi / 360.0)) * (
        _MERC_PER_DEGREE * 180.0 / math.pi
    )
    return x, y


def mercator_metres_per_ground_metre(latitude: float) -> float:
    """Web Mercator exaggerates distance away from the equator by 1/cos(lat)."""
    return 1.0 / max(0.02, math.cos(math.radians(latitude)))


@dataclass
class RadarEcho:
    """One reflectivity image and the moment the mosaic was valid.

    ``valid_at`` is ``None`` only when the server's advertised scan times could
    not be read and the frame was requested without one, which yields whatever
    the server considers current.
    """

    reflectivity: bytes
    valid_at: datetime | None
    #: The GOES infrared frame nearest this scan, when clouds were asked for
    #: and the satellite composite covers the view.
    clouds: bytes | None = None


@dataclass
class RadarSequence:
    """A radar view: one base map, and the echoes to animate over it.

    Every echo covers the identical extent at the identical size, so the base
    map is fetched once and painted under whichever echo is showing.
    """

    basemap: bytes
    #: Oldest first; the last echo is the most recent scan available.
    echoes: list[RadarEcho]
    width: int
    height: int
    #: Ground width of the view in kilometres, used to draw the scale bar.
    width_km: float
    fetched_at: datetime

    @property
    def current(self) -> RadarEcho:
        return self.echoes[-1]


def _radar_request(
    layers: str,
    bbox: str,
    width: int,
    height: int,
    moment: datetime | None = None,
    base: str = RADAR_BASE,
) -> str:
    query = {
        "service": "WMS",
        "version": "1.3.0",
        "request": "GetMap",
        "layers": layers,
        "styles": "",
        "crs": "EPSG:3857",
        "bbox": bbox,
        "width": str(width),
        "height": str(height),
        "format": "image/png",
        "transparent": "true",
    }
    if moment is not None:
        query["time"] = _wms_time(moment)
    return f"{base}?{urllib.parse.urlencode(query)}"


def _wms_time(moment: datetime) -> str:
    """Format a scan time exactly as the capabilities document lists it."""
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _radar_capabilities_url(layer: str, server: str = RADAR_SERVER) -> str:
    # Asking the workspace that owns the layer rather than the server root
    # keeps the document to a few kilobytes instead of several megabytes.
    workspace = layer.split(":")[0]
    query = urllib.parse.urlencode(
        {"service": "WMS", "version": "1.3.0", "request": "GetCapabilities"}
    )
    return f"{server}/{workspace}/ows?{query}"


def _parse_scan_times(document: str, layer: str) -> list[datetime]:
    """Read a layer's advertised ``time`` dimension, oldest first.

    The capabilities document is namespaced, and which namespace depends on
    the WMS version negotiated, so tags are matched on their local name.
    """
    try:
        root = ElementTree.fromstring(document)
    except ElementTree.ParseError as exc:
        raise WeatherError(
            f"The radar service returned unreadable metadata: {exc}"
        ) from exc

    short = layer.split(":")[-1]
    for element in root.iter():
        if not element.tag.endswith("}Layer") and element.tag != "Layer":
            continue
        name = next(
            (
                child.text
                for child in element
                if child.tag.endswith("Name") and child.text
            ),
            None,
        )
        if name not in (layer, short):
            continue
        for child in element:
            if child.tag.endswith("Dimension") and child.get("name") == "time":
                moments = [
                    parsed
                    for raw in (child.text or "").split(",")
                    if (parsed := _parse_time(raw.strip())) is not None
                ]
                return sorted(moments)
    return []


def radar_scan_times(layer: str, server: str = RADAR_SERVER) -> list[datetime]:
    """The scan times *layer* currently offers, oldest first."""
    url = _radar_capabilities_url(layer, server)
    cached = _cache.get(url, TTL_RADAR_TIMES)
    if cached is None:
        moments = _parse_scan_times(fetch_text(url), layer)
        cached = [moment.isoformat() for moment in moments]
        _cache.set(url, cached)
    return [datetime.fromisoformat(raw) for raw in cached]


def _sample_scan_times(
    available: list[datetime], count: int, span: timedelta
) -> list[datetime]:
    """Pick *count* scan times ending at the newest, spread evenly over *span*.

    Snapping evenly spaced targets onto whatever the server actually offers,
    rather than taking every nth entry, keeps the animation running at a
    steady rate through the gaps that appear when a scan is late or missing.
    """
    if not available or count < 1:
        return []
    newest = available[-1]
    if count == 1:
        return [newest]

    oldest = max(available[0], newest - span)
    step = (newest - oldest) / (count - 1)
    chosen: list[datetime] = []
    for index in range(count):
        target = oldest + step * index
        nearest = min(available, key=lambda moment: abs(moment - target))
        if not chosen or nearest != chosen[-1]:
            chosen.append(nearest)
    return chosen


def fetch_radar(
    latitude: float,
    longitude: float,
    width_km: float,
    width: int,
    height: int,
    count: int = 1,
    include_clouds: bool = False,
) -> RadarSequence:
    """Fetch *count* radar frames centred on a coordinate, oldest first.

    The bounding box is built in Web Mercator with the same aspect ratio as
    the target image, so nothing is stretched, and every layer is requested
    over the identical extent so they overlay exactly.

    A count of one asks only for the latest scan, which is what the view shows
    while the rest of the animation is still arriving.

    With *include_clouds*, each frame also carries the GOES infrared image
    nearest its scan time, when the satellite composite covers the view.
    """
    sweep_binary_cache()

    width = max(64, min(1600, int(width)))
    height = max(64, min(1000, int(height)))

    centre_x, centre_y = _to_mercator(longitude, latitude)
    half_width = (width_km * 1000.0 * mercator_metres_per_ground_metre(latitude)) / 2.0
    half_height = half_width * (height / width)
    bbox = (
        f"{centre_x - half_width:.1f},{centre_y - half_height:.1f},"
        f"{centre_x + half_width:.1f},{centre_y + half_height:.1f}"
    )

    layer = radar_layer(latitude, longitude)
    basemap = fetch_binary(
        _radar_request(RADAR_BOUNDARY_LAYER, bbox, width, height), TTL_LEGEND
    )

    try:
        moments: list[datetime | None] = list(
            _sample_scan_times(radar_scan_times(layer), count, RADAR_SPAN)
        )
    except WeatherError as exc:
        # The imagery is still worth showing without its scan times; the view
        # simply cannot animate or caption the frame it is displaying.
        log.debug("radar scan times unavailable: %s", exc)
        moments = []
    if not moments:
        moments = [None]

    echoes: list[RadarEcho] = []
    for moment in moments:
        url = _radar_request(layer, bbox, width, height, moment)
        try:
            payload = fetch_binary(url, TTL_RADAR if moment is None else TTL_RADAR_FRAME)
        except WeatherError as exc:
            # One missing frame should shorten the animation, not lose it --
            # unless it was the only one asked for.
            log.debug("radar frame %s unavailable: %s", moment, exc)
            continue
        echoes.append(RadarEcho(reflectivity=payload, valid_at=moment))

    if not echoes:
        raise WeatherError("The radar service returned no imagery.")

    if include_clouds and clouds_available(latitude, longitude):
        _attach_clouds(echoes, bbox, width, height)

    return RadarSequence(
        basemap=basemap,
        echoes=echoes,
        width=width,
        height=height,
        width_km=width_km,
        fetched_at=datetime.now(timezone.utc),
    )


def _attach_clouds(
    echoes: list[RadarEcho], bbox: str, width: int, height: int
) -> None:
    """Pair each echo with the GOES infrared frame nearest its scan time.

    Clouds are garnish on the radar view: any frame that cannot be fetched is
    simply left bare rather than failing the sequence. GOES publishes about
    every five minutes against the mosaics' two, so the worst mismatch
    between an echo and its clouds is around two and a half minutes.
    """
    try:
        available = radar_scan_times(CLOUDS_LAYER, server=CLOUDS_SERVER)
    except WeatherError as exc:
        log.debug("cloud scan times unavailable: %s", exc)
        available = []

    for echo in echoes:
        moment: datetime | None = None
        if echo.valid_at is not None:
            if not available:
                # Without advertised times, an untimed request would put the
                # current clouds under a past scan; better none at all.
                continue
            moment = min(available, key=lambda m: abs(m - echo.valid_at))
        url = _radar_request(
            CLOUDS_LAYER, bbox, width, height, moment, base=CLOUDS_BASE
        )
        try:
            echo.clouds = fetch_binary(
                url, TTL_RADAR if moment is None else TTL_RADAR_FRAME
            )
        except WeatherError as exc:
            log.debug("cloud frame %s unavailable: %s", moment, exc)


def fetch_radar_legend() -> bytes:
    """The dBZ colour ramp for the reflectivity layers."""
    query = urllib.parse.urlencode(
        {
            "service": "WMS",
            "version": "1.3.0",
            "request": "GetLegendGraphic",
            "layer": _RADAR_REGIONS[-1][0],
            "format": "image/png",
        }
    )
    return fetch_binary(f"{RADAR_BASE}?{query}", TTL_LEGEND)
