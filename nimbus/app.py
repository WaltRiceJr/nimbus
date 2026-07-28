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

"""Application entry point."""

from __future__ import annotations

import logging
import os
import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from .ui.window import NimbusWindow  # noqa: E402

APP_ID = "org.nimbus.Weather"
VERSION = "1.0.0"

log = logging.getLogger(__name__)


class NimbusApplication(Adw.Application):
    """The Nimbus weather application."""

    def __init__(self) -> None:
        super().__init__(
            application_id=APP_ID,
            flags=Gio.ApplicationFlags.DEFAULT_FLAGS,
        )
        self._window: NimbusWindow | None = None

        self.create_action("quit", self._on_quit, ["<primary>q"])
        self.create_action("about", self._on_about)
        self.create_action("search", self._on_search, ["<primary>f"])
        self.create_action("refresh", self._on_refresh, ["<primary>r", "F5"])

    def create_action(
        self, name: str, callback, accels: list[str] | None = None
    ) -> None:
        action = Gio.SimpleAction.new(name, None)
        action.connect("activate", callback)
        self.add_action(action)
        if accels:
            self.set_accels_for_action(f"app.{name}", accels)

    def do_startup(self) -> None:  # type: ignore[override]
        Adw.Application.do_startup(self)
        self._load_styles()

    def do_activate(self) -> None:  # type: ignore[override]
        if self._window is None:
            self._window = NimbusWindow(self)
        self._window.present()

    def _load_styles(self) -> None:
        css_path = os.path.join(os.path.dirname(__file__), "ui", "style.css")
        provider = Gtk.CssProvider()
        try:
            provider.load_from_path(css_path)
        except GLib.Error as exc:
            log.error("could not load stylesheet: %s", exc)
            return

        display = Gdk.Display.get_default()
        if display is not None:
            Gtk.StyleContext.add_provider_for_display(
                display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
            )

    # -- actions ----------------------------------------------------------

    def _on_quit(self, *_args) -> None:
        self.quit()

    def _on_search(self, *_args) -> None:
        if self._window is not None:
            self._window.begin_search()

    def _on_refresh(self, *_args) -> None:
        if self._window is not None:
            self._window.refresh_active()

    def _on_about(self, *_args) -> None:
        about = Adw.AboutDialog(
            application_name="Nimbus",
            application_icon="weather-few-clouds-symbolic",
            version=VERSION,
            developer_name="Walter Rice",
            copyright="Copyright © 2026 Walter Rice",
            comments=(
                "All weather data is provided by the United States "
                "National Weather Service. This application is an "
                "independent project and is not affiliated with or "
                "endorsed by the National Weather Service or NOAA. "
                "Sun and moon positions are computed locally."
            ),
            license_type=Gtk.License.GPL_3_0,
        )
        # add_link instead of the website property, so the row reads
        # "Project Website" rather than the stock "Website" label.
        about.add_link("Project Website", "https://github.com/WaltRiceJr/nimbus")
        about.add_credit_section("Data", ["National Weather Service (api.weather.gov)"])
        about.add_credit_section("Geocoding", ["Open-Meteo"])
        if self._window is not None:
            about.present(self._window)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO if os.environ.get("NIMBUS_DEBUG") else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    app = NimbusApplication()
    return app.run(argv if argv is not None else sys.argv)


if __name__ == "__main__":
    sys.exit(main())
