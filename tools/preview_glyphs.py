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

"""Render every weather glyph and moon phase to a PNG contact sheet.

Used during development to eyeball the icon set without launching the app.
"""

import math
import os
import sys

import cairo

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from nimbus import drawing  # noqa: E402
from nimbus.model import Condition  # noqa: E402

CELL = 104
PAD = 14
LABEL_H = 20


def draw_sheet(path: str) -> None:
    conditions = [c for c in Condition]
    cols = 8
    rows = math.ceil(len(conditions) / cols)

    moon_cols = 8
    moon_rows = 1

    width = cols * (CELL + PAD) + PAD
    # Two condition blocks (day + night) plus a moon-phase strip.
    height = (
        PAD
        + 30 + rows * (CELL + LABEL_H + PAD)
        + 30 + rows * (CELL + LABEL_H + PAD)
        + 30 + moon_rows * (CELL + LABEL_H + PAD)
        + PAD
    )

    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, width, height)
    cr = cairo.Context(surface)

    cr.set_source_rgb(0.09, 0.11, 0.16)
    cr.paint()

    cr.select_font_face("Cantarell", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)

    def header(text: str, y: float) -> None:
        cr.set_source_rgb(0.95, 0.96, 0.98)
        cr.set_font_size(17)
        cr.move_to(PAD, y)
        cr.show_text(text)

    def label(text: str, x: float, y: float) -> None:
        cr.set_source_rgba(0.78, 0.82, 0.88, 0.9)
        cr.set_font_size(11)
        extents = cr.text_extents(text)
        cr.move_to(x + (CELL - extents.width) / 2, y)
        cr.show_text(text)

    y = PAD + 20

    for is_day, title in ((True, "Day glyphs"), (False, "Night glyphs")):
        header(title, y)
        y += 18
        for index, condition in enumerate(conditions):
            col, row = index % cols, index // cols
            x = PAD + col * (CELL + PAD)
            cy = y + row * (CELL + LABEL_H + PAD)

            cr.set_source_rgba(1, 1, 1, 0.045)
            drawing.rounded_rect(cr, x, cy, CELL, CELL, 16)
            cr.fill()

            drawing.draw_glyph(
                cr, condition, x + CELL * 0.1, cy + CELL * 0.1, CELL * 0.8,
                is_day=is_day, moon_phase=0.22,
                colors=drawing.ON_SKY_COLORS,
            )
            label(condition.value, x, cy + CELL + 14)
        y += rows * (CELL + LABEL_H + PAD) + 30

    header("Moon phases (0.0 new -> 0.5 full -> 1.0 new)", y)
    y += 18
    for i in range(moon_cols):
        phase = i / moon_cols
        x = PAD + i * (CELL + PAD)

        cr.set_source_rgba(1, 1, 1, 0.045)
        drawing.rounded_rect(cr, x, y, CELL, CELL, 16)
        cr.fill()

        drawing.draw_moon(
            cr, x + CELL / 2, y + CELL / 2, CELL * 0.30, phase,
            drawing.DEFAULT_COLORS, glow=0.8,
        )
        illum = (1 - math.cos(phase * 2 * math.pi)) / 2
        label(f"{phase:.3f}  {illum * 100:.0f}%", x, y + CELL + 14)

    surface.write_to_png(path)
    print(f"wrote {path} ({width}x{height})")


if __name__ == "__main__":
    draw_sheet(sys.argv[1] if len(sys.argv) > 1 else "glyphs.png")
