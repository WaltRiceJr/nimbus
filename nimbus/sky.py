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

"""The animated sky backdrop.

:class:`SkyView` paints a live scene for one location: a gradient keyed to
the sun's real altitude, stars that appear as twilight deepens, drifting
cloud layers, falling precipitation, and lightning during storms. The sun and
moon are placed along their actual arcs, and the moon shows its true phase.

Rendering is split into a cached static layer (gradient, stars, celestial
body) and a live layer (clouds, particles) redrawn each frame, which keeps
the cost low enough to run several of these at once on the dashboard.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from datetime import datetime

import cairo
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Graphene", "1.0")
from gi.repository import GLib, Graphene, Gtk  # noqa: E402

from . import astro, drawing
from .conditions import SkyPalette, mix, palette as build_palette
from .model import Cloudiness, Condition

TAU = math.pi * 2

#: Frame interval in milliseconds. 20 fps is smooth for drifting clouds and
#: falling rain while leaving plenty of headroom on the dashboard grid.
FRAME_MS = 50


@dataclass
class _Particle:
    x: float
    y: float
    speed: float
    size: float
    drift: float
    phase: float


class SkyView(Gtk.Widget):
    """A weather scene sized to fill whatever box it is given."""

    __gtype_name__ = "NimbusSkyView"

    def __init__(
        self,
        condition: Condition = Condition.CLEAR,
        compact: bool = False,
        animate: bool = True,
    ) -> None:
        super().__init__()
        self._condition = condition
        self._compact = compact
        self._animate = animate

        self._sun_altitude = 30.0
        self._wind = 8.0

        self._palette: SkyPalette = build_palette(condition, self._sun_altitude)
        self._time = 0.0
        self._tick_id = 0
        self._flash = 0.0
        self._next_flash = 4.0

        self._static: cairo.ImageSurface | None = None
        self._static_key: tuple = ()

        self._rng = random.Random(0xC10D)
        self._seeded_factor = 1.0
        self._stars: list[tuple[float, float, float, float]] = []
        self._clouds: list[_Particle] = []
        self._drops: list[_Particle] = []

        self.set_overflow(Gtk.Overflow.HIDDEN)
        self.connect("map", self._on_map)
        self.connect("unmap", self._on_unmap)

    # -- configuration ----------------------------------------------------

    def set_scene(
        self,
        condition: Condition,
        moment: datetime,
        latitude: float,
        longitude: float,
        tz=None,
        wind_mph: float | None = None,
    ) -> None:
        """Point the scene at a real place and time."""
        # Only the sun's altitude is needed: it drives the gradient and the
        # star opacity. The scene no longer draws the sun or moon itself, so
        # the far more expensive lunar rise/set search is not performed here.
        sun = astro.sun_times(moment, latitude, longitude, tz)

        self._condition = condition
        self._sun_altitude = sun.altitude_now
        self._wind = max(2.0, min(40.0, wind_mph or 8.0))

        self._palette = build_palette(condition, self._sun_altitude)
        self._static = None
        self._seed_particles()
        self._sync_animation()
        self.queue_draw()

    def set_condition(self, condition: Condition, sun_altitude: float | None = None) -> None:
        """Set the scene without a full astronomical fix, for previews."""
        self._condition = condition
        if sun_altitude is not None:
            self._sun_altitude = sun_altitude
        self._palette = build_palette(condition, self._sun_altitude)
        self._static = None
        self._seed_particles()
        self._sync_animation()
        self.queue_draw()

    @property
    def palette(self) -> SkyPalette:
        return self._palette

    # -- animation lifecycle ----------------------------------------------

    def _on_map(self, *_args) -> None:
        self._sync_animation()

    def _on_unmap(self, *_args) -> None:
        self._stop_animation()

    def _sync_animation(self) -> None:
        needs_motion = self._animate and (
            self._condition.is_precipitating
            or self._cloudiness() > Cloudiness.NONE
            or self._palette.star_opacity > 0.2
        )
        if needs_motion and self.get_mapped():
            self._start_animation()
        else:
            self._stop_animation()

    def _start_animation(self) -> None:
        if self._tick_id:
            return
        self._tick_id = GLib.timeout_add(FRAME_MS, self._on_tick)

    def _stop_animation(self) -> None:
        if self._tick_id:
            GLib.source_remove(self._tick_id)
            self._tick_id = 0

    def _on_tick(self) -> bool:
        self._time += FRAME_MS / 1000.0
        self._advance()
        self.queue_draw()
        return GLib.SOURCE_CONTINUE

    def do_unroot(self) -> None:  # type: ignore[override]
        self._stop_animation()
        Gtk.Widget.do_unroot(self)

    # -- scene contents ---------------------------------------------------

    def _cloudiness(self) -> Cloudiness:
        from .conditions import CLOUDINESS

        return CLOUDINESS.get(self._condition, Cloudiness.LIGHT)

    #: Aspect ratio a cloud layout is tuned for; wider widgets scale up the
    #: cloud count rather than stretching each cloud.
    REFERENCE_ASPECT = 1.75

    def _width_factor(self, width: int = 0, height: int = 0) -> float:
        width = width or self.get_width()
        height = height or self.get_height()
        if width <= 0 or height <= 0:
            return 1.0
        return max(1.0, min(4.0, (width / height) / self.REFERENCE_ASPECT))

    def do_size_allocate(self, width: int, height: int, baseline: int) -> None:  # type: ignore[override]
        Gtk.Widget.do_size_allocate(self, width, height, baseline)
        # The cloud count depends on the aspect ratio, which is not known when
        # the scene is first set, so re-seed once the real allocation lands.
        factor = self._width_factor(width, height)
        if abs(factor - self._seeded_factor) > 0.2:
            self._seed_particles(factor)

    def _seed_particles(self, factor: float | None = None) -> None:
        """Rebuild the particle sets for the current condition.

        Positions are normalised 0..1 so they survive widget resizes.
        """
        rng = random.Random(0xC10D ^ hash(self._condition.value) & 0xFFFF)

        star_count = 0 if self._palette.star_opacity <= 0.01 else (40 if self._compact else 120)
        self._stars = [
            (
                rng.random(),
                rng.random() ** 1.6 * 0.75,  # cluster toward the top
                rng.uniform(0.4, 1.0),
                rng.uniform(0.0, TAU),
            )
            for _ in range(star_count)
        ]

        cloudiness = self._cloudiness()
        cloud_counts = {
            Cloudiness.NONE: 0,
            Cloudiness.LIGHT: 2,
            Cloudiness.MEDIUM: 3,
            Cloudiness.HEAVY: 5,
            # A deck already covers the sky at this level, so the puffs only
            # need to break up its edge.
            Cloudiness.TOTAL: 4,
        }
        count = cloud_counts[cloudiness]
        if self._condition is Condition.FOG:
            count = 1  # fog is carried by the haze bands, not by cloud shapes
        if self._compact:
            count = max(0, count - 1)

        # Clouds are sized against the widget's height (see _paint_live), so a
        # wide hero needs proportionally more of them to keep the same
        # apparent coverage as a small card.
        if factor is None:
            factor = self._width_factor()
        self._seeded_factor = factor
        count = int(round(count * factor))

        # Clouds spread across the frame with a bias toward the upper half and
        # stay smaller than the tile, so they read as distinct shapes rather
        # than one merged bank. Where a deck is painted they ride its lower
        # edge instead, which avoids stamping visible rings across it.
        if cloudiness >= Cloudiness.TOTAL:
            y_low, y_high = 0.26, 0.50
        else:
            y_low, y_high = -0.10, 0.52

        self._clouds = [
            _Particle(
                x=rng.random() * 1.3 - 0.15,
                y=y_low + (rng.random() ** 1.3) * (y_high - y_low),
                speed=rng.uniform(0.004, 0.016),
                size=rng.uniform(0.38, 0.85),
                drift=rng.uniform(0.6, 1.4),
                phase=rng.uniform(0, TAU),
            )
            for _ in range(count)
        ]
        # Nearer clouds sit lower and drift faster, which reads as depth.
        for cloud in self._clouds:
            cloud.speed *= 0.7 + cloud.y * 1.6

        self._drops = []
        if self._condition.is_precipitating:
            heavy = self._condition in (
                Condition.RAIN,
                Condition.THUNDERSTORM,
                Condition.BLIZZARD,
                Condition.HURRICANE,
            )
            base = 90 if heavy else 55
            if self._compact:
                base //= 2
            frozen = self._condition.is_frozen
            for _ in range(base):
                self._drops.append(
                    _Particle(
                        x=rng.random(),
                        y=rng.random(),
                        speed=rng.uniform(0.9, 1.5) * (0.22 if frozen else 1.0),
                        size=rng.uniform(0.5, 1.0),
                        drift=rng.uniform(-0.4, 0.4),
                        phase=rng.uniform(0, TAU),
                    )
                )

    def _advance(self) -> None:
        """Step the particle simulation by one frame."""
        dt = FRAME_MS / 1000.0
        wind_factor = self._wind / 10.0

        for cloud in self._clouds:
            cloud.x += cloud.speed * dt * cloud.drift * wind_factor
            if cloud.x > 1.35:
                cloud.x = -0.35

        frozen = self._condition.is_frozen
        for drop in self._drops:
            drop.y += drop.speed * dt * (0.55 if frozen else 1.0)
            drop.x += drop.drift * dt * 0.04 * wind_factor
            if frozen:
                drop.x += math.sin(self._time * 1.2 + drop.phase) * dt * 0.02
            if drop.y > 1.05:
                drop.y -= 1.15
                drop.x = self._rng.random()
            if drop.x > 1.05:
                drop.x -= 1.1
            elif drop.x < -0.05:
                drop.x += 1.1

        if self._condition is Condition.THUNDERSTORM:
            self._flash = max(0.0, self._flash - dt * 3.2)
            self._next_flash -= dt
            if self._next_flash <= 0:
                self._flash = 1.0
                self._next_flash = self._rng.uniform(3.5, 9.0)

    # -- painting ---------------------------------------------------------

    def do_snapshot(self, snapshot) -> None:  # type: ignore[override]
        width = self.get_width()
        height = self.get_height()
        if width <= 0 or height <= 0:
            return

        rect = Graphene.Rect().init(0, 0, width, height)
        cr = snapshot.append_cairo(rect)

        self._paint_static(cr, width, height)
        self._paint_live(cr, width, height)

    def _paint_static(self, cr, width: int, height: int) -> None:
        """Blit the cached gradient and star layer, rebuilding if stale."""
        key = (width, height, self._condition, round(self._sun_altitude, 1))
        if self._static is None or self._static_key != key:
            surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, width, height)
            layer = cairo.Context(surface)
            self._draw_gradient(layer, width, height)
            self._draw_stars(layer, width, height)
            self._static = surface
            self._static_key = key

        cr.set_source_surface(self._static, 0, 0)
        cr.paint()

    def _draw_gradient(self, cr, width: int, height: int) -> None:
        pal = self._palette
        grad = cairo.LinearGradient(0, 0, 0, height)
        grad.add_color_stop_rgb(0.0, *pal.top)
        grad.add_color_stop_rgb(0.55, *pal.middle)
        grad.add_color_stop_rgb(1.0, *pal.bottom)
        cr.set_source(grad)
        cr.rectangle(0, 0, width, height)
        cr.fill()

        # A warm band hugging the horizon during golden hour, which is what
        # sells a sunrise or sunset more than the overall hue does.
        if -8.0 < self._sun_altitude < 12.0:
            warmth = 1.0 - abs(self._sun_altitude - 2.0) / 10.0
            warmth = max(0.0, min(1.0, warmth))
            band = cairo.LinearGradient(0, height * 0.45, 0, height)
            band.add_color_stop_rgba(0.0, *pal.glow, 0.0)
            band.add_color_stop_rgba(1.0, *pal.glow, 0.34 * warmth)
            cr.set_source(band)
            cr.rectangle(0, height * 0.45, width, height * 0.55)
            cr.fill()

    def _draw_stars(self, cr, width: int, height: int) -> None:
        opacity = self._palette.star_opacity
        if opacity <= 0.01:
            return
        for sx, sy, brightness, phase in self._stars:
            # Twinkle is baked into the static layer using a fixed phase; the
            # motion of the clouds above supplies the visible life.
            alpha = opacity * brightness * (0.65 + 0.35 * math.sin(phase))
            radius = (0.7 + brightness * 0.9) * (1.0 if self._compact else 1.3)
            cr.set_source_rgba(1.0, 1.0, 1.0, alpha)
            cr.arc(sx * width, sy * height, radius, 0, TAU)
            cr.fill()

    def _paint_cloud_deck(self, cr, width: int, height: int) -> None:
        """A continuous low deck for fully overcast skies.

        Scattered puffs alone never read as "covered"; a deck with a slowly
        undulating lower edge does, and the puffs on top then break up its
        outline so it does not look like a flat band.
        """
        pal = self._palette
        base = height * 0.50
        amp = height * 0.07

        grad = cairo.LinearGradient(0, -height * 0.15, 0, base + amp)
        grad.add_color_stop_rgba(0.0, *pal.cloud_light, 0.92)
        grad.add_color_stop_rgba(0.55, *mix(pal.cloud_light, pal.cloud_dark, 0.55), 0.86)
        grad.add_color_stop_rgba(1.0, *pal.cloud_dark, 0.62)
        cr.set_source(grad)

        cr.move_to(-2, -2)
        cr.line_to(width + 2, -2)
        steps = 48
        for i in range(steps + 1):
            t = 1.0 - i / steps
            px = t * (width + 4) - 2
            py = (
                base
                + math.sin(t * 5.5 + self._time * 0.10) * amp
                + math.sin(t * 12.0 + self._time * 0.065) * amp * 0.38
            )
            cr.line_to(px, py)
        cr.close_path()
        cr.fill()

        # Feather the underside so the deck dissolves instead of ending on a
        # hard line.
        fade = cairo.LinearGradient(0, base - amp, 0, base + amp * 2.6)
        fade.add_color_stop_rgba(0.0, *pal.cloud_dark, 0.30)
        fade.add_color_stop_rgba(1.0, *pal.cloud_dark, 0.0)
        cr.set_source(fade)
        cr.rectangle(0, base - amp, width, amp * 3.6)
        cr.fill()

    def _paint_live(self, cr, width: int, height: int) -> None:
        pal = self._palette

        cloudiness = self._cloudiness()
        has_deck = cloudiness >= Cloudiness.TOTAL
        if has_deck:
            self._paint_cloud_deck(cr, width, height)

        if has_deck:
            cloud_alpha = 0.45
        elif cloudiness <= Cloudiness.MEDIUM:
            cloud_alpha = 0.55
        else:
            cloud_alpha = 0.78

        for cloud in self._clouds:
            cloud_width = height * cloud.size
            bob = math.sin(self._time * 0.35 + cloud.phase) * height * 0.012
            drawing.draw_cloud(
                cr,
                cloud.x * width - cloud_width * 0.5,
                cloud.y * height + bob,
                cloud_width,
                pal.cloud_light,
                pal.cloud_dark,
                alpha=cloud_alpha,
                soft=True,
            )

        if self._drops:
            self._paint_precipitation(cr, width, height)

        if self._flash > 0.01:
            cr.set_source_rgba(1.0, 0.98, 0.88, 0.30 * self._flash)
            cr.rectangle(0, 0, width, height)
            cr.fill()

        # Fog rolls in as horizontal bands rather than particles.
        if self._condition in (Condition.FOG, Condition.HAZE, Condition.SMOKE):
            self._paint_fog(cr, width, height)

        # A vignette settles the scene and keeps overlaid text legible.
        edge = cairo.LinearGradient(0, 0, 0, height)
        edge.add_color_stop_rgba(0.0, 0, 0, 0, 0.20)
        edge.add_color_stop_rgba(0.35, 0, 0, 0, 0.02)
        edge.add_color_stop_rgba(1.0, 0, 0, 0, 0.16)
        cr.set_source(edge)
        cr.rectangle(0, 0, width, height)
        cr.fill()

    def _paint_precipitation(self, cr, width: int, height: int) -> None:
        pal = self._palette
        frozen = self._condition.is_frozen
        slant = (self._wind / 40.0) * 0.35

        if frozen:
            for drop in self._drops:
                radius = drop.size * (1.6 if self._compact else 2.3)
                cr.set_source_rgba(*pal.precip, 0.55 + 0.35 * drop.size)
                cr.arc(drop.x * width, drop.y * height, radius, 0, TAU)
                cr.fill()
        else:
            cr.save()
            cr.set_line_cap(cairo.LINE_CAP_ROUND)
            for drop in self._drops:
                length = height * (0.035 + 0.045 * drop.size)
                x = drop.x * width
                y = drop.y * height
                cr.set_source_rgba(*pal.precip, 0.30 + 0.35 * drop.size)
                cr.set_line_width(0.9 + drop.size * 0.9)
                cr.move_to(x, y)
                cr.line_to(x - length * slant, y + length)
                cr.stroke()
            cr.restore()

    def _paint_fog(self, cr, width: int, height: int) -> None:
        pal = self._palette
        for i in range(4):
            offset = math.sin(self._time * 0.12 + i * 1.7) * width * 0.06
            band_y = height * (0.30 + i * 0.17)
            band_h = height * 0.20
            grad = cairo.LinearGradient(0, band_y, 0, band_y + band_h)
            grad.add_color_stop_rgba(0.0, *pal.cloud_light, 0.0)
            grad.add_color_stop_rgba(0.5, *pal.cloud_light, 0.22)
            grad.add_color_stop_rgba(1.0, *pal.cloud_light, 0.0)
            cr.set_source(grad)
            cr.rectangle(offset - width * 0.1, band_y, width * 1.2, band_h)
            cr.fill()


class GlyphIcon(Gtk.Widget):
    """A small standalone weather symbol, for lists and cards."""

    __gtype_name__ = "NimbusGlyphIcon"

    def __init__(
        self,
        condition: Condition = Condition.CLEAR,
        size: int = 32,
        is_day: bool = True,
        moon_phase: float = 0.5,
        on_sky: bool = False,
    ) -> None:
        super().__init__()
        self._condition = condition
        self._size = size
        self._is_day = is_day
        self._moon_phase = moon_phase
        self._colors = drawing.ON_SKY_COLORS if on_sky else drawing.DEFAULT_COLORS
        self.set_size_request(size, size)
        self.set_halign(Gtk.Align.CENTER)
        self.set_valign(Gtk.Align.CENTER)

    def set_weather(
        self, condition: Condition, is_day: bool = True, moon_phase: float | None = None
    ) -> None:
        self._condition = condition
        self._is_day = is_day
        if moon_phase is not None:
            self._moon_phase = moon_phase
        self.queue_draw()

    def do_snapshot(self, snapshot) -> None:  # type: ignore[override]
        width, height = self.get_width(), self.get_height()
        if width <= 0 or height <= 0:
            return
        rect = Graphene.Rect().init(0, 0, width, height)
        cr = snapshot.append_cairo(rect)
        size = min(width, height)
        drawing.draw_glyph(
            cr, self._condition,
            (width - size) / 2, (height - size) / 2, size,
            is_day=self._is_day, moon_phase=self._moon_phase, colors=self._colors,
        )
