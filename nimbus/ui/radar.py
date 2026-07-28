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

"""The radar map shown in a location's expanded view.

Two NOAA WMS layers are requested over an identical Web Mercator extent --
geopolitical boundaries and the regional reflectivity mosaic -- so they
overlay exactly. The boundary layer ships as dark lines on transparency,
which would vanish against a dark card, so it is used as a mask and painted
in a colour chosen to suit the current theme rather than drawn as-is.

The mosaic layers are time-enabled, so the last hour of scans can be fetched
and played as a loop. The current scan is the one worth reading, so the loop
rests on it, then returns to the beginning and runs forward again.
"""

from __future__ import annotations

import io
import logging
import math
from datetime import datetime, timezone

import cairo
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Graphene", "1.0")
from gi.repository import Adw, GLib, Graphene, Gtk  # noqa: E402

from ..conditions import RGB, mix
from ..model import Location
from ..nws import RADAR_FRAME_COUNT
from .charts import _draw_text, _layout, _readable_on

log = logging.getLogger(__name__)

TAU = math.pi * 2

#: Ground widths available from the zoom control, in kilometres.
ZOOM_LEVELS = (80.0, 160.0, 320.0, 640.0, 1200.0)
DEFAULT_ZOOM = 2

MAP_HEIGHT = 340

#: Wait this long after a resize before re-requesting imagery, so dragging a
#: window edge does not fire a request per frame.
RESIZE_DEBOUNCE_MS = 450
#: Ignore resizes smaller than this; the imagery is scaled to fit meanwhile.
RESIZE_THRESHOLD = 48

#: How long each past scan is held while the loop plays.
FRAME_MS = 340
#: And how long the loop rests on the current scan before starting over. The
#: pause has to read as deliberate rather than as a stutter, so it is several
#: times a normal frame.
CURRENT_HOLD_MS = 2600


class RadarMap(Gtk.Widget):
    """A radar view centred on one location."""

    __gtype_name__ = "NimbusRadarMap"

    def __init__(self) -> None:
        super().__init__()
        self._location: Location | None = None
        self._service = None

        self._basemap: cairo.ImageSurface | None = None
        self._legend: cairo.ImageSurface | None = None

        #: Decoded echoes as (image, scan time), oldest first.
        self._echoes: list[tuple[cairo.ImageSurface, datetime | None]] = []
        self._index = 0
        self._anim_source = 0

        self._zoom = DEFAULT_ZOOM
        self._fetched_at: datetime | None = None
        self._status = "Loading radar…"
        self._requested_size: tuple[int, int] = (0, 0)
        self._resize_source = 0
        self._token = 0
        #: The request generation whose loop has already been asked for, so
        #: expanding and collapsing the panel cannot queue it twice.
        self._loop_token = -1

        self.set_size_request(-1, MAP_HEIGHT)
        self.set_hexpand(True)
        self.set_overflow(Gtk.Overflow.HIDDEN)

    # -- configuration ----------------------------------------------------

    def configure(self, service, location: Location) -> None:
        self._service = service
        self._location = location
        self._requested_size = (0, 0)
        self._status = "Loading radar…"
        self.refresh()

    @property
    def width_km(self) -> float:
        return ZOOM_LEVELS[self._zoom]

    def can_zoom(self, step: int) -> bool:
        return 0 <= self._zoom + step < len(ZOOM_LEVELS)

    def zoom(self, step: int) -> None:
        if not self.can_zoom(step):
            return
        self._zoom += step
        self._requested_size = (0, 0)  # force a refetch at the new extent
        self.refresh()

    def refresh(self) -> None:
        if self._service is None or self._location is None:
            return
        width = self.get_width() or 720
        height = self.get_height() or MAP_HEIGHT
        self._requested_size = (width, height)

        self._token += 1
        token = self._token

        def apply_current(sequence) -> None:
            if token != self._token:
                return  # superseded by a later request
            self._adopt(sequence)
            self._load_loop()

        def failed(error: Exception) -> None:
            if token != self._token:
                return
            self._status = f"Radar unavailable — {error}"
            self.queue_draw()

        self._service.load_radar(
            self._location, self.width_km, width, height, apply_current, failed
        )

        if self._legend is None:
            self._service.load_radar_legend(self._set_legend, lambda _e: None)

    def _load_loop(self) -> None:
        """Fetch the frames behind the current scan, once they can be seen.

        The loop costs a request per frame, so it waits for two things: a
        current scan already on screen, and a mapped widget. The radar card
        lives inside the details panel, which starts collapsed, so a location
        the user never expands never asks for any of this.
        """
        if self._loop_token == self._token or not self.get_mapped():
            return
        if self._service is None or self._location is None or not self._echoes:
            return

        # Expanding the panel gives the map its real size for the first time.
        # Fetching a dozen frames for the placeholder size, only for the
        # debounced refetch to replace every one of them, is pure waste.
        width, height = self._requested_size
        if (
            abs(self.get_width() - width) >= RESIZE_THRESHOLD
            or abs(self.get_height() - height) >= RESIZE_THRESHOLD
        ):
            return

        self._loop_token = self._token
        token = self._token

        def apply_loop(sequence) -> None:
            if token != self._token:
                return
            self._adopt(sequence)

        def failed(error: Exception) -> None:
            # The current scan is already showing; losing the history costs
            # the animation, not the map.
            if token == self._token:
                self._loop_token = -1
            log.debug("radar loop unavailable: %s", error)

        self._service.load_radar(
            self._location,
            self.width_km,
            width,
            height,
            apply_loop,
            failed,
            count=RADAR_FRAME_COUNT,
        )

    def _adopt(self, sequence) -> None:
        """Show a freshly fetched sequence, resting on its current scan."""
        echoes = [
            (image, echo.valid_at)
            for echo in sequence.echoes
            if (image := _surface(echo.reflectivity)) is not None
        ]
        if not echoes:
            return

        self._basemap = _surface(sequence.basemap)
        self._echoes = echoes
        self._index = len(echoes) - 1  # the current scan, where the loop rests
        self._fetched_at = sequence.fetched_at
        self._status = ""

        # Drop any frame already scheduled against the previous sequence, so
        # the new one gets its full pause on the current scan rather than
        # inheriting whatever was left of an earlier frame's timer.
        self._stop_animation()
        self._sync_animation()
        self.queue_draw()

    def _set_legend(self, payload: bytes) -> None:
        self._legend = _surface(payload)
        self.queue_draw()

    # -- animation --------------------------------------------------------

    def _sync_animation(self) -> None:
        if len(self._echoes) > 1 and self.get_mapped():
            self._arm()
        else:
            self._stop_animation()

    def _arm(self) -> None:
        """Schedule the next frame, dwelling on the current scan."""
        if self._anim_source or len(self._echoes) < 2 or not self.get_mapped():
            return
        resting = self._index == len(self._echoes) - 1
        self._anim_source = GLib.timeout_add(
            CURRENT_HOLD_MS if resting else FRAME_MS, self._advance
        )

    def _advance(self) -> bool:
        self._anim_source = 0
        self._index = (self._index + 1) % len(self._echoes)
        self.queue_draw()
        self._arm()
        return GLib.SOURCE_REMOVE

    def _stop_animation(self) -> None:
        if self._anim_source:
            GLib.source_remove(self._anim_source)
            self._anim_source = 0

    def do_map(self) -> None:  # type: ignore[override]
        Gtk.Widget.do_map(self)
        self._load_loop()
        self._sync_animation()

    def do_unmap(self) -> None:  # type: ignore[override]
        self._stop_animation()
        Gtk.Widget.do_unmap(self)

    # -- resizing ---------------------------------------------------------

    def do_size_allocate(self, width: int, height: int, baseline: int) -> None:  # type: ignore[override]
        Gtk.Widget.do_size_allocate(self, width, height, baseline)

        requested_w, requested_h = self._requested_size
        if (
            abs(width - requested_w) < RESIZE_THRESHOLD
            and abs(height - requested_h) < RESIZE_THRESHOLD
        ):
            # Close enough to keep the imagery, so no refetch will follow --
            # which makes this the moment a loop deferred at map time can go.
            self._load_loop()
            return

        if self._resize_source:
            GLib.source_remove(self._resize_source)
        self._resize_source = GLib.timeout_add(
            RESIZE_DEBOUNCE_MS, self._on_resize_settled
        )

    def _on_resize_settled(self) -> bool:
        self._resize_source = 0
        self.refresh()
        return GLib.SOURCE_REMOVE

    def do_unroot(self) -> None:  # type: ignore[override]
        if self._resize_source:
            GLib.source_remove(self._resize_source)
            self._resize_source = 0
        self._stop_animation()
        Gtk.Widget.do_unroot(self)

    # -- painting ---------------------------------------------------------

    def do_snapshot(self, snapshot) -> None:  # type: ignore[override]
        width, height = self.get_width(), self.get_height()
        if width <= 0 or height <= 0:
            return

        cr = snapshot.append_cairo(Graphene.Rect().init(0, 0, width, height))
        dark = Adw.StyleManager.get_default().get_dark()

        water: RGB = (0.055, 0.075, 0.115) if dark else (0.118, 0.145, 0.204)
        land: RGB = mix(water, (1.0, 1.0, 1.0), 0.06)
        lines: RGB = mix(water, (1.0, 1.0, 1.0), 0.55)
        fg = _readable_on(water)

        cr.set_source_rgb(*land)
        cr.rectangle(0, 0, width, height)
        cr.fill()

        # Both layers were rendered at the size requested last time; if the
        # widget has since changed size, scale them to fit until the debounced
        # refetch lands.
        if self._basemap is not None:
            self._paint_layer(cr, self._basemap, width, height, mask_color=lines)
        if self._echoes:
            echo = self._echoes[self._index][0]
            self._paint_layer(cr, echo, width, height, alpha=0.92)

        # Radar returns can be bright anywhere, so the bottom captions get
        # their own scrim rather than relying on the map being dark there.
        scrim = cairo.LinearGradient(0, height - 58, 0, height)
        scrim.add_color_stop_rgba(0.0, 0, 0, 0, 0.0)
        scrim.add_color_stop_rgba(1.0, 0, 0, 0, 0.62)
        cr.set_source(scrim)
        cr.rectangle(0, height - 58, width, 58)
        cr.fill()

        self._draw_marker(cr, width, height, fg)
        self._draw_scale_bar(cr, width, height, fg)
        self._draw_caption(cr, width, height, fg)
        self._draw_timeline(cr, width, height)
        self._draw_legend(cr, width, height)

        if self._status:
            cr.set_source_rgba(*water, 0.72)
            cr.rectangle(0, 0, width, height)
            cr.fill()
            _draw_text(cr, self, self._status, width / 2, height / 2, 13.0, fg, 0.95)

    def _paint_layer(
        self,
        cr,
        surface: cairo.ImageSurface,
        width: int,
        height: int,
        mask_color: RGB | None = None,
        alpha: float = 1.0,
    ) -> None:
        source_w, source_h = surface.get_width(), surface.get_height()
        if source_w <= 0 or source_h <= 0:
            return

        cr.save()
        cr.scale(width / source_w, height / source_h)
        if mask_color is not None:
            # The boundary layer is dark lines on transparency. Using it as a
            # mask paints our own colour through its alpha, which keeps the
            # coastlines visible on a dark map.
            cr.set_source_rgba(*mask_color, alpha)
            cr.mask_surface(surface, 0, 0)
        else:
            cr.set_source_surface(surface, 0, 0)
            cr.paint_with_alpha(alpha)
        cr.restore()

    def _draw_marker(self, cr, width: int, height: int, fg: RGB) -> None:
        cx, cy = width / 2, height / 2

        cr.set_source_rgba(0, 0, 0, 0.55)
        cr.set_line_width(3.4)
        cr.arc(cx, cy, 6.0, 0, TAU)
        cr.stroke()

        cr.set_source_rgba(*fg, 0.98)
        cr.set_line_width(1.8)
        cr.arc(cx, cy, 6.0, 0, TAU)
        cr.stroke()

        cr.set_source_rgba(*fg, 0.95)
        cr.arc(cx, cy, 2.0, 0, TAU)
        cr.fill()

    def _draw_scale_bar(self, cr, width: int, height: int, fg: RGB) -> None:
        if self.width_km <= 0:
            return
        # Choose a round distance that fills roughly a fifth of the view.
        target = self.width_km / 5.0
        nice = min(
            (10, 20, 25, 50, 100, 200, 250, 500),
            key=lambda candidate: abs(candidate - target),
        )
        bar_px = width * (nice / self.width_km)

        x, y = 16.0, height - 22.0
        cr.set_source_rgba(0, 0, 0, 0.55)
        cr.set_line_width(4.0)
        cr.move_to(x, y)
        cr.line_to(x + bar_px, y)
        cr.stroke()

        cr.set_source_rgba(1.0, 1.0, 1.0, 0.95)
        cr.set_line_width(2.0)
        cr.move_to(x, y)
        cr.line_to(x + bar_px, y)
        cr.stroke()
        for tick in (x, x + bar_px):
            cr.move_to(tick, y - 4)
            cr.line_to(tick, y + 4)
        cr.stroke()

        _draw_text(
            cr, self, f"{nice:g} km", x + bar_px / 2, y - 12, 10.5,
            (1.0, 1.0, 1.0), 0.92,
        )

    def _draw_caption(self, cr, width: int, height: int, fg: RGB) -> None:
        parts = ["NOAA base reflectivity"]

        moment = self._frame_time()
        if moment is not None:
            parts.append(moment.astimezone().strftime("%-I:%M %p"))

        # On any frame but the last, say how far back it is, so a time from
        # forty minutes ago cannot be mistaken for the current picture.
        newest = self._echoes[-1][1] if self._echoes else None
        if moment is not None and newest is not None and newest != moment:
            behind = (newest - moment).total_seconds() / 60.0
            parts.append(f"\N{MINUS SIGN}{behind:.0f} min")

        text = "   ·   ".join(parts)

        # Measure rather than estimate, so the caption sits flush to the edge.
        text_w = _layout(self, text, 10.5).get_pixel_size()[0]
        _draw_text(
            cr, self, text, width - text_w - 16, height - 20, 10.5,
            (1.0, 1.0, 1.0), 0.82, center=False,
        )

    def _frame_time(self) -> datetime | None:
        """When the scan on screen was valid, however well that is known."""
        if self._echoes:
            valid_at = self._echoes[self._index][1]
            if valid_at is not None:
                return valid_at
        return self._fetched_at

    def _draw_timeline(self, cr, width: int, height: int) -> None:
        """A slim track showing where in the hour the visible frame sits."""
        if len(self._echoes) < 2:
            return

        left, right = 16.0, width - 16.0
        y = height - 8.0
        head = left + (right - left) * (self._index / (len(self._echoes) - 1))

        cr.save()
        cr.set_line_cap(cairo.LINE_CAP_ROUND)
        cr.set_line_width(2.5)

        cr.set_source_rgba(1.0, 1.0, 1.0, 0.22)
        cr.move_to(left, y)
        cr.line_to(right, y)
        cr.stroke()

        cr.set_source_rgba(1.0, 1.0, 1.0, 0.85)
        cr.move_to(left, y)
        cr.line_to(head, y)
        cr.stroke()
        cr.restore()

    def _draw_legend(self, cr, width: int, height: int) -> None:
        if self._legend is None:
            return
        source_w = self._legend.get_width()
        source_h = self._legend.get_height()
        if source_w <= 0:
            return

        target_w = min(300.0, width - 32.0)
        scale = target_w / source_w
        x, y = 16.0, 14.0

        cr.save()
        cr.translate(x, y)
        cr.scale(scale, scale)
        cr.set_source_surface(self._legend, 0, 0)
        cr.paint_with_alpha(0.92)
        cr.restore()


def _surface(payload: bytes) -> cairo.ImageSurface | None:
    try:
        return cairo.ImageSurface.create_from_png(io.BytesIO(payload))
    except Exception:  # noqa: BLE001 - a bad tile must not take the page down
        return None


class RadarCard(Gtk.Box):
    """The radar map plus its heading and zoom controls."""

    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.add_css_class("card-panel")

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)

        title = Gtk.Label(label="Radar", xalign=0.0)
        title.add_css_class("section-title")
        title.set_hexpand(True)
        header.append(title)

        self._range_label = Gtk.Label()
        self._range_label.add_css_class("radar-range")
        header.append(self._range_label)

        self._zoom_in = Gtk.Button.new_from_icon_name("zoom-in-symbolic")
        self._zoom_in.add_css_class("flat")
        self._zoom_in.set_tooltip_text("Zoom in")
        self._zoom_in.connect("clicked", lambda *_: self._zoom(-1))
        header.append(self._zoom_in)

        self._zoom_out = Gtk.Button.new_from_icon_name("zoom-out-symbolic")
        self._zoom_out.add_css_class("flat")
        self._zoom_out.set_tooltip_text("Zoom out")
        self._zoom_out.connect("clicked", lambda *_: self._zoom(1))
        header.append(self._zoom_out)

        self.append(header)

        self.map = RadarMap()
        self.map.add_css_class("radar-map")
        self.append(self.map)

        self._sync()

    def configure(self, service, location: Location) -> None:
        self.map.configure(service, location)
        self._sync()

    def _zoom(self, step: int) -> None:
        self.map.zoom(step)
        self._sync()

    def _sync(self) -> None:
        self._range_label.set_label(f"{self.map.width_km:g} km across")
        self._zoom_in.set_sensitive(self.map.can_zoom(-1))
        self._zoom_out.set_sensitive(self.map.can_zoom(1))
