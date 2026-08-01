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

"""Custom-drawn chart widgets: the hourly strip, day range bars and moon dial.

These are single canvases rather than compositions of labels and boxes, which
lets the temperature curve flow continuously behind the hour columns and
keeps the type baselines aligned exactly.
"""

from __future__ import annotations

import math
from datetime import datetime, tzinfo

import cairo
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Graphene", "1.0")
gi.require_version("Pango", "1.0")
gi.require_version("PangoCairo", "1.0")
from gi.repository import Adw, Graphene, Gtk, Pango, PangoCairo  # noqa: E402

from .. import astro, drawing
from ..conditions import RGB, luminance, mix
from ..model import HourEntry

TAU = math.pi * 2

#: Width of one hour column in the hourly strip.
HOUR_COLUMN = 74
#: Full height, including the precipitation band, and the height used when no
#: hour in the window has a meaningful chance of precipitation.
#: Clear band kept at the foot of the strip for the overlay scrollbar, which
#: is drawn on top of the content rather than beside it. Adwaita's horizontal
#: scrollbar measures 24px, so this is that plus breathing room.
SCROLLBAR_BAND = 34
#: Band across the top holding the section title and the sticky date, drawn
#: inside the canvas so it sits on the day/night background rather than on
#: the card behind it.
HEADER_BAND = 38
HOURLY_HEIGHT = 208 + HEADER_BAND + SCROLLBAR_BAND
HOURLY_HEIGHT_DRY = 164 + HEADER_BAND + SCROLLBAR_BAND
#: A column needs at least this chance of precipitation to earn a bar.
PRECIP_FLOOR = 5.0


def _theme_colors(_widget: Gtk.Widget) -> tuple[RGB, RGB, RGB]:
    """Foreground, dimmed foreground and accent for the active theme.

    Read from libadwaita rather than GtkStyleContext, which is deprecated and
    reports the wrong colour for widgets that have not been realised yet.
    """
    manager = Adw.StyleManager.get_default()
    dark = manager.get_dark()

    fg: RGB = (0.94, 0.95, 0.96) if dark else (0.11, 0.12, 0.14)
    dim: RGB = (0.66, 0.69, 0.74) if dark else (0.42, 0.45, 0.50)

    accent: RGB = (0.21, 0.52, 0.89)
    try:
        rgba = manager.get_accent_color_rgba()
        accent = (rgba.red, rgba.green, rgba.blue)
    except (AttributeError, TypeError):
        pass  # libadwaita older than 1.6 has no accent colour API
    return fg, dim, accent


#: Solar altitudes bounding the day-to-night fade, in degrees. The midpoint
#: sits near the horizon so the change reads as sunrise and sunset, and the
#: span is wide enough to cross smoothly rather than snap.
_DAY_ALTITUDE = 3.0
_NIGHT_ALTITUDE = -8.0

#: (daytime, night-time) strip backgrounds for the light and dark themes.
_STRIP_BG_LIGHT: tuple[RGB, RGB] = (
    (0.918, 0.949, 0.984),
    (0.129, 0.157, 0.235),
)
_STRIP_BG_DARK: tuple[RGB, RGB] = (
    (0.259, 0.310, 0.404),
    (0.071, 0.086, 0.137),
)


def _nightness(altitude: float) -> float:
    """How dark the sky is at a given solar altitude, 0 (day) to 1 (night)."""
    if altitude >= _DAY_ALTITUDE:
        return 0.0
    if altitude <= _NIGHT_ALTITUDE:
        return 1.0
    t = (_DAY_ALTITUDE - altitude) / (_DAY_ALTITUDE - _NIGHT_ALTITUDE)
    return t * t * (3.0 - 2.0 * t)  # smoothstep, so the ends ease in


#: Text tones for the strip. Kept near pure white and pure black on purpose:
#: at the crossover where the background suits neither, contrast bottoms out
#: at about 4.58:1, and only these extremes clear the 4.5:1 mark for the
#: small labels. Softer tones dip below it mid-twilight.
_STRIP_FG_LIGHT: RGB = (1.0, 1.0, 1.0)
_STRIP_FG_DARK: RGB = (0.02, 0.03, 0.05)


def _contrast(a: RGB, b: RGB) -> float:
    """WCAG contrast ratio between two colours."""
    high, low = sorted((luminance(a), luminance(b)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def _readable_on(background: RGB) -> RGB:
    """Pick whichever foreground contrasts better against *background*.

    Measuring both rather than thresholding on luminance matters most in the
    middle of a twilight, where the background is mid-toned: the crossover
    where light and dark text are equally readable sits near a luminance of
    0.18, not at the midpoint one would guess.
    """
    if _contrast(_STRIP_FG_LIGHT, background) >= _contrast(_STRIP_FG_DARK, background):
        return _STRIP_FG_LIGHT
    return _STRIP_FG_DARK


def _day_label(day, today) -> str:
    """A short, human date for one section of the hourly strip."""
    delta = (day - today).days
    if delta == 0:
        return f"Today · {day.strftime('%a %b %-d')}"
    if delta == 1:
        return f"Tomorrow · {day.strftime('%a %b %-d')}"
    return day.strftime("%A · %b %-d")


def _layout(widget: Gtk.Widget, text: str, size: float, bold: bool = False):
    layout = widget.create_pango_layout(text)
    desc = layout.get_context().get_font_description()
    if desc is None:
        desc = Pango.FontDescription()
    desc = desc.copy()
    desc.set_absolute_size(size * Pango.SCALE)
    if bold:
        desc.set_weight(Pango.Weight.BOLD)
    layout.set_font_description(desc)
    return layout


def _draw_text(
    cr, widget: Gtk.Widget, text: str, x: float, y: float, size: float,
    color: RGB, alpha: float = 1.0, bold: bool = False, center: bool = True,
) -> None:
    layout = _layout(widget, text, size, bold)
    width, height = layout.get_pixel_size()
    cr.save()
    cr.set_source_rgba(*color, alpha)
    cr.move_to(x - (width / 2 if center else 0), y - height / 2)
    PangoCairo.show_layout(cr, layout)
    cr.restore()


def add_drag_to_pan(scroller: Gtk.ScrolledWindow) -> None:
    """Let the user drag the contents of *scroller* horizontally.

    The gesture belongs on the scrolled window, never on the child being
    scrolled. A gesture on the child measures its offset in the child's own
    coordinates -- and the child slides as it scrolls, so panning moves the
    very space the offset is measured against. That feeds back on itself and
    the drag oscillates instead of tracking the pointer. The scrolled
    window's allocation stays put, so offsets taken there are stable.
    """
    origin = 0.0

    def begin(_gesture, _start_x, _start_y) -> None:
        nonlocal origin
        origin = scroller.get_hadjustment().get_value()
        scroller.set_cursor_from_name("grabbing")

    def update(_gesture, offset_x, _offset_y) -> None:
        adjustment = scroller.get_hadjustment()
        upper = max(0.0, adjustment.get_upper() - adjustment.get_page_size())
        adjustment.set_value(max(0.0, min(upper, origin - offset_x)))

    def end(_gesture, _offset_x, _offset_y) -> None:
        scroller.set_cursor_from_name("grab")

    drag = Gtk.GestureDrag()
    drag.connect("drag-begin", begin)
    drag.connect("drag-update", update)
    drag.connect("drag-end", end)
    scroller.add_controller(drag)
    scroller.set_cursor_from_name("grab")


class HourlyStrip(Gtk.Widget):
    """A scrolling multi-day hourly forecast drawn as one continuous chart.

    Each column carries the hour, a weather glyph, and its temperature, with
    a smoothed curve threading through the temperature points and a
    precipitation-probability band along the bottom.

    The background tracks the real position of the sun, fading from a pale
    daytime tone into a dark night one across each twilight. Every element
    that sits on top -- text, glyphs, the curve -- takes its colour from the
    background directly beneath it, so nothing loses contrast as the strip
    passes through sunrise and sunset.

    The strip spans more than one day, so each day's run of hours is labelled
    with its date. Those labels stick to the left edge while their day is in
    view and are pushed off by the next one, which keeps the date of whatever
    you are looking at on screen at all times.
    """

    __gtype_name__ = "NimbusHourlyStrip"

    def __init__(self) -> None:
        super().__init__()
        self._hours: list[HourEntry] = []
        self._tz: tzinfo | None = None
        self._moon_phase = 0.5
        self._wet = False
        #: How dark each hour's background is, 0 = full day, 1 = full night.
        self._nightness: list[float] = []
        #: (first index, last index + 1, label) for each calendar day shown.
        self._day_groups: list[tuple[int, int, str]] = []
        self._adjustment: Gtk.Adjustment | None = None

        self.set_size_request(-1, HOURLY_HEIGHT_DRY)
        self.set_hexpand(True)

    # -- data -------------------------------------------------------------

    def set_hours(
        self,
        hours: list[HourEntry],
        tz: tzinfo | None,
        moon_phase: float = 0.5,
        latitude: float | None = None,
        longitude: float | None = None,
    ) -> None:
        self._hours = hours
        self._tz = tz
        self._moon_phase = moon_phase
        self._wet = any(h.precip_chance >= PRECIP_FLOOR for h in hours)

        # Solar altitude per hour. This is the cheap part of the astronomy
        # module -- no rise/set search -- so doing it for 48 hours is fine.
        self._nightness = []
        if latitude is not None and longitude is not None:
            for hour in hours:
                jd = astro.to_julian(hour.time)
                alt = astro.altitude(astro.sun_position(jd), jd, latitude, longitude)
                self._nightness.append(_nightness(alt))
        else:
            self._nightness = [0.0 if h.is_daytime else 1.0 for h in hours]

        self._day_groups = self._group_by_day(hours, tz)

        self.set_size_request(
            max(1, len(hours)) * HOUR_COLUMN,
            HOURLY_HEIGHT if self._wet else HOURLY_HEIGHT_DRY,
        )
        self.queue_draw()

    @staticmethod
    def _group_by_day(hours: list[HourEntry], tz) -> list[tuple[int, int, str]]:
        """Split the hours into runs sharing a local calendar date."""
        groups: list[tuple[int, int, str]] = []
        today = datetime.now(tz).date() if tz else datetime.now().date()

        start = 0
        current = None
        for index, hour in enumerate(hours):
            local = hour.time.astimezone(tz) if tz else hour.time
            day = local.date()
            if current is None:
                current, start = day, index
            elif day != current:
                groups.append((start, index, _day_label(current, today)))
                current, start = day, index
        if current is not None:
            groups.append((start, len(hours), _day_label(current, today)))
        return groups

    def _night_at(self, index: int) -> float:
        if not self._nightness:
            return 0.0
        return self._nightness[min(index, len(self._nightness) - 1)]

    # -- scroll tracking --------------------------------------------------

    def _hadjustment(self) -> Gtk.Adjustment | None:
        scroller = self.get_ancestor(Gtk.ScrolledWindow)
        if scroller is None:
            return None
        adjustment = scroller.get_hadjustment()
        if adjustment is not self._adjustment:
            # Redraw as the view scrolls so the pinned header and the sticky
            # date labels keep up with it.
            self._adjustment = adjustment
            adjustment.connect("value-changed", lambda *_: self.queue_draw())
        return adjustment

    # -- painting ---------------------------------------------------------

    def do_snapshot(self, snapshot) -> None:  # type: ignore[override]
        width, height = self.get_width(), self.get_height()
        if width <= 0 or height <= 0 or not self._hours:
            return

        rect = Graphene.Rect().init(0, 0, width, height)
        cr = snapshot.append_cairo(rect)

        dark_theme = Adw.StyleManager.get_default().get_dark()
        day_bg, night_bg = _STRIP_BG_DARK if dark_theme else _STRIP_BG_LIGHT
        _, _, accent = _theme_colors(self)

        adjustment = self._hadjustment()
        view_left = adjustment.get_value() if adjustment else 0.0

        hours = self._hours
        temps = [h.temperature for h in hours]
        low, high = min(temps), max(temps)
        span = max(1.0, high - low)

        # Vertical bands of the chart, top to bottom.
        row_time = HEADER_BAND + 18.0
        row_glyph = HEADER_BAND + 54.0
        curve_top = HEADER_BAND + 94.0
        curve_bottom = HEADER_BAND + 148.0
        row_precip_top = HEADER_BAND + 164.0
        row_precip_h = 26.0

        def column_x(index: int) -> float:
            return index * HOUR_COLUMN + HOUR_COLUMN / 2

        def point(index: int) -> tuple[float, float]:
            y = curve_bottom - (temps[index] - low) / span * (curve_bottom - curve_top)
            return column_x(index), y

        points = [point(i) for i in range(len(hours))]

        def bg_at(index: int) -> RGB:
            return mix(day_bg, night_bg, self._night_at(index))

        def accent_at(index: int) -> RGB:
            # Lift the accent over night so the curve keeps its punch.
            return mix(accent, mix(accent, (1.0, 1.0, 1.0), 0.45), self._night_at(index))

        def bg_at_x(x: float) -> RGB:
            return bg_at(max(0, min(len(hours) - 1, int(x / HOUR_COLUMN))))

        def horizontal(colors, alpha: float = 1.0):
            """A gradient with one stop per hour, spanning the whole strip."""
            grad = cairo.LinearGradient(0, 0, width, 0)
            for index in range(len(hours)):
                offset = min(1.0, max(0.0, column_x(index) / width))
                grad.add_color_stop_rgba(offset, *colors(index), alpha)
            return grad

        # -- background ---------------------------------------------------
        cr.save()
        cr.set_source(horizontal(bg_at))
        cr.rectangle(0, 0, width, height)
        cr.fill()
        cr.restore()

        # -- area under the curve ----------------------------------------
        # A horizontal colour ramp masked by a vertical alpha ramp, which
        # gives the fill both the day/night tint and its downward fade.
        cr.save()
        drawing.smooth_path(cr, points)
        cr.line_to(points[-1][0], height)
        cr.line_to(points[0][0], height)
        cr.close_path()
        cr.clip()
        cr.set_source(horizontal(accent_at))
        fade = cairo.LinearGradient(0, curve_top - 10, 0, row_precip_top)
        fade.add_color_stop_rgba(0.0, 0, 0, 0, 0.30)
        fade.add_color_stop_rgba(1.0, 0, 0, 0, 0.03)
        cr.mask(fade)
        cr.restore()

        # -- the curve ----------------------------------------------------
        cr.save()
        drawing.smooth_path(cr, points)
        cr.set_source(horizontal(accent_at, 0.95))
        cr.set_line_width(2.4)
        cr.set_line_join(cairo.LINE_JOIN_ROUND)
        cr.set_line_cap(cairo.LINE_CAP_ROUND)
        cr.stroke()
        cr.restore()

        # -- per-column contents ------------------------------------------
        for index, hour in enumerate(hours):
            x = column_x(index)
            night = self._night_at(index)
            background = bg_at(index)
            fg = _readable_on(background)
            dim = mix(fg, background, 0.42)
            local = hour.time.astimezone(self._tz) if self._tz else hour.time

            # Day boundaries get a divider.
            if index > 0 and local.hour == 0:
                cr.set_source_rgba(*fg, 0.20)
                cr.set_line_width(1.0)
                cr.move_to(index * HOUR_COLUMN, HEADER_BAND)
                cr.line_to(index * HOUR_COLUMN, height - 12)
                cr.stroke()

            if index == 0:
                _draw_text(cr, self, "Now", x, row_time, 12.5, fg, 0.95, bold=True)
            else:
                emphasis = local.hour % 3 == 0
                _draw_text(
                    cr, self, local.strftime("%-I %p"), x, row_time, 12.0,
                    fg if emphasis else dim, 0.95 if emphasis else 0.75,
                )

            drawing.draw_glyph(
                cr, hour.condition, x - 17, row_glyph - 17, 34,
                is_day=hour.is_daytime, moon_phase=self._moon_phase,
                colors=drawing.blend_colors(
                    drawing.ON_LIGHT_COLORS, drawing.ON_SKY_COLORS, night
                ),
            )

            px, py = points[index]
            cr.set_source_rgba(*accent_at(index), 1.0)
            cr.arc(px, py, 3.0, 0, TAU)
            cr.fill()
            _draw_text(
                cr, self, f"{hour.temperature:.0f}°", px, py - 17, 13.5,
                fg, 0.98, bold=True,
            )

            if self._wet and hour.precip_chance >= PRECIP_FLOOR:
                bar_h = row_precip_h * min(1.0, hour.precip_chance / 100.0)
                drawing.rounded_rect(
                    cr, x - 9, row_precip_top + (row_precip_h - bar_h),
                    18, bar_h, 4,
                )
                cr.set_source_rgba(*accent_at(index), 0.55)
                cr.fill()
                _draw_text(
                    cr, self, f"{hour.precip_chance:.0f}%", x,
                    row_precip_top + row_precip_h + 12, 10.5, dim, 0.95,
                )

        self._draw_header(cr, width, view_left, bg_at_x)

    def _draw_header(self, cr, width: float, view_left: float, bg_at_x) -> None:
        """The pinned title and the sticky per-day date labels."""
        title = "HOURLY FORECAST"
        title_x = view_left + 18.0
        title_fg = _readable_on(bg_at_x(title_x + 50))

        _draw_text(
            cr, self, title, title_x, HEADER_BAND / 2 + 1, 11.0,
            title_fg, 0.62, bold=True, center=False,
        )

        title_layout = _layout(self, title, 11.0, bold=True)
        gutter = title_x + title_layout.get_pixel_size()[0] + 26.0

        for start, stop, label in self._day_groups:
            group_left = start * HOUR_COLUMN
            group_right = stop * HOUR_COLUMN

            layout = _layout(self, label, 12.5, bold=True)
            text_w = layout.get_pixel_size()[0]

            # Stick to the gutter while this day is in view, but never let a
            # label outrun its own section.
            x = max(group_left + 6.0, gutter)
            x = min(x, group_right - text_w - 10.0)
            if x + text_w < view_left or x > view_left + width:
                continue
            # A day being pushed off by its section's right edge slides left
            # of the gutter and into the pinned title; hide it rather than
            # let the two collide.
            if x < gutter:
                continue

            _draw_text(
                cr, self, label, x, HEADER_BAND / 2 + 1, 12.5,
                _readable_on(bg_at_x(x + text_w / 2)), 0.95,
                bold=True, center=False,
            )


class DayRangeBar(Gtk.Widget):
    """The high/low range bar shown on each row of the 7-day forecast.

    Every bar is positioned against the whole week's temperature range, so
    the rows read as a single comparable chart rather than seven unrelated
    gauges.
    """

    __gtype_name__ = "NimbusDayRangeBar"

    def __init__(self) -> None:
        super().__init__()
        self._low = 0.0
        self._high = 0.0
        self._week_low = 0.0
        self._week_high = 1.0
        self._is_today = False
        self._now_temp: float | None = None
        # Narrow enough that a 7-day row still fits a single flow column;
        # hexpand stretches the bar when there is more room.
        self.set_size_request(80, 26)
        self.set_hexpand(True)
        self.set_valign(Gtk.Align.CENTER)

    def set_range(
        self, low: float, high: float, week_low: float, week_high: float,
        is_today: bool = False, now_temp: float | None = None,
    ) -> None:
        self._low, self._high = low, high
        self._week_low, self._week_high = week_low, week_high
        self._is_today = is_today
        self._now_temp = now_temp
        self.queue_draw()

    def do_snapshot(self, snapshot) -> None:  # type: ignore[override]
        width, height = self.get_width(), self.get_height()
        if width <= 0 or height <= 0:
            return
        rect = Graphene.Rect().init(0, 0, width, height)
        cr = snapshot.append_cairo(rect)
        fg, dim, _ = _theme_colors(self)

        span = max(1.0, self._week_high - self._week_low)
        track_h = 7.0
        y = (height - track_h) / 2

        cr.set_source_rgba(*dim, 0.16)
        drawing.rounded_rect(cr, 0, y, width, track_h, track_h / 2)
        cr.fill()

        start = (self._low - self._week_low) / span * width
        end = (self._high - self._week_low) / span * width
        bar_w = max(track_h, end - start)

        # Cool-to-warm ramp keyed to where the bar sits in the week's range.
        grad = cairo.LinearGradient(start, 0, start + bar_w, 0)
        grad.add_color_stop_rgb(0.0, *_temp_color(self._low))
        grad.add_color_stop_rgb(1.0, *_temp_color(self._high))
        cr.set_source(grad)
        drawing.rounded_rect(cr, start, y, bar_w, track_h, track_h / 2)
        cr.fill()

        if self._is_today and self._now_temp is not None:
            marker = (self._now_temp - self._week_low) / span * width
            marker = max(3.0, min(width - 3.0, marker))
            cr.set_source_rgba(*fg, 0.92)
            cr.arc(marker, height / 2, 4.2, 0, TAU)
            cr.fill()
            cr.set_source_rgba(0, 0, 0, 0.35)
            cr.set_line_width(1.4)
            cr.arc(marker, height / 2, 4.2, 0, TAU)
            cr.stroke()


#: Anchor colours for the temperature ramp, in degrees Fahrenheit.
_TEMP_STOPS: tuple[tuple[float, RGB], ...] = (
    (-10.0, (0.42, 0.36, 0.78)),
    (20.0, (0.30, 0.55, 0.88)),
    (40.0, (0.33, 0.72, 0.82)),
    (58.0, (0.40, 0.76, 0.52)),
    (72.0, (0.94, 0.78, 0.31)),
    (86.0, (0.94, 0.55, 0.25)),
    (100.0, (0.88, 0.30, 0.28)),
    (115.0, (0.72, 0.16, 0.34)),
)


def _temp_color(temp: float) -> RGB:
    """Map a Fahrenheit temperature onto the cool-to-warm ramp."""
    from ..conditions import mix

    if temp <= _TEMP_STOPS[0][0]:
        return _TEMP_STOPS[0][1]
    if temp >= _TEMP_STOPS[-1][0]:
        return _TEMP_STOPS[-1][1]
    for i in range(len(_TEMP_STOPS) - 1):
        low_t, low_c = _TEMP_STOPS[i]
        high_t, high_c = _TEMP_STOPS[i + 1]
        if low_t <= temp <= high_t:
            return mix(low_c, high_c, (temp - low_t) / (high_t - low_t))
    return _TEMP_STOPS[-1][1]


def temp_color(temp: float) -> RGB:
    """Public alias for the temperature ramp."""
    return _temp_color(temp)


class MoonDial(Gtk.Widget):
    """A moon rendered at its true phase, for the details panel."""

    __gtype_name__ = "NimbusMoonDial"

    def __init__(self, size: int = 84) -> None:
        super().__init__()
        self._phase = 0.5
        self._size = size
        self.set_size_request(size, size)
        self.set_halign(Gtk.Align.CENTER)

    def set_phase(self, phase: float) -> None:
        self._phase = phase
        self.queue_draw()

    def do_snapshot(self, snapshot) -> None:  # type: ignore[override]
        width, height = self.get_width(), self.get_height()
        if width <= 0 or height <= 0:
            return
        rect = Graphene.Rect().init(0, 0, width, height)
        cr = snapshot.append_cairo(rect)
        # Keep radius * halo_scale within half the widget so the glow fades
        # out before the edge rather than being clipped into a bright square.
        radius = min(width, height) * 0.30
        drawing.draw_moon(
            cr, width / 2, height / 2, radius, self._phase,
            drawing.DEFAULT_COLORS, glow=0.55, show_dark_limb=True,
            halo_scale=1.65,
        )


class SunArc(Gtk.Widget):
    """Sunrise-to-sunset arc with the sun's current position marked."""

    __gtype_name__ = "NimbusSunArc"

    def __init__(self) -> None:
        super().__init__()
        self._progress = 0.5
        self._is_day = True
        self._sunrise = ""
        self._sunset = ""
        self.set_size_request(-1, 96)
        self.set_hexpand(True)

    def set_arc(
        self, progress: float, is_day: bool, sunrise: str, sunset: str
    ) -> None:
        self._progress = max(0.0, min(1.0, progress))
        self._is_day = is_day
        self._sunrise, self._sunset = sunrise, sunset
        self.queue_draw()

    def do_snapshot(self, snapshot) -> None:  # type: ignore[override]
        width, height = self.get_width(), self.get_height()
        if width <= 0 or height <= 0:
            return
        rect = Graphene.Rect().init(0, 0, width, height)
        cr = snapshot.append_cairo(rect)
        fg, dim, accent = _theme_colors(self)

        inset = 26.0
        baseline = height - 26.0
        arc_h = height - 52.0
        left, right = inset, width - inset
        if right <= left:
            return

        def arc_point(t: float) -> tuple[float, float]:
            x = left + (right - left) * t
            y = baseline - math.sin(t * math.pi) * arc_h
            return x, y

        # Horizon line.
        cr.set_source_rgba(*dim, 0.25)
        cr.set_line_width(1.0)
        cr.move_to(6, baseline)
        cr.line_to(width - 6, baseline)
        cr.stroke()

        # The full arc, dimmed, then the elapsed portion highlighted.
        steps = 60
        cr.set_source_rgba(*dim, 0.28)
        cr.set_line_width(2.0)
        cr.set_dash([3.0, 4.0])
        for i in range(steps + 1):
            x, y = arc_point(i / steps)
            if i:
                cr.line_to(x, y)
            else:
                cr.move_to(x, y)
        cr.stroke()
        cr.set_dash([])

        if self._is_day:
            cr.set_source_rgba(*accent, 0.85)
            cr.set_line_width(2.6)
            span = max(1, int(steps * self._progress))
            for i in range(span + 1):
                x, y = arc_point(i / steps)
                if i:
                    cr.line_to(x, y)
                else:
                    cr.move_to(x, y)
            cr.stroke()

            sx, sy = arc_point(self._progress)
            drawing.draw_sun(cr, sx, sy, 8.0, drawing.DEFAULT_COLORS, rays=True, glow=0.9)

        _draw_text(cr, self, self._sunrise, left, baseline + 14, 11.5, dim, 0.9)
        _draw_text(cr, self, self._sunset, right, baseline + 14, 11.5, dim, 0.9)
