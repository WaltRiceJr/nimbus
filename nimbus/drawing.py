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

"""Cairo drawing primitives and the weather glyph set.

Every weather symbol in the app is drawn as vector art rather than loaded
from an icon theme, so glyphs stay crisp at any size, share one visual
language, and can be tinted to match the sky behind them.

Glyphs are drawn into a square box of a given ``size`` with the origin at the
top-left corner. Helpers take colours as ``(r, g, b)`` floats.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import cairo

from .conditions import RGB, mix
from .model import Condition

TAU = math.pi * 2
ROUND = cairo.LINE_CAP_ROUND


# ---------------------------------------------------------------------------
# Colour helpers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GlyphColors:
    """The four tones a weather glyph is built from."""

    sun: RGB = (1.0, 0.78, 0.28)
    sun_bright: RGB = (1.0, 0.90, 0.55)
    moon: RGB = (0.94, 0.95, 0.99)
    cloud_light: RGB = (0.99, 1.0, 1.0)
    cloud_dark: RGB = (0.68, 0.74, 0.82)
    rain: RGB = (0.42, 0.68, 0.94)
    snow: RGB = (0.86, 0.93, 1.0)
    bolt: RGB = (1.0, 0.82, 0.30)
    fog: RGB = (0.78, 0.83, 0.88)
    wind: RGB = (0.75, 0.83, 0.90)


DEFAULT_COLORS = GlyphColors()

#: A brighter variant for use on dark or saturated backgrounds, where the
#: default cloud grey would otherwise disappear.
ON_SKY_COLORS = GlyphColors(
    cloud_light=(1.0, 1.0, 1.0),
    cloud_dark=(0.80, 0.85, 0.92),
    rain=(0.72, 0.86, 1.0),
    fog=(0.88, 0.92, 0.96),
    wind=(0.90, 0.94, 0.98),
)

#: A darker variant for pale backgrounds, where near-white clouds would wash
#: out. Used at the daylight end of the hourly strip.
ON_LIGHT_COLORS = GlyphColors(
    sun=(0.96, 0.70, 0.13),
    sun_bright=(1.0, 0.83, 0.36),
    moon=(0.62, 0.66, 0.76),
    cloud_light=(0.80, 0.85, 0.91),
    cloud_dark=(0.52, 0.59, 0.69),
    rain=(0.25, 0.52, 0.82),
    snow=(0.55, 0.68, 0.85),
    bolt=(0.93, 0.68, 0.10),
    fog=(0.55, 0.62, 0.70),
    wind=(0.52, 0.60, 0.70),
)


def blend_colors(a: GlyphColors, b: GlyphColors, amount: float) -> GlyphColors:
    """Interpolate every tone of two glyph palettes.

    Lets a glyph track a background that shifts continuously, such as the
    hourly strip fading from day into night.
    """
    from dataclasses import fields

    return GlyphColors(
        **{
            field.name: mix(getattr(a, field.name), getattr(b, field.name), amount)
            for field in fields(GlyphColors)
        }
    )


def set_rgb(cr, color: RGB, alpha: float = 1.0) -> None:
    cr.set_source_rgba(color[0], color[1], color[2], alpha)


# ---------------------------------------------------------------------------
# Celestial bodies
# ---------------------------------------------------------------------------


def draw_sun(
    cr,
    cx: float,
    cy: float,
    radius: float,
    colors: GlyphColors = DEFAULT_COLORS,
    rays: bool = True,
    glow: float = 1.0,
) -> None:
    """A sun disc with an optional corona and radiating spokes."""
    if glow > 0:
        halo = cairo.RadialGradient(cx, cy, radius * 0.6, cx, cy, radius * 2.4)
        halo.add_color_stop_rgba(0.0, *colors.sun_bright, 0.45 * glow)
        halo.add_color_stop_rgba(0.45, *colors.sun, 0.18 * glow)
        halo.add_color_stop_rgba(1.0, *colors.sun, 0.0)
        cr.set_source(halo)
        cr.arc(cx, cy, radius * 2.4, 0, TAU)
        cr.fill()

    if rays:
        cr.save()
        set_rgb(cr, colors.sun, 0.9)
        cr.set_line_width(max(1.4, radius * 0.16))
        cr.set_line_cap(ROUND)
        for i in range(8):
            angle = i * TAU / 8
            inner = radius * 1.42
            outer = radius * 1.92
            cr.move_to(cx + math.cos(angle) * inner, cy + math.sin(angle) * inner)
            cr.line_to(cx + math.cos(angle) * outer, cy + math.sin(angle) * outer)
        cr.stroke()
        cr.restore()

    disc = cairo.RadialGradient(
        cx - radius * 0.3, cy - radius * 0.35, radius * 0.1, cx, cy, radius
    )
    disc.add_color_stop_rgb(0.0, *colors.sun_bright)
    disc.add_color_stop_rgb(1.0, *colors.sun)
    cr.set_source(disc)
    cr.arc(cx, cy, radius, 0, TAU)
    cr.fill()


def draw_moon(
    cr,
    cx: float,
    cy: float,
    radius: float,
    phase: float,
    colors: GlyphColors = DEFAULT_COLORS,
    glow: float = 1.0,
    show_dark_limb: bool = True,
    halo_scale: float = 2.6,
) -> None:
    """A moon rendered at its true phase.

    *phase* is the synodic position: 0 is new, 0.25 first quarter, 0.5 full,
    0.75 last quarter. The lit region is composed from a half-disc and a
    half-ellipse, which avoids the degenerate cases a single swept arc hits
    at the quarters.

    *halo_scale* sets how far the glow reaches, as a multiple of *radius*.
    Callers drawing into a tight box should shrink it so the widget edge does
    not clip the halo into a visible rectangle.
    """
    phase %= 1.0
    illumination = (1.0 - math.cos(phase * TAU)) / 2.0
    waxing = phase < 0.5

    if glow > 0 and illumination > 0.04:
        reach = radius * halo_scale
        halo = cairo.RadialGradient(cx, cy, radius * 0.7, cx, cy, reach)
        halo.add_color_stop_rgba(0.0, *colors.moon, 0.34 * glow * illumination)
        halo.add_color_stop_rgba(1.0, *colors.moon, 0.0)
        cr.set_source(halo)
        cr.arc(cx, cy, reach, 0, TAU)
        cr.fill()

    dark = mix(colors.moon, (0.10, 0.12, 0.20), 0.82)

    if show_dark_limb:
        set_rgb(cr, dark, 0.55)
        cr.arc(cx, cy, radius, 0, TAU)
        cr.fill()

    if illumination <= 0.01:
        return

    # The lit region is the right half-disc combined with a half-ellipse whose
    # x half-axis is the projected terminator. Past the quarters that ellipse
    # sits on the left and adds to the shape; before them it sits on the right
    # and subtracts. An even-odd fill of both sub-paths yields the union in the
    # first case and the difference in the second, so one fill covers both --
    # and it never depends on a dark limb being painted underneath.
    terminator = radius * abs(2.0 * illumination - 1.0)
    gibbous = illumination >= 0.5

    cr.save()
    cr.translate(cx, cy)
    if not waxing:
        cr.scale(-1.0, 1.0)  # mirror so the lit limb faces the other way

    cr.new_path()
    cr.new_sub_path()
    cr.arc(0, 0, radius, -math.pi / 2, math.pi / 2)
    cr.close_path()

    cr.save()
    cr.scale(max(terminator, 1e-4) / radius, 1.0)
    cr.new_sub_path()
    if gibbous:
        cr.arc_negative(0, 0, radius, -math.pi / 2, math.pi / 2)
    else:
        cr.arc(0, 0, radius, -math.pi / 2, math.pi / 2)
    cr.close_path()
    cr.restore()

    cr.set_fill_rule(cairo.FILL_RULE_EVEN_ODD)
    set_rgb(cr, colors.moon)
    cr.fill()
    cr.set_fill_rule(cairo.FILL_RULE_WINDING)
    cr.restore()

    # A few subtle maria so a full moon does not read as a flat circle.
    if illumination > 0.55:
        cr.save()
        cr.arc(cx, cy, radius, 0, TAU)
        cr.clip()
        set_rgb(cr, mix(colors.moon, (0.55, 0.58, 0.68), 0.5), 0.35 * illumination)
        for ox, oy, orad in (
            (-0.30, -0.22, 0.26),
            (0.18, -0.36, 0.16),
            (0.26, 0.22, 0.22),
            (-0.12, 0.34, 0.14),
        ):
            cr.arc(cx + ox * radius, cy + oy * radius, orad * radius, 0, TAU)
            cr.fill()
        cr.restore()


# ---------------------------------------------------------------------------
# Clouds
# ---------------------------------------------------------------------------

#: Puff layout for the standard cloud silhouette, as (x, y, radius) fractions
#: of the cloud's overall width.
_CLOUD_PUFFS = (
    (0.26, 0.62, 0.24),
    (0.46, 0.44, 0.30),
    (0.68, 0.56, 0.25),
    (0.84, 0.66, 0.17),
    (0.15, 0.70, 0.16),
)


def cloud_path(cr, x: float, y: float, width: float) -> None:
    """Append the standard cloud silhouette to the current path."""
    height = width * 0.62
    for fx, fy, fr in _CLOUD_PUFFS:
        cr.new_sub_path()
        cr.arc(x + fx * width, y + fy * height, fr * width, 0, TAU)
    # Flat base so the cloud reads as sitting on a horizon line.
    cr.new_sub_path()
    cr.rectangle(x + 0.14 * width, y + 0.60 * height, width * 0.72, height * 0.24)


def draw_cloud(
    cr,
    x: float,
    y: float,
    width: float,
    light: RGB,
    dark: RGB,
    alpha: float = 1.0,
    soft: bool = False,
) -> None:
    """Draw a cloud shaded from *light* at the top to *dark* underneath.

    With ``soft`` the puffs are drawn as radial gradients that fade at their
    edges, which suits the atmospheric clouds in the sky backdrop. Crisp
    edges suit the smaller glyphs.
    """
    height = width * 0.62

    if soft:
        # Each puff is lit from above with its own gradient, and puffs lower in
        # the silhouette start darker. Together that gives the cloud volume
        # instead of the flat grey mass a single tone produces.
        for fx, fy, fr in _CLOUD_PUFFS:
            px, py = x + fx * width, y + fy * height
            pr = fr * width * 1.18
            depth = max(0.0, min(1.0, (fy - 0.42) / 0.34))
            crown = mix(light, dark, depth * 0.42)
            belly = mix(light, dark, 0.38 + depth * 0.62)

            grad = cairo.RadialGradient(px, py - pr * 0.40, pr * 0.05, px, py, pr)
            grad.add_color_stop_rgba(0.00, *crown, 0.94 * alpha)
            grad.add_color_stop_rgba(0.45, *mix(crown, belly, 0.55), 0.78 * alpha)
            grad.add_color_stop_rgba(0.80, *belly, 0.34 * alpha)
            grad.add_color_stop_rgba(1.00, *belly, 0.0)
            cr.set_source(grad)
            cr.arc(px, py, pr, 0, TAU)
            cr.fill()
        return

    grad = cairo.LinearGradient(x, y, x, y + height)
    grad.add_color_stop_rgba(0.0, *light, alpha)
    grad.add_color_stop_rgba(1.0, *dark, alpha)
    cr.set_source(grad)
    cloud_path(cr, x, y, width)
    cr.fill()


# ---------------------------------------------------------------------------
# Precipitation and weather marks
# ---------------------------------------------------------------------------


def draw_raindrops(
    cr, x: float, y: float, width: float, color: RGB, count: int = 3, alpha: float = 1.0
) -> None:
    """Short slanted streaks beneath a cloud."""
    cr.save()
    set_rgb(cr, color, alpha)
    cr.set_line_width(max(1.6, width * 0.055))
    cr.set_line_cap(ROUND)
    for i in range(count):
        dx = x + width * (0.28 + i * 0.22)
        dy = y + width * (0.06 + (i % 2) * 0.10)
        cr.move_to(dx, dy)
        cr.line_to(dx - width * 0.07, dy + width * 0.20)
    cr.stroke()
    cr.restore()


def draw_snowflakes(
    cr, x: float, y: float, width: float, color: RGB, count: int = 3, alpha: float = 1.0
) -> None:
    """Six-armed flakes beneath a cloud."""
    cr.save()
    set_rgb(cr, color, alpha)
    cr.set_line_width(max(1.3, width * 0.04))
    cr.set_line_cap(ROUND)
    for i in range(count):
        fx = x + width * (0.30 + i * 0.21)
        fy = y + width * (0.13 + (i % 2) * 0.10)
        arm = width * 0.075
        for k in range(3):
            angle = k * math.pi / 3
            cr.move_to(fx - math.cos(angle) * arm, fy - math.sin(angle) * arm)
            cr.line_to(fx + math.cos(angle) * arm, fy + math.sin(angle) * arm)
    cr.stroke()
    cr.restore()


def bolt_path(cr, x: float, y: float, size: float) -> None:
    """A lightning bolt inscribed in a *size* box at (x, y)."""
    points = (
        (0.56, 0.0), (0.20, 0.52), (0.44, 0.52),
        (0.30, 1.0), (0.78, 0.40), (0.50, 0.40), (0.68, 0.0),
    )
    cr.move_to(x + points[0][0] * size, y + points[0][1] * size)
    for px, py in points[1:]:
        cr.line_to(x + px * size, y + py * size)
    cr.close_path()


def draw_bolt(cr, x: float, y: float, size: float, color: RGB, alpha: float = 1.0) -> None:
    glow = cairo.RadialGradient(
        x + size * 0.5, y + size * 0.5, 0, x + size * 0.5, y + size * 0.5, size
    )
    glow.add_color_stop_rgba(0.0, *color, 0.35 * alpha)
    glow.add_color_stop_rgba(1.0, *color, 0.0)
    cr.set_source(glow)
    cr.arc(x + size * 0.5, y + size * 0.5, size, 0, TAU)
    cr.fill()

    set_rgb(cr, color, alpha)
    bolt_path(cr, x, y, size)
    cr.fill()


def draw_fog_lines(
    cr, x: float, y: float, width: float, color: RGB, alpha: float = 1.0
) -> None:
    """Stacked horizontal wisps."""
    cr.save()
    set_rgb(cr, color, alpha)
    cr.set_line_width(max(1.8, width * 0.06))
    cr.set_line_cap(ROUND)
    for i, (start, end) in enumerate(((0.10, 0.86), (0.20, 0.95), (0.06, 0.78))):
        ly = y + width * (0.12 + i * 0.16)
        cr.move_to(x + start * width, ly)
        cr.line_to(x + end * width, ly)
    cr.stroke()
    cr.restore()


def draw_wind_swirl(
    cr, x: float, y: float, width: float, color: RGB, alpha: float = 1.0
) -> None:
    """Curling gust lines."""
    cr.save()
    set_rgb(cr, color, alpha)
    cr.set_line_width(max(1.8, width * 0.055))
    cr.set_line_cap(ROUND)

    cr.move_to(x + 0.08 * width, y + 0.34 * width)
    cr.line_to(x + 0.60 * width, y + 0.34 * width)
    cr.curve_to(
        x + 0.80 * width, y + 0.34 * width,
        x + 0.80 * width, y + 0.06 * width,
        x + 0.60 * width, y + 0.10 * width,
    )
    cr.stroke()

    cr.move_to(x + 0.08 * width, y + 0.58 * width)
    cr.line_to(x + 0.72 * width, y + 0.58 * width)
    cr.curve_to(
        x + 0.94 * width, y + 0.58 * width,
        x + 0.94 * width, y + 0.86 * width,
        x + 0.70 * width, y + 0.82 * width,
    )
    cr.stroke()

    cr.move_to(x + 0.14 * width, y + 0.80 * width)
    cr.line_to(x + 0.48 * width, y + 0.80 * width)
    cr.stroke()
    cr.restore()


# ---------------------------------------------------------------------------
# Composite glyphs
# ---------------------------------------------------------------------------


def draw_glyph(
    cr,
    condition: Condition,
    x: float,
    y: float,
    size: float,
    is_day: bool = True,
    moon_phase: float = 0.5,
    colors: GlyphColors = DEFAULT_COLORS,
    alpha: float = 1.0,
) -> None:
    """Draw the composite symbol for *condition* in a *size* box at (x, y)."""
    cr.save()
    cr.translate(x, y)

    body_radius = size * 0.17
    light, dark = colors.cloud_light, colors.cloud_dark

    def celestial(cx: float, cy: float, radius: float, rays: bool = True) -> None:
        if is_day:
            draw_sun(cr, cx, cy, radius, colors, rays=rays, glow=0.7 * alpha)
        else:
            draw_moon(
                cr, cx, cy, radius, moon_phase, colors,
                glow=0.7 * alpha, show_dark_limb=False,
            )

    if condition in (Condition.CLEAR, Condition.HOT, Condition.COLD):
        radius = size * 0.24
        if is_day:
            draw_sun(cr, size * 0.5, size * 0.5, radius, colors, glow=alpha)
        else:
            draw_moon(
                cr, size * 0.5, size * 0.5, radius * 1.05, moon_phase, colors,
                glow=alpha, show_dark_limb=False,
            )
        if condition is Condition.HOT:
            set_rgb(cr, colors.sun, 0.85 * alpha)
            cr.set_line_width(size * 0.05)
            cr.set_line_cap(ROUND)
            for i in range(2):
                ly = size * (0.86 + i * 0.09)
                cr.move_to(size * 0.24, ly)
                cr.line_to(size * 0.76, ly)
            cr.stroke()

    elif condition in (Condition.FEW_CLOUDS, Condition.PARTLY_CLOUDY):
        celestial(size * 0.36, size * 0.34, body_radius)
        width = size * 0.62 if condition is Condition.FEW_CLOUDS else size * 0.72
        draw_cloud(cr, size * 0.24, size * 0.40, width, light, dark, alpha)

    elif condition is Condition.MOSTLY_CLOUDY:
        celestial(size * 0.68, size * 0.28, body_radius * 0.85, rays=False)
        draw_cloud(cr, size * 0.08, size * 0.30, size * 0.82, light, dark, alpha)

    elif condition is Condition.OVERCAST:
        # A distinct back layer, pushed up and darkened, so the glyph reads as
        # a covered sky rather than one lone cloud.
        draw_cloud(
            cr, size * 0.24, size * 0.06, size * 0.66,
            mix(light, dark, 0.55), mix(dark, (0.42, 0.46, 0.53), 0.5), 0.85 * alpha,
        )
        draw_cloud(cr, size * 0.02, size * 0.34, size * 0.88, light, dark, alpha)

    elif condition in (Condition.RAIN, Condition.SHOWERS):
        if condition is Condition.SHOWERS:
            celestial(size * 0.70, size * 0.22, body_radius * 0.8, rays=False)
        draw_cloud(cr, size * 0.08, size * 0.16, size * 0.80, light, dark, alpha)
        draw_raindrops(
            cr, size * 0.06, size * 0.66, size * 0.86, colors.rain,
            count=3 if condition is Condition.SHOWERS else 4, alpha=alpha,
        )

    elif condition is Condition.THUNDERSTORM:
        draw_cloud(
            cr, size * 0.06, size * 0.12, size * 0.84,
            mix(light, dark, 0.30), mix(dark, (0.35, 0.38, 0.45), 0.45), alpha,
        )
        draw_bolt(cr, size * 0.36, size * 0.56, size * 0.34, colors.bolt, alpha)
        draw_raindrops(cr, size * 0.0, size * 0.66, size * 0.60, colors.rain, 2, alpha)

    elif condition in (Condition.SNOW, Condition.BLIZZARD):
        draw_cloud(cr, size * 0.08, size * 0.14, size * 0.80, light, dark, alpha)
        draw_snowflakes(cr, size * 0.06, size * 0.64, size * 0.86, colors.snow, 3, alpha)

    elif condition in (Condition.SLEET, Condition.FREEZING_RAIN):
        draw_cloud(cr, size * 0.08, size * 0.14, size * 0.80, light, dark, alpha)
        draw_raindrops(cr, size * 0.02, size * 0.64, size * 0.62, colors.rain, 2, alpha)
        draw_snowflakes(cr, size * 0.42, size * 0.64, size * 0.56, colors.snow, 2, alpha)

    elif condition in (Condition.FOG, Condition.HAZE, Condition.SMOKE, Condition.DUST):
        tint = colors.fog
        if condition is Condition.SMOKE:
            tint = mix(colors.fog, (0.45, 0.42, 0.38), 0.5)
        elif condition is Condition.DUST:
            tint = mix(colors.fog, (0.78, 0.62, 0.36), 0.55)
        if condition is not Condition.FOG:
            celestial(size * 0.62, size * 0.28, body_radius * 0.8, rays=False)
        draw_cloud(cr, size * 0.10, size * 0.10, size * 0.72, light, dark, 0.55 * alpha)
        draw_fog_lines(cr, size * 0.04, size * 0.58, size * 0.92, tint, alpha)

    elif condition is Condition.WIND:
        celestial(size * 0.30, size * 0.30, body_radius * 0.9)
        draw_wind_swirl(cr, size * 0.10, size * 0.34, size * 0.82, colors.wind, alpha)

    elif condition in (Condition.TORNADO, Condition.HURRICANE):
        if condition is Condition.TORNADO:
            draw_cloud(
                cr, size * 0.04, size * 0.10, size * 0.90,
                mix(light, dark, 0.45), mix(dark, (0.28, 0.30, 0.36), 0.55), alpha,
            )
            # A tapered funnel: two mirrored curves from a wide mouth at the
            # cloud base down to a narrow, offset touchdown point.
            funnel = cairo.LinearGradient(0, size * 0.48, 0, size * 1.0)
            funnel.add_color_stop_rgba(0.0, *mix(dark, (0.36, 0.39, 0.46), 0.35), alpha)
            funnel.add_color_stop_rgba(1.0, *mix(dark, (0.20, 0.22, 0.28), 0.6), alpha)
            cr.set_source(funnel)
            cr.move_to(size * 0.28, size * 0.48)
            cr.curve_to(
                size * 0.34, size * 0.72, size * 0.40, size * 0.86, size * 0.46, size * 0.98
            )
            cr.line_to(size * 0.58, size * 0.98)
            cr.curve_to(
                size * 0.62, size * 0.84, size * 0.66, size * 0.68, size * 0.74, size * 0.48
            )
            cr.close_path()
            cr.fill()

            # Debris bands crossing the funnel give it motion.
            set_rgb(cr, mix(light, dark, 0.2), 0.45 * alpha)
            cr.set_line_width(size * 0.028)
            cr.set_line_cap(ROUND)
            for fy, half in ((0.60, 0.20), (0.74, 0.145), (0.88, 0.09)):
                cr.move_to(size * (0.51 - half), size * fy)
                cr.curve_to(
                    size * (0.51 - half * 0.3), size * (fy - 0.03),
                    size * (0.51 + half * 0.3), size * (fy + 0.03),
                    size * (0.51 + half), size * fy,
                )
                cr.stroke()
        else:
            # Hurricane reads best as the satellite view: a clear eye with two
            # spiral bands, drawn on its own rather than tucked under a cloud.
            cx, cy = size * 0.50, size * 0.50
            cr.set_line_cap(ROUND)
            for turn in range(2):
                start = turn * math.pi
                cr.new_path()
                steps = 36
                for step in range(steps + 1):
                    t = step / steps
                    theta = start + t * 2.9
                    radius = size * (0.085 + 0.315 * t)
                    px = cx + math.cos(theta) * radius
                    py = cy + math.sin(theta) * radius
                    if step == 0:
                        cr.move_to(px, py)
                    else:
                        cr.line_to(px, py)
                # Bands taper from thick at the eye wall to thin at the tip.
                band = cairo.LinearGradient(
                    cx, cy, cx + size * 0.4, cy + size * 0.4
                )
                band.add_color_stop_rgba(0.0, *light, 0.95 * alpha)
                band.add_color_stop_rgba(1.0, *dark, 0.45 * alpha)
                cr.set_source(band)
                cr.set_line_width(size * 0.13)
                cr.stroke()

            # The gap the two bands leave at the centre already reads as the
            # eye, so nothing is drawn over it.

    else:  # UNKNOWN
        draw_cloud(cr, size * 0.10, size * 0.24, size * 0.78, light, dark, alpha)

    cr.restore()


def glyph_for_sky(palette) -> GlyphColors:
    """Glyph tones tuned to sit legibly on a given sky palette."""
    return GlyphColors(
        cloud_light=palette.cloud_light,
        cloud_dark=palette.cloud_dark,
        rain=palette.precip,
        snow=palette.precip,
        fog=mix(palette.cloud_light, (1.0, 1.0, 1.0), 0.3),
        wind=mix(palette.cloud_light, (1.0, 1.0, 1.0), 0.4),
    )


# ---------------------------------------------------------------------------
# Misc primitives used by charts and cards
# ---------------------------------------------------------------------------


def rounded_rect(cr, x: float, y: float, width: float, height: float, radius: float) -> None:
    """Append a rounded rectangle to the current path."""
    radius = min(radius, width / 2, height / 2)
    cr.new_sub_path()
    cr.arc(x + width - radius, y + radius, radius, -math.pi / 2, 0)
    cr.arc(x + width - radius, y + height - radius, radius, 0, math.pi / 2)
    cr.arc(x + radius, y + height - radius, radius, math.pi / 2, math.pi)
    cr.arc(x + radius, y + radius, radius, math.pi, 3 * math.pi / 2)
    cr.close_path()


def smooth_path(cr, points: list[tuple[float, float]], tension: float = 0.35) -> None:
    """Append a Catmull-Rom style smoothed curve through *points*."""
    if len(points) < 2:
        return
    cr.move_to(*points[0])
    if len(points) == 2:
        cr.line_to(*points[1])
        return

    for i in range(len(points) - 1):
        p0 = points[i - 1] if i > 0 else points[i]
        p1 = points[i]
        p2 = points[i + 1]
        p3 = points[i + 2] if i + 2 < len(points) else p2

        c1x = p1[0] + (p2[0] - p0[0]) * tension / 3.0
        c1y = p1[1] + (p2[1] - p0[1]) * tension / 3.0
        c2x = p2[0] - (p3[0] - p1[0]) * tension / 3.0
        c2y = p2[1] - (p3[1] - p1[1]) * tension / 3.0
        cr.curve_to(c1x, c1y, c2x, c2y, p2[0], p2[1])
