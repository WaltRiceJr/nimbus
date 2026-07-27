"""Astronomical calculations for sun and moon.

Pure standard-library math -- no external dependencies. Algorithms follow
Jean Meeus, *Astronomical Algorithms* (2nd ed.), using the low-precision
solar and lunar series. Accuracy is roughly +/-1 minute for rise/set times
and better than 0.5 degrees for the moon's position, which is far more
precision than a weather display needs.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo

RAD = math.pi / 180.0
DEG = 180.0 / math.pi

#: Julian date of the J2000.0 epoch (2000 Jan 1, 12:00 TT).
J2000 = 2451545.0

#: Standard refraction-corrected altitude of the solar/lunar disc centre at
#: the moment the upper limb touches the horizon.
SUN_HORIZON = -0.833
MOON_HORIZON = 0.125
CIVIL_TWILIGHT = -6.0
NAUTICAL_TWILIGHT = -12.0
ASTRONOMICAL_TWILIGHT = -18.0

MOON_SYNODIC_MONTH = 29.530588853

PHASE_NAMES = (
    "New Moon",
    "Waxing Crescent",
    "First Quarter",
    "Waxing Gibbous",
    "Full Moon",
    "Waning Gibbous",
    "Last Quarter",
    "Waning Crescent",
)


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------


def to_julian(moment: datetime) -> float:
    """Convert an aware datetime to a Julian date."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    stamp = moment.astimezone(timezone.utc)
    year, month = stamp.year, stamp.month
    day = (
        stamp.day
        + (stamp.hour + (stamp.minute + (stamp.second + stamp.microsecond / 1e6) / 60.0) / 60.0)
        / 24.0
    )
    if month <= 2:
        year -= 1
        month += 12
    a = year // 100
    b = 2 - a + a // 4
    return (
        math.floor(365.25 * (year + 4716))
        + math.floor(30.6001 * (month + 1))
        + day
        + b
        - 1524.5
    )


def from_julian(jd: float) -> datetime:
    """Convert a Julian date back to an aware UTC datetime."""
    jd = jd + 0.5
    z = math.floor(jd)
    frac = jd - z
    if z < 2299161:
        a = z
    else:
        alpha = math.floor((z - 1867216.25) / 36524.25)
        a = z + 1 + alpha - math.floor(alpha / 4)
    b = a + 1524
    c = math.floor((b - 122.1) / 365.25)
    d = math.floor(365.25 * c)
    e = math.floor((b - d) / 30.6001)

    day = b - d - math.floor(30.6001 * e) + frac
    month = e - 1 if e < 14 else e - 13
    year = c - 4716 if month > 2 else c - 4715

    day_int = int(math.floor(day))
    seconds = (day - day_int) * 86400.0
    base = datetime(int(year), int(month), day_int, tzinfo=timezone.utc)
    return base + timedelta(seconds=seconds)


def julian_centuries(jd: float) -> float:
    """Julian centuries since J2000.0."""
    return (jd - J2000) / 36525.0


def _norm360(angle: float) -> float:
    return angle % 360.0


def greenwich_sidereal(jd: float) -> float:
    """Apparent Greenwich mean sidereal time in degrees."""
    t = julian_centuries(jd)
    theta = (
        280.46061837
        + 360.98564736629 * (jd - J2000)
        + 0.000387933 * t * t
        - t * t * t / 38710000.0
    )
    return _norm360(theta)


# ---------------------------------------------------------------------------
# Equatorial coordinates
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Equatorial:
    """Right ascension and declination, both in degrees, plus distance in km."""

    right_ascension: float
    declination: float
    distance: float = 0.0


def obliquity(t: float) -> float:
    """Mean obliquity of the ecliptic in degrees, corrected for nutation."""
    seconds = 21.448 - t * (46.8150 + t * (0.00059 - t * 0.001813))
    eps0 = 23.0 + (26.0 + seconds / 60.0) / 60.0
    omega = 125.04 - 1934.136 * t
    return eps0 + 0.00256 * math.cos(omega * RAD)


def sun_position(jd: float) -> Equatorial:
    """Apparent geocentric equatorial coordinates of the sun."""
    t = julian_centuries(jd)

    # Geometric mean longitude and anomaly.
    mean_lon = _norm360(280.46646 + t * (36000.76983 + t * 0.0003032))
    mean_anom = 357.52911 + t * (35999.05029 - 0.0001537 * t)
    eccentricity = 0.016708634 - t * (0.000042037 + 0.0000001267 * t)

    m_rad = mean_anom * RAD
    centre = (
        (1.914602 - t * (0.004817 + 0.000014 * t)) * math.sin(m_rad)
        + (0.019993 - 0.000101 * t) * math.sin(2 * m_rad)
        + 0.000289 * math.sin(3 * m_rad)
    )

    true_lon = mean_lon + centre
    true_anom = mean_anom + centre

    # Radius vector in astronomical units.
    radius = (1.000001018 * (1 - eccentricity * eccentricity)) / (
        1 + eccentricity * math.cos(true_anom * RAD)
    )

    omega = 125.04 - 1934.136 * t
    apparent_lon = true_lon - 0.00569 - 0.00478 * math.sin(omega * RAD)

    eps = obliquity(t)
    lam = apparent_lon * RAD
    eps_rad = eps * RAD

    ra = math.atan2(math.cos(eps_rad) * math.sin(lam), math.cos(lam)) * DEG
    dec = math.asin(math.sin(eps_rad) * math.sin(lam)) * DEG
    return Equatorial(_norm360(ra), dec, radius * 149597870.7)


def moon_position(jd: float) -> Equatorial:
    """Apparent geocentric equatorial coordinates of the moon.

    Uses the principal periodic terms of the ELP-2000/82 truncation given by
    Meeus, which is accurate to roughly 10 arcminutes -- ample for phase
    rendering and rise/set times.
    """
    t = julian_centuries(jd)

    # Fundamental arguments (degrees).
    lp = _norm360(218.3164477 + 481267.88123421 * t - 0.0015786 * t * t)
    d = _norm360(297.8501921 + 445267.1114034 * t - 0.0018819 * t * t)
    m = _norm360(357.5291092 + 35999.0502909 * t - 0.0001536 * t * t)
    mp = _norm360(134.9633964 + 477198.8675055 * t + 0.0087414 * t * t)
    f = _norm360(93.2720950 + 483202.0175233 * t - 0.0036539 * t * t)

    d_r, m_r, mp_r, f_r = d * RAD, m * RAD, mp * RAD, f * RAD

    # Longitude (degrees) -- leading terms.
    lon = lp + (
        6.288774 * math.sin(mp_r)
        + 1.274027 * math.sin(2 * d_r - mp_r)
        + 0.658314 * math.sin(2 * d_r)
        + 0.213618 * math.sin(2 * mp_r)
        - 0.185116 * math.sin(m_r)
        - 0.114332 * math.sin(2 * f_r)
        + 0.058793 * math.sin(2 * d_r - 2 * mp_r)
        + 0.057066 * math.sin(2 * d_r - m_r - mp_r)
        + 0.053322 * math.sin(2 * d_r + mp_r)
        + 0.045758 * math.sin(2 * d_r - m_r)
        - 0.040923 * math.sin(m_r - mp_r)
        - 0.034720 * math.sin(d_r)
        - 0.030383 * math.sin(m_r + mp_r)
        + 0.015327 * math.sin(2 * d_r - 2 * f_r)
        - 0.012528 * math.sin(mp_r + 2 * f_r)
        + 0.010980 * math.sin(mp_r - 2 * f_r)
        + 0.010675 * math.sin(4 * d_r - mp_r)
        + 0.010034 * math.sin(3 * mp_r)
    )

    # Latitude (degrees) -- leading terms.
    lat = (
        5.128122 * math.sin(f_r)
        + 0.280602 * math.sin(mp_r + f_r)
        + 0.277693 * math.sin(mp_r - f_r)
        + 0.173237 * math.sin(2 * d_r - f_r)
        + 0.055413 * math.sin(2 * d_r - mp_r + f_r)
        + 0.046271 * math.sin(2 * d_r - mp_r - f_r)
        + 0.032573 * math.sin(2 * d_r + f_r)
        + 0.017198 * math.sin(2 * mp_r + f_r)
        + 0.009266 * math.sin(2 * d_r + mp_r - f_r)
    )

    # Distance in kilometres.
    dist = (
        385000.56
        - 20905.355 * math.cos(mp_r)
        - 3699.111 * math.cos(2 * d_r - mp_r)
        - 2955.968 * math.cos(2 * d_r)
        - 569.925 * math.cos(2 * mp_r)
        + 48.888 * math.cos(m_r)
        - 152.138 * math.cos(2 * d_r - 2 * mp_r)
        - 170.733 * math.cos(2 * d_r + mp_r)
        - 204.586 * math.cos(2 * d_r - m_r)
    )

    eps = obliquity(t) * RAD
    lam, beta = lon * RAD, lat * RAD

    ra = math.atan2(
        math.sin(lam) * math.cos(eps) - math.tan(beta) * math.sin(eps), math.cos(lam)
    ) * DEG
    dec = math.asin(
        math.sin(beta) * math.cos(eps) + math.cos(beta) * math.sin(eps) * math.sin(lam)
    ) * DEG
    return Equatorial(_norm360(ra), dec, dist)


def altitude(body: Equatorial, jd: float, latitude: float, longitude: float) -> float:
    """Altitude of *body* above the horizon, in degrees."""
    hour_angle = (greenwich_sidereal(jd) + longitude - body.right_ascension) * RAD
    lat_r = latitude * RAD
    dec_r = body.declination * RAD
    sin_alt = math.sin(lat_r) * math.sin(dec_r) + math.cos(lat_r) * math.cos(
        dec_r
    ) * math.cos(hour_angle)
    return math.asin(max(-1.0, min(1.0, sin_alt))) * DEG


def azimuth(body: Equatorial, jd: float, latitude: float, longitude: float) -> float:
    """Azimuth of *body* measured clockwise from true north, in degrees."""
    hour_angle = (greenwich_sidereal(jd) + longitude - body.right_ascension) * RAD
    lat_r = latitude * RAD
    dec_r = body.declination * RAD
    y = math.sin(hour_angle)
    x = math.cos(hour_angle) * math.sin(lat_r) - math.tan(dec_r) * math.cos(lat_r)
    return _norm360(math.atan2(y, x) * DEG + 180.0)


# ---------------------------------------------------------------------------
# Rise / set search
# ---------------------------------------------------------------------------


def _crossings(
    position_fn,
    start_jd: float,
    latitude: float,
    longitude: float,
    target_alt: float,
    hours: float = 24.0,
    step_minutes: float = 10.0,
) -> tuple[float | None, float | None]:
    """Scan for the first rising and setting crossing of *target_alt*.

    Returns ``(rise_jd, set_jd)``; either may be ``None`` when the body does
    not cross the given altitude during the window (polar day/night, or a
    moon that simply does not rise on that date).
    """
    step = step_minutes / (24.0 * 60.0)
    steps = int(hours * 60.0 / step_minutes)

    rise_jd: float | None = None
    set_jd: float | None = None

    prev_jd = start_jd
    prev_alt = altitude(position_fn(prev_jd), prev_jd, latitude, longitude) - target_alt

    for i in range(1, steps + 1):
        cur_jd = start_jd + i * step
        cur_alt = altitude(position_fn(cur_jd), cur_jd, latitude, longitude) - target_alt

        if prev_alt * cur_alt < 0:
            # Refine by bisection; 20 iterations lands well under a second.
            lo, hi = prev_jd, cur_jd
            lo_alt = prev_alt
            for _ in range(20):
                mid = (lo + hi) / 2.0
                mid_alt = (
                    altitude(position_fn(mid), mid, latitude, longitude) - target_alt
                )
                if lo_alt * mid_alt <= 0:
                    hi = mid
                else:
                    lo, lo_alt = mid, mid_alt
            crossing = (lo + hi) / 2.0
            if prev_alt < 0 and rise_jd is None:
                rise_jd = crossing
            elif prev_alt > 0 and set_jd is None:
                set_jd = crossing

        prev_jd, prev_alt = cur_jd, cur_alt
        if rise_jd is not None and set_jd is not None:
            break

    return rise_jd, set_jd


# ---------------------------------------------------------------------------
# Public results
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SunTimes:
    """Solar event times for one local day, as aware datetimes."""

    sunrise: datetime | None
    sunset: datetime | None
    solar_noon: datetime | None
    dawn: datetime | None
    dusk: datetime | None
    day_length: timedelta | None
    altitude_now: float
    azimuth_now: float

    @property
    def is_daytime(self) -> bool:
        return self.altitude_now > SUN_HORIZON


@dataclass(frozen=True)
class MoonInfo:
    """Lunar phase and event times."""

    phase: float
    """Position in the synodic cycle, 0.0 = new, 0.5 = full, wrapping at 1.0."""

    illumination: float
    """Fraction of the disc lit, 0.0 to 1.0."""

    name: str
    age_days: float
    distance_km: float
    moonrise: datetime | None
    moonset: datetime | None
    altitude_now: float
    azimuth_now: float

    @property
    def is_waxing(self) -> bool:
        return self.phase < 0.5


def _local_midnight(moment: datetime, tz: tzinfo) -> datetime:
    local = moment.astimezone(tz)
    return local.replace(hour=0, minute=0, second=0, microsecond=0)


def sun_times(
    moment: datetime, latitude: float, longitude: float, tz: tzinfo | None = None
) -> SunTimes:
    """Compute sunrise, sunset, twilight and solar noon for the local day."""
    tz = tz or moment.tzinfo or timezone.utc
    midnight = _local_midnight(moment, tz)
    start_jd = to_julian(midnight)

    rise_jd, set_jd = _crossings(
        sun_position, start_jd, latitude, longitude, SUN_HORIZON
    )
    dawn_jd, dusk_jd = _crossings(
        sun_position, start_jd, latitude, longitude, CIVIL_TWILIGHT
    )

    noon_dt: datetime | None = None
    if rise_jd is not None and set_jd is not None:
        noon_dt = from_julian((rise_jd + set_jd) / 2.0).astimezone(tz)

    length: timedelta | None = None
    if rise_jd is not None and set_jd is not None and set_jd > rise_jd:
        length = timedelta(days=set_jd - rise_jd)

    now_jd = to_julian(moment)
    sun_now = sun_position(now_jd)

    return SunTimes(
        sunrise=from_julian(rise_jd).astimezone(tz) if rise_jd else None,
        sunset=from_julian(set_jd).astimezone(tz) if set_jd else None,
        solar_noon=noon_dt,
        dawn=from_julian(dawn_jd).astimezone(tz) if dawn_jd else None,
        dusk=from_julian(dusk_jd).astimezone(tz) if dusk_jd else None,
        day_length=length,
        altitude_now=altitude(sun_now, now_jd, latitude, longitude),
        azimuth_now=azimuth(sun_now, now_jd, latitude, longitude),
    )


def moon_info(
    moment: datetime, latitude: float, longitude: float, tz: tzinfo | None = None
) -> MoonInfo:
    """Compute the moon's phase, illumination and rise/set for the local day."""
    tz = tz or moment.tzinfo or timezone.utc
    now_jd = to_julian(moment)

    sun = sun_position(now_jd)
    moon = moon_position(now_jd)

    # Phase angle from the geocentric elongation between sun and moon.
    sun_ra, sun_dec = sun.right_ascension * RAD, sun.declination * RAD
    moon_ra, moon_dec = moon.right_ascension * RAD, moon.declination * RAD

    elongation = math.acos(
        max(
            -1.0,
            min(
                1.0,
                math.sin(sun_dec) * math.sin(moon_dec)
                + math.cos(sun_dec) * math.cos(moon_dec) * math.cos(sun_ra - moon_ra),
            ),
        )
    )
    phase_angle = math.atan2(
        sun.distance * math.sin(elongation),
        moon.distance - sun.distance * math.cos(elongation),
    )
    illumination = (1.0 + math.cos(phase_angle)) / 2.0

    # Waxing or waning is decided by the sign of the difference in ecliptic
    # longitude between moon and sun.
    t = julian_centuries(now_jd)
    moon_lon = _norm360(218.3164477 + 481267.88123421 * t)
    sun_lon = _norm360(280.46646 + 36000.76983 * t)
    delta = _norm360(moon_lon - sun_lon)
    waxing = delta < 180.0

    # Map illumination onto a 0..1 synodic position. The phase angle alone is
    # ambiguous -- it gives the same value either side of full -- so the
    # elongation sign picks the branch.
    offset = math.acos(max(-1.0, min(1.0, 2 * illumination - 1))) / (2 * math.pi)
    phase = (0.5 - offset if waxing else 0.5 + offset) % 1.0

    index = int((phase * 8.0) + 0.5) % 8
    name = PHASE_NAMES[index]

    midnight = _local_midnight(moment, tz)
    start_jd = to_julian(midnight)
    rise_jd, set_jd = _crossings(
        moon_position, start_jd, latitude, longitude, MOON_HORIZON
    )

    return MoonInfo(
        phase=phase,
        illumination=illumination,
        name=name,
        age_days=phase * MOON_SYNODIC_MONTH,
        distance_km=moon.distance,
        moonrise=from_julian(rise_jd).astimezone(tz) if rise_jd else None,
        moonset=from_julian(set_jd).astimezone(tz) if set_jd else None,
        altitude_now=altitude(moon, now_jd, latitude, longitude),
        azimuth_now=azimuth(moon, now_jd, latitude, longitude),
    )


def day_progress(times: SunTimes, moment: datetime) -> float:
    """How far through the daylight span *moment* falls, clamped to 0..1.

    Used to place the sun along its arc in the sky illustration.
    """
    if not times.sunrise or not times.sunset:
        return 0.5
    span = (times.sunset - times.sunrise).total_seconds()
    if span <= 0:
        return 0.5
    elapsed = (moment - times.sunrise).total_seconds()
    return max(0.0, min(1.0, elapsed / span))
