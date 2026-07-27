#!/usr/bin/env python3
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

"""Render SkyView scenes offscreen to a PNG contact sheet.

Drives the widget's internal paint routines against a plain image surface, so
scenes can be checked without opening a window.
"""

import os
import sys

import cairo
import gi

gi.require_version("Gtk", "4.0")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from nimbus.model import Condition  # noqa: E402
from nimbus.sky import SkyView  # noqa: E402

W, H = 300, 190
PAD = 12
LABEL = 22

#: (label, condition, sun altitude)
SCENES = [
    ("Clear noon", Condition.CLEAR, 62.0),
    ("Clear morning", Condition.CLEAR, 22.0),
    ("Golden hour", Condition.CLEAR, 3.0),
    ("Sunset", Condition.CLEAR, -1.0),
    ("Civil dusk", Condition.CLEAR, -6.0),
    ("Night clear", Condition.CLEAR, -25.0),
    ("Few clouds", Condition.FEW_CLOUDS, 40.0),
    ("Partly cloudy", Condition.PARTLY_CLOUDY, 35.0),
    ("Mostly cloudy", Condition.MOSTLY_CLOUDY, 30.0),
    ("Overcast", Condition.OVERCAST, 28.0),
    ("Rain", Condition.RAIN, 20.0),
    ("Showers", Condition.SHOWERS, 25.0),
    ("Thunderstorm", Condition.THUNDERSTORM, 18.0),
    ("Snow", Condition.SNOW, 15.0),
    ("Fog", Condition.FOG, 12.0),
    ("Night rain", Condition.RAIN, -22.0),
    ("Night snow", Condition.SNOW, -20.0),
    ("Night storm", Condition.THUNDERSTORM, -18.0),
]


def render(path: str) -> None:
    cols = 6
    rows = (len(SCENES) + cols - 1) // cols
    width = cols * (W + PAD) + PAD
    height = rows * (H + LABEL + PAD) + PAD

    sheet = cairo.ImageSurface(cairo.FORMAT_ARGB32, width, height)
    top = cairo.Context(sheet)
    top.set_source_rgb(0.07, 0.08, 0.11)
    top.paint()
    top.select_font_face("Cantarell")

    for index, (label, condition, altitude) in enumerate(SCENES):
        col, row = index % cols, index // cols
        x = PAD + col * (W + PAD)
        y = PAD + row * (H + LABEL + PAD)

        view = SkyView(condition)
        view.set_condition(condition, altitude)
        # Advance a little so particles are mid-flight rather than on their
        # seeded starting row.
        for _ in range(40):
            view._time += 0.05
            view._advance()

        tile = cairo.ImageSurface(cairo.FORMAT_ARGB32, W, H)
        cr = cairo.Context(tile)
        view._draw_gradient(cr, W, H)
        view._draw_stars(cr, W, H)
        view._paint_live(cr, W, H)

        top.save()
        from nimbus.drawing import rounded_rect

        rounded_rect(top, x, y, W, H, 14)
        top.clip()
        top.set_source_surface(tile, x, y)
        top.paint()
        top.restore()

        top.set_source_rgba(0.85, 0.88, 0.93, 0.95)
        top.set_font_size(12)
        top.move_to(x + 2, y + H + 15)
        top.show_text(label)

    sheet.write_to_png(path)
    print(f"wrote {path} ({width}x{height})")


if __name__ == "__main__":
    render(sys.argv[1] if len(sys.argv) > 1 else "sky.png")
