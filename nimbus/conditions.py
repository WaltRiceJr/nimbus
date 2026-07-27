"""Condition classification and the sky colour system.

Two jobs live here. First, distilling the NWS icon vocabulary into our
:class:`~nimbus.model.Condition` enum. Second, turning a condition plus the
sun's altitude into the gradient the sky widget paints.

The colour model is continuous rather than bucketed: a clear-sky gradient is
interpolated between anchor points keyed on solar altitude, then blended
toward a condition tint. That means dusk actually *fades* rather than
snapping between a "day" and a "night" theme.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .model import Cloudiness, Condition

RGB = tuple[float, float, float]


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

#: NWS icon path codes mapped to canonical conditions. Order matters only in
#: that longer codes must be checked before their prefixes.
_ICON_CODES: dict[str, Condition] = {
    "skc": Condition.CLEAR,
    "few": Condition.FEW_CLOUDS,
    "sct": Condition.PARTLY_CLOUDY,
    "bkn": Condition.MOSTLY_CLOUDY,
    "ovc": Condition.OVERCAST,
    "wind_skc": Condition.WIND,
    "wind_few": Condition.WIND,
    "wind_sct": Condition.WIND,
    "wind_bkn": Condition.WIND,
    "wind_ovc": Condition.WIND,
    "snow": Condition.SNOW,
    "rain_snow": Condition.SNOW,
    "rain_sleet": Condition.SLEET,
    "snow_sleet": Condition.SLEET,
    "sleet": Condition.SLEET,
    "fzra": Condition.FREEZING_RAIN,
    "rain_fzra": Condition.FREEZING_RAIN,
    "snow_fzra": Condition.FREEZING_RAIN,
    "rain": Condition.RAIN,
    "rain_showers": Condition.SHOWERS,
    "rain_showers_hi": Condition.SHOWERS,
    "tsra": Condition.THUNDERSTORM,
    "tsra_sct": Condition.THUNDERSTORM,
    "tsra_hi": Condition.THUNDERSTORM,
    "tornado": Condition.TORNADO,
    "hurricane": Condition.HURRICANE,
    "tropical_storm": Condition.HURRICANE,
    "dust": Condition.DUST,
    "smoke": Condition.SMOKE,
    "haze": Condition.HAZE,
    "hot": Condition.HOT,
    "cold": Condition.COLD,
    "blizzard": Condition.BLIZZARD,
    "fog": Condition.FOG,
}

#: Fallback keyword matching against free-text forecast summaries, checked in
#: order so that "freezing rain" wins over plain "rain".
_TEXT_RULES: tuple[tuple[str, Condition], ...] = (
    (r"tornado", Condition.TORNADO),
    (r"hurricane|tropical storm", Condition.HURRICANE),
    (r"blizzard", Condition.BLIZZARD),
    (r"freezing rain|freezing drizzle", Condition.FREEZING_RAIN),
    (r"sleet|ice pellets|wintry mix", Condition.SLEET),
    (r"thunder|t-storm", Condition.THUNDERSTORM),
    (r"snow|flurries", Condition.SNOW),
    (r"shower|drizzle", Condition.SHOWERS),
    (r"rain", Condition.RAIN),
    (r"\bfog\b|mist", Condition.FOG),
    (r"haze", Condition.HAZE),
    (r"smoke", Condition.SMOKE),
    (r"dust|sand", Condition.DUST),
    (r"blustery|windy|breezy", Condition.WIND),
    (r"overcast", Condition.OVERCAST),
    (r"mostly cloudy|considerable cloud", Condition.MOSTLY_CLOUDY),
    (r"partly cloudy|partly sunny", Condition.PARTLY_CLOUDY),
    (r"mostly sunny|mostly clear|a few clouds", Condition.FEW_CLOUDS),
    (r"sunny|clear|fair", Condition.CLEAR),
)

_ICON_RE = re.compile(r"/icons/(?:land|water)/(day|night)/([a-z_]+)")


def classify(icon_url: str | None, text: str | None = None) -> Condition:
    """Determine the canonical condition from an NWS icon URL or summary text.

    Dual-condition icons such as ``/day/bkn/rain,60`` describe the first and
    second half of a period; we take the second, more significant half when
    it is present since that is what the period is remembered for.
    """
    if icon_url:
        matches = _ICON_RE.search(icon_url)
        if matches:
            tail = icon_url[matches.start(2) :]
            tail = tail.split("?")[0]
            # Split dual icons, drop the ",NN" probability suffixes.
            segments = [seg.split(",")[0] for seg in tail.split("/") if seg]
            for segment in reversed(segments):
                if segment in _ICON_CODES:
                    return _ICON_CODES[segment]

    if text:
        lowered = text.lower()
        for pattern, condition in _TEXT_RULES:
            if re.search(pattern, lowered):
                return condition

    return Condition.UNKNOWN


def is_night_icon(icon_url: str | None) -> bool:
    """True when the NWS icon URL refers to the night variant."""
    matches = _ICON_RE.search(icon_url or "")
    return bool(matches and matches.group(1) == "night")


#: How much sky each condition should cover with cloud in the illustration.
CLOUDINESS: dict[Condition, Cloudiness] = {
    Condition.CLEAR: Cloudiness.NONE,
    Condition.HOT: Cloudiness.NONE,
    Condition.COLD: Cloudiness.NONE,
    Condition.FEW_CLOUDS: Cloudiness.LIGHT,
    Condition.WIND: Cloudiness.LIGHT,
    Condition.HAZE: Cloudiness.LIGHT,
    Condition.SMOKE: Cloudiness.LIGHT,
    Condition.DUST: Cloudiness.LIGHT,
    Condition.PARTLY_CLOUDY: Cloudiness.MEDIUM,
    Condition.MOSTLY_CLOUDY: Cloudiness.HEAVY,
    Condition.SHOWERS: Cloudiness.HEAVY,
    Condition.OVERCAST: Cloudiness.TOTAL,
    Condition.FOG: Cloudiness.TOTAL,
    Condition.RAIN: Cloudiness.TOTAL,
    Condition.THUNDERSTORM: Cloudiness.TOTAL,
    Condition.SNOW: Cloudiness.TOTAL,
    Condition.SLEET: Cloudiness.TOTAL,
    Condition.FREEZING_RAIN: Cloudiness.TOTAL,
    Condition.BLIZZARD: Cloudiness.TOTAL,
    Condition.TORNADO: Cloudiness.TOTAL,
    Condition.HURRICANE: Cloudiness.TOTAL,
    Condition.UNKNOWN: Cloudiness.LIGHT,
}


# ---------------------------------------------------------------------------
# Colour
# ---------------------------------------------------------------------------


def _hex(value: str) -> RGB:
    value = value.lstrip("#")
    return (
        int(value[0:2], 16) / 255.0,
        int(value[2:4], 16) / 255.0,
        int(value[4:6], 16) / 255.0,
    )


def mix(a: RGB, b: RGB, amount: float) -> RGB:
    """Linear blend from *a* to *b*."""
    amount = max(0.0, min(1.0, amount))
    return (
        a[0] + (b[0] - a[0]) * amount,
        a[1] + (b[1] - a[1]) * amount,
        a[2] + (b[2] - a[2]) * amount,
    )


def luminance(color: RGB) -> float:
    """Perceptual relative luminance, used to pick readable foregrounds."""
    def channel(c: float) -> float:
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (channel(c) for c in color)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


@dataclass(frozen=True)
class SkyPalette:
    """The colours the sky widget needs for one moment in time."""

    top: RGB
    middle: RGB
    bottom: RGB
    #: Glow colour radiating from the sun or moon, dimmed by cloud cover.
    glow: RGB
    #: The undimmed solar tint. Cloud cover reduces how far the glow carries,
    #: but the disc itself stays warm rather than going grey.
    sun_core: RGB
    #: Sunlit face of clouds.
    cloud_light: RGB
    #: Shadowed underside of clouds.
    cloud_dark: RGB
    #: Precipitation streaks and flakes.
    precip: RGB
    #: How visible the stars are, 0..1.
    star_opacity: float
    #: True while the sun is above the horizon.
    is_day: bool

    @property
    def accent(self) -> RGB:
        return self.glow


#: Clear-sky gradients anchored on solar altitude in degrees, brightest first.
#: Each entry is (altitude, top, middle, bottom, glow).
_SKY_ANCHORS: tuple[tuple[float, RGB, RGB, RGB, RGB], ...] = (
    (
        60.0,
        _hex("0d5cba"), _hex("3b93e0"), _hex("9fd4f5"), _hex("fff6d8"),
    ),
    (
        25.0,
        _hex("1668c8"), _hex("4a9de6"), _hex("b3ddf7"), _hex("fff2c9"),
    ),
    (
        8.0,
        _hex("2374cc"), _hex("6bb2ea"), _hex("d3e9f8"), _hex("ffe8a8"),
    ),
    (
        2.0,
        _hex("2f5fa8"), _hex("d4884f"), _hex("ffc98a"), _hex("ffcf7a"),
    ),
    (
        -1.0,
        _hex("223f7e"), _hex("cf6242"), _hex("ff9e63"), _hex("ff9d4d"),
    ),
    (
        -6.0,
        _hex("14204a"), _hex("59356b"), _hex("c9525f"), _hex("e0644f"),
    ),
    (
        -12.0,
        _hex("0a1030"), _hex("1d2450"), _hex("46305c"), _hex("7a4a63"),
    ),
    (
        -18.0,
        _hex("05070f"), _hex("0b1024"), _hex("161d38"), _hex("2a3358"),
    ),
)


def _clear_sky(sun_altitude: float) -> tuple[RGB, RGB, RGB, RGB]:
    """Interpolate the clear-sky gradient for a given solar altitude."""
    anchors = _SKY_ANCHORS
    if sun_altitude >= anchors[0][0]:
        return anchors[0][1:]
    if sun_altitude <= anchors[-1][0]:
        return anchors[-1][1:]

    for i in range(len(anchors) - 1):
        upper, lower = anchors[i], anchors[i + 1]
        if lower[0] <= sun_altitude <= upper[0]:
            span = upper[0] - lower[0]
            # 0 at the lower anchor, 1 at the upper one.
            t = (sun_altitude - lower[0]) / span if span else 0.0
            return tuple(  # type: ignore[return-value]
                mix(lower[1 + k], upper[1 + k], t) for k in range(4)
            )
    return anchors[-1][1:]


#: Per-condition tint and how strongly it overrides the clear sky.
#: (tint colour, blend strength 0..1, extra darkening 0..1)
_CONDITION_TINT: dict[Condition, tuple[RGB, float, float]] = {
    Condition.CLEAR: (_hex("000000"), 0.0, 0.0),
    Condition.HOT: (_hex("ffb057"), 0.16, 0.0),
    Condition.COLD: (_hex("bcd9f0"), 0.14, 0.0),
    Condition.FEW_CLOUDS: (_hex("9fb4c9"), 0.08, 0.02),
    Condition.PARTLY_CLOUDY: (_hex("94a9bd"), 0.18, 0.05),
    Condition.MOSTLY_CLOUDY: (_hex("7b8b9c"), 0.38, 0.10),
    Condition.OVERCAST: (_hex("6b7885"), 0.60, 0.16),
    Condition.WIND: (_hex("8fa8bb"), 0.20, 0.04),
    Condition.HAZE: (_hex("c2b49a"), 0.34, 0.06),
    Condition.SMOKE: (_hex("9b8a76"), 0.44, 0.12),
    Condition.DUST: (_hex("c9a878"), 0.44, 0.10),
    Condition.FOG: (_hex("aab4bc"), 0.66, 0.10),
    Condition.SHOWERS: (_hex("5f7183"), 0.52, 0.18),
    Condition.RAIN: (_hex("4d5f72"), 0.64, 0.24),
    Condition.THUNDERSTORM: (_hex("39424f"), 0.72, 0.32),
    Condition.SNOW: (_hex("8f9bb0"), 0.58, 0.12),
    Condition.SLEET: (_hex("77869b"), 0.60, 0.18),
    Condition.FREEZING_RAIN: (_hex("6d7d94"), 0.62, 0.20),
    Condition.BLIZZARD: (_hex("7c8798"), 0.70, 0.20),
    Condition.TORNADO: (_hex("434436"), 0.74, 0.34),
    Condition.HURRICANE: (_hex("3d4a55"), 0.74, 0.32),
    Condition.UNKNOWN: (_hex("8fa0b0"), 0.20, 0.04),
}


def palette(condition: Condition, sun_altitude: float) -> SkyPalette:
    """Build the sky palette for *condition* at the given solar altitude."""
    top, middle, bottom, glow = _clear_sky(sun_altitude)
    sun_core = glow
    tint, strength, darken = _CONDITION_TINT.get(
        condition, _CONDITION_TINT[Condition.UNKNOWN]
    )

    is_day = sun_altitude > -0.833

    # Overcast skies flatten out: the tint applies more strongly toward the
    # horizon, which is where a clear sky would otherwise still be bright.
    top = mix(top, tint, strength * 0.75)
    middle = mix(middle, tint, strength)
    bottom = mix(bottom, tint, strength * 1.05)

    if darken:
        black = (0.0, 0.0, 0.0)
        top = mix(top, black, darken)
        middle = mix(middle, black, darken * 0.8)
        bottom = mix(bottom, black, darken * 0.6)

    # Thick cloud dims the glow from the sun or moon.
    glow = mix(glow, middle, strength * 0.6)

    # Cloud shading tracks the ambient light so clouds sit in the scene
    # rather than floating on top of it.
    if is_day:
        cloud_light = mix(_hex("ffffff"), glow, 0.28)
        cloud_dark = mix(_hex("8d9aa8"), middle, 0.40)
    else:
        cloud_light = mix(_hex("6a7488"), glow, 0.30)
        cloud_dark = mix(_hex("2a3040"), middle, 0.45)

    if condition in (Condition.THUNDERSTORM, Condition.TORNADO, Condition.HURRICANE):
        cloud_light = mix(cloud_light, _hex("55606d"), 0.5)
        cloud_dark = mix(cloud_dark, _hex("1d232c"), 0.5)

    precip = _hex("cfe4f5") if not condition.is_frozen else _hex("ffffff")
    if not is_day:
        precip = mix(precip, middle, 0.35)

    # Stars fade in through nautical twilight and are hidden by thick cloud.
    star_opacity = 0.0
    if sun_altitude < -6.0:
        star_opacity = min(1.0, (-6.0 - sun_altitude) / 8.0)
    star_opacity *= max(0.0, 1.0 - strength * 1.25)

    return SkyPalette(
        top=top,
        middle=middle,
        bottom=bottom,
        glow=glow,
        sun_core=sun_core,
        cloud_light=cloud_light,
        cloud_dark=cloud_dark,
        precip=precip,
        star_opacity=star_opacity,
        is_day=is_day,
    )


#: Short, human-facing names for each condition, used where the NWS free text
#: is missing or too long for the space available.
DISPLAY_NAMES: dict[Condition, str] = {
    Condition.CLEAR: "Clear",
    Condition.FEW_CLOUDS: "Mostly Clear",
    Condition.PARTLY_CLOUDY: "Partly Cloudy",
    Condition.MOSTLY_CLOUDY: "Mostly Cloudy",
    Condition.OVERCAST: "Overcast",
    Condition.FOG: "Fog",
    Condition.HAZE: "Haze",
    Condition.SMOKE: "Smoke",
    Condition.DUST: "Blowing Dust",
    Condition.WIND: "Windy",
    Condition.RAIN: "Rain",
    Condition.SHOWERS: "Showers",
    Condition.THUNDERSTORM: "Thunderstorms",
    Condition.SNOW: "Snow",
    Condition.SLEET: "Sleet",
    Condition.FREEZING_RAIN: "Freezing Rain",
    Condition.BLIZZARD: "Blizzard",
    Condition.HOT: "Hot",
    Condition.COLD: "Cold",
    Condition.TORNADO: "Tornado",
    Condition.HURRICANE: "Hurricane",
    Condition.UNKNOWN: "Unknown",
}


def display_name(condition: Condition) -> str:
    return DISPLAY_NAMES.get(condition, "Unknown")
