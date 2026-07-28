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

"""The application window and navigation between dashboard and locations."""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from ..model import Location, WeatherBundle
from ..nws import WeatherService
from ..store import FavoritesStore
from .dashboard import DashboardPage
from .detail import LocationPage

#: Window heights for the collapsed and expanded location views.
COMPACT_HEIGHT = 760
EXPANDED_HEIGHT = 980

#: Pinned locations are re-fetched on this cadence while the app is open.
#: The service's disk cache caps the real request rate, so a short timer
#: mostly picks up whatever has expired -- alerts in particular.
AUTO_REFRESH_SECONDS = 5 * 60


class NimbusWindow(Adw.ApplicationWindow):
    """Hosts the navigation stack, the shared service and the toast overlay."""

    def __init__(self, application: Adw.Application) -> None:
        super().__init__(application=application)
        # Capitalised NimbUS: the app is specifically about US weather.
        self.set_title("NimbUS")
        self.set_default_size(1080, COMPACT_HEIGHT)

        self.favorites = FavoritesStore()
        self.service = WeatherService()

        self._pages: dict[str, LocationPage] = {}
        self._active_page: Adw.NavigationPage | None = None
        self._refresh_source = 0

        self._toasts = Adw.ToastOverlay()
        self._navigation = Adw.NavigationView()
        self._toasts.set_child(self._navigation)
        self.set_content(self._toasts)

        self._dashboard = DashboardPage(self)
        self._navigation.add(self._dashboard)

        self.connect("close-request", self._on_close)
        self._dashboard.reload()
        self._start_auto_refresh()

    # -- navigation -------------------------------------------------------

    def set_active_page(self, page: Adw.NavigationPage) -> None:
        self._active_page = page

    def open_location(
        self, location: Location, bundle: WeatherBundle | None = None
    ) -> None:
        """Push the page for *location*, reusing it if already built."""
        page = self._pages.get(location.key)
        if page is None:
            page = LocationPage(self, location)
            self._pages[location.key] = page

        if self._navigation.find_page(location.key) is not None:
            # Already somewhere in the stack -- surface it rather than
            # pushing a second copy, which libadwaita rejects.
            self._navigation.pop_to_tag(location.key)
        else:
            self._navigation.push(page)

        if bundle is not None:
            page.apply_bundle(bundle)
        else:
            page.refresh()

    def note_expanded(self, expanded: bool) -> None:
        """Grow the window when a location page expands its details."""
        if self.is_maximized() or self.is_fullscreen():
            return
        width, height = self.get_default_size()
        target = EXPANDED_HEIGHT if expanded else COMPACT_HEIGHT
        if expanded and height >= target:
            return
        self.set_default_size(width, target)

    def show_toast(self, message: str) -> None:
        self._toasts.add_toast(Adw.Toast(title=message, timeout=4))

    def begin_search(self) -> None:
        """Pop back to the dashboard and focus its search entry."""
        while self._navigation.get_visible_page() is not self._dashboard:
            if not self._navigation.pop():
                break
        self._dashboard.focus_search()

    def refresh_active(self) -> None:
        """Reload whichever page is in front."""
        page = self._navigation.get_visible_page()
        if isinstance(page, LocationPage):
            page.refresh(force=True)
        else:
            self._dashboard.reload()

    # -- lifecycle --------------------------------------------------------

    def _start_auto_refresh(self) -> None:
        self._refresh_source = GLib.timeout_add_seconds(
            AUTO_REFRESH_SECONDS, self._on_auto_refresh
        )

    def _on_auto_refresh(self) -> bool:
        self._dashboard.reload()
        if isinstance(self._active_page, LocationPage):
            self._active_page.refresh()
        return GLib.SOURCE_CONTINUE

    def _on_close(self, *_args) -> bool:
        if self._refresh_source:
            GLib.source_remove(self._refresh_source)
            self._refresh_source = 0
        self.service.shutdown()
        return False
