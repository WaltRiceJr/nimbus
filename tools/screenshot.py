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

"""Drive the real app and capture its window to PNG files.

Renders through the window's own GSK renderer rather than a compositor
screenshot API, so it works regardless of the desktop's screenshot policy and
captures exactly the widget tree.

Usage:  python3 tools/screenshot.py OUTPUT_DIR [step ...]

Steps run in order, one per capture:
  dashboard   the pinned-location grid
  search:TEXT type TEXT into the search bar
  open:N      open the Nth pinned location (0-based)
  expand      toggle the details panel on the open location
  scroll:N    scroll the open location page down by N pixels
"""

from __future__ import annotations

import os
import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from nimbus.app import NimbusApplication  # noqa: E402

#: Seconds to let network fetches and animations settle before each capture.
SETTLE = 6.0
BETWEEN = 3.5


def capture(widget: Gtk.Widget, path: str, title: str | None = None) -> bool:
    """Grab the window to *path*.

    Prefers an X11 grab of the real on-screen window. Rendering the widget
    tree through a WidgetPaintable takes a different text path that
    mis-rasterises very large glyphs, so it cannot be trusted for judging
    typography. Falls back to the offscreen route when X11 is unavailable.
    """
    if os.environ.get("GDK_BACKEND") == "x11":
        import subprocess

        # Grab by window title. `import` drops into interactive click-to-
        # select when the name does not match, which blocks forever, so the
        # call is bounded and falls through to the offscreen path.
        target = title or os.environ.get("NIMBUS_SHOT_WINDOW") or "Nimbus"
        try:
            result = subprocess.run(
                ["import", "-window", target, path],
                capture_output=True, timeout=10,
            )
        except subprocess.TimeoutExpired:
            print(f"  ! x11 grab timed out for window {target!r}")
        else:
            if result.returncode == 0 and os.path.exists(path):
                print(f"  captured {os.path.basename(path)}  (on-screen, {target!r})")
                return True
            print(f"  ! x11 grab failed: {result.stderr.decode().strip()[:80]}")

    native = widget.get_native()
    if native is None:
        print(f"  ! no native surface for capture -> {path}")
        return False
    renderer = native.get_renderer()
    if renderer is None:
        print(f"  ! no renderer for capture -> {path}")
        return False

    width, height = widget.get_width(), widget.get_height()
    if width <= 0 or height <= 0:
        print(f"  ! widget not sized ({width}x{height}) -> {path}")
        return False

    paintable = Gtk.WidgetPaintable.new(widget)
    snapshot = Gtk.Snapshot()
    paintable.snapshot(snapshot, width, height)
    node = snapshot.to_node()
    if node is None:
        print(f"  ! empty render node -> {path}")
        return False

    texture = renderer.render_texture(node, None)
    texture.save_to_png(path)
    print(f"  captured {os.path.basename(path)}  ({width}x{height})")
    return True


class Driver:
    """Runs the capture steps against a live window on the main loop."""

    def __init__(self, app: NimbusApplication, outdir: str, steps: list[str]) -> None:
        self.app = app
        self.outdir = outdir
        self.steps = steps
        self.index = 0
        os.makedirs(outdir, exist_ok=True)

    def start(self) -> None:
        GLib.timeout_add(int(SETTLE * 1000), self._next)

    @property
    def window(self):
        return self.app.props.active_window

    def _next(self) -> bool:
        if self.index >= len(self.steps):
            self.app.quit()
            return GLib.SOURCE_REMOVE

        step = self.steps[self.index]
        self.index += 1
        print(f"[{self.index}/{len(self.steps)}] {step}")

        try:
            self._perform(step)
        except Exception as exc:  # noqa: BLE001 - a driver fault must not hang
            print(f"  ! step failed: {exc}")

        # Give the step's effect (navigation, fetch, animation) time to land.
        GLib.timeout_add(int(BETWEEN * 1000), self._shoot, step)
        return GLib.SOURCE_REMOVE

    def _shoot(self, step: str) -> bool:
        name = step.replace(":", "-").replace(" ", "_")
        # Capture whichever window is frontmost, so steps that open a
        # secondary window (the alert reader) photograph that instead. The
        # alert window is not registered with the application, so the full
        # toplevel list is what finds it.
        # PyGObject returns a plain list here, not a GListModel.
        target = self.window
        for win in Gtk.Window.list_toplevels():
            if win is not self.window and win.get_visible() and win.get_title():
                target = win
        capture(
            target,
            os.path.join(self.outdir, f"{self.index:02d}-{name}.png"),
            title=target.get_title(),
        )
        GLib.timeout_add(400, self._next)
        return GLib.SOURCE_REMOVE

    # -- steps ------------------------------------------------------------

    def _perform(self, step: str) -> None:
        window = self.window
        dashboard = window._dashboard

        if step == "dashboard":
            return

        if step.startswith("search:"):
            # Go through the window so it pops back to the dashboard first,
            # exactly as the Ctrl+F accelerator does.
            window.begin_search()
            dashboard._search_entry.set_text(step.split(":", 1)[1])
            return

        if step.startswith("open:"):
            index = int(step.split(":", 1)[1])
            cards = dashboard._cards
            if index < len(cards):
                cards[index].open()
            return

        if step.startswith("hover:"):
            # Force the prelight state so :hover styling (the remove button)
            # shows up in a capture, where there is no real pointer.
            index = int(step.split(":", 1)[1])
            cards = dashboard._cards
            if index < len(cards):
                cards[index].set_state_flags(Gtk.StateFlags.PRELIGHT, False)
            return

        if step.startswith("alert:"):
            # Open the Nth alert's detail window on the visible location page.
            index = int(step.split(":", 1)[1])
            page = window._navigation.get_visible_page()
            cards = []
            child = page._alert_box.get_first_child()
            while child is not None:
                cards.append(child)
                child = child.get_next_sibling()
            if index < len(cards):
                cards[index].emit("clicked")
            return

        if step == "expand":
            page = window._navigation.get_visible_page()
            page._expand_button.set_active(True)
            return

        if step.startswith("scroll:"):
            amount = float(step.split(":", 1)[1])
            page = window._navigation.get_visible_page()
            adjustment = page._scroller.get_vadjustment()
            limit = adjustment.get_upper() - adjustment.get_page_size()
            adjustment.set_value(max(0.0, min(amount, limit)))
            print(f"    scroll -> asked {amount:.0f}, limit {limit:.0f}, "
                  f"got {adjustment.get_value():.0f} "
                  f"(content {adjustment.get_upper():.0f}, view {adjustment.get_page_size():.0f})")
            return

        print(f"  ! unknown step {step!r}")


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2

    outdir = sys.argv[1]
    steps = sys.argv[2:] or ["dashboard"]

    app = NimbusApplication()
    driver = Driver(app, outdir, steps)

    def on_activate(_app) -> None:
        driver.start()

    app.connect("activate", on_activate)
    return app.run([sys.argv[0]])


if __name__ == "__main__":
    sys.exit(main())
