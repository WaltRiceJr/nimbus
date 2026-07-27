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

"""Generate the application icon from the app's own drawing primitives.

Writes a set of PNG sizes into data/icons/.
"""

import os
import sys

import cairo

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from nimbus import drawing  # noqa: E402
from nimbus.conditions import palette  # noqa: E402
from nimbus.model import Condition  # noqa: E402

SIZES = (16, 24, 32, 48, 64, 128, 256, 512)


def render(size: int, path: str) -> None:
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, size, size)
    cr = cairo.Context(surface)

    # A late-afternoon clear sky makes the warmest backdrop for the glyph.
    sky = palette(Condition.CLEAR, 18.0)

    grad = cairo.LinearGradient(0, 0, 0, size)
    grad.add_color_stop_rgb(0.0, *sky.top)
    grad.add_color_stop_rgb(0.6, *sky.middle)
    grad.add_color_stop_rgb(1.0, *sky.bottom)
    cr.set_source(grad)
    drawing.rounded_rect(cr, 0, 0, size, size, size * 0.225)
    cr.fill()

    drawing.draw_glyph(
        cr, Condition.PARTLY_CLOUDY,
        size * 0.11, size * 0.13, size * 0.78,
        is_day=True, colors=drawing.ON_SKY_COLORS,
    )

    surface.write_to_png(path)


def main() -> int:
    out = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "data", "icons"
    )
    os.makedirs(out, exist_ok=True)
    for size in SIZES:
        path = os.path.join(out, f"nimbus-{size}.png")
        render(size, path)
        print(f"wrote {os.path.relpath(path)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
