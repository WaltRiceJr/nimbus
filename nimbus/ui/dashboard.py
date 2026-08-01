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

"""The dashboard: pinned locations at a glance, plus location search."""

from __future__ import annotations

from datetime import datetime, timezone

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GLib, Gio, GObject, Gtk, Pango  # noqa: E402

from .. import astro
from ..conditions import display_name
from ..model import Condition, Location, WeatherBundle
from ..sky import GlyphIcon, SkyView

#: Cards are square, and stay square: the size is fixed rather than being a
#: minimum, so neither dimension stretches to fill the window.
CARD_WIDTH = 240
CARD_HEIGHT = 240

#: How long a search box sits idle before the query is sent.
SEARCH_DEBOUNCE_MS = 320


class LocationCard(Gtk.Overlay):
    """One pinned location, showing live conditions over its own sky.

    The clickable card and the remove button are siblings inside this overlay
    rather than parent and child. A button nested inside another button does
    not reliably receive its own clicks -- the outer button's gesture claims
    the sequence first, so every press on the trash can opened the location
    instead of deleting it.
    """

    def __init__(self, dashboard: "DashboardPage", location: Location) -> None:
        super().__init__()
        self._dashboard = dashboard
        self.location = location
        self.bundle: WeatherBundle | None = None

        self.add_css_class("card-root")
        # Pin the card to its natural size inside whatever cell the flow box
        # hands it, so the square never stretches with the window.
        self.set_hexpand(False)
        self.set_vexpand(False)
        self.set_halign(Gtk.Align.CENTER)
        self.set_valign(Gtk.Align.START)

        self._card = Gtk.Button()
        self._card.add_css_class("location-card")
        self._card.set_size_request(CARD_WIDTH, CARD_HEIGHT)
        self._card.set_hexpand(False)
        self._card.set_vexpand(False)
        # The sky fills the whole button, so without clipping it paints over
        # the rounded corners the card's border-radius defines.
        self._card.set_overflow(Gtk.Overflow.HIDDEN)
        self.set_child(self._card)

        overlay = Gtk.Overlay()

        self._sky = SkyView(Condition.CLEAR, compact=True)
        self._sky.set_size_request(CARD_WIDTH, CARD_HEIGHT)
        overlay.set_child(self._sky)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        content.add_css_class("card-content")

        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        name_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        name_box.set_hexpand(True)
        name_box.set_valign(Gtk.Align.START)

        self._name = Gtk.Label(label=location.name, xalign=0.0)
        self._name.add_css_class("card-place")
        self._name.set_ellipsize(Pango.EllipsizeMode.END)
        name_box.append(self._name)

        self._state = Gtk.Label(label=location.state, xalign=0.0)
        self._state.add_css_class("card-state")
        name_box.append(self._state)
        top.append(name_box)

        self._icon = GlyphIcon(Condition.CLEAR, size=44, on_sky=True)
        self._icon.set_valign(Gtk.Align.START)
        top.append(self._icon)
        content.append(top)

        spacer = Gtk.Box()
        spacer.set_vexpand(True)
        content.append(spacer)

        # On a near-square card the readings stack, which gives the condition
        # text a full line instead of squeezing it beside the temperature.
        bottom = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        bottom.set_valign(Gtk.Align.END)

        self._temp = Gtk.Label(label="--°", xalign=0.0)
        self._temp.add_css_class("card-temp")
        bottom.append(self._temp)

        self._condition = Gtk.Label(label="Loading…", xalign=0.0)
        self._condition.add_css_class("card-condition")
        self._condition.set_ellipsize(Pango.EllipsizeMode.END)
        bottom.append(self._condition)

        self._range = Gtk.Label(label="", xalign=0.0)
        self._range.add_css_class("card-range")
        self._range.set_ellipsize(Pango.EllipsizeMode.END)
        bottom.append(self._range)

        content.append(bottom)
        overlay.add_overlay(content)
        self._card.set_child(overlay)

        # Removing the card is the only thing this button can do, so it shows
        # a trash can rather than a star -- a star reads as a toggle whose
        # "on" state is already implied by the card being on the dashboard.
        # It is added to the outer overlay, so it sits beside the card button
        # rather than inside it and receives its own clicks.
        self._remove = Gtk.Button.new_from_icon_name("user-trash-symbolic")
        self._remove.add_css_class("card-remove")
        self._remove.set_halign(Gtk.Align.END)
        self._remove.set_valign(Gtk.Align.END)
        self._remove.set_tooltip_text("Remove from dashboard")
        self._remove.connect("clicked", self._on_remove)
        self.add_overlay(self._remove)

        self._card.connect("clicked", self._on_clicked)

        menu = Gio.Menu()
        menu.append("Open", "card.open")
        menu.append("Move left", "card.left")
        menu.append("Move right", "card.right")
        menu.append("Remove from dashboard", "card.remove")

        actions = Gio.SimpleActionGroup()
        for name, handler in (
            ("open", lambda *_: self._on_clicked(self)),
            ("left", lambda *_: self._dashboard.move_card(self, -1)),
            ("right", lambda *_: self._dashboard.move_card(self, 1)),
            ("remove", lambda *_: self._on_remove(self)),
        ):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", handler)
            actions.add_action(action)
        self.insert_action_group("card", actions)

        self._popover = Gtk.PopoverMenu.new_from_model(menu)
        self._popover.set_parent(self)
        self._popover.set_has_arrow(False)

        gesture = Gtk.GestureClick(button=3)
        gesture.connect("pressed", self._on_right_click)
        self.add_controller(gesture)

        # Drag to rearrange: the card is both a drag handle and a drop
        # target; dropping card A on card B gives A B's position.
        drag = Gtk.DragSource()
        drag.set_actions(Gdk.DragAction.MOVE)
        drag.connect("prepare", self._on_drag_prepare)
        drag.connect("drag-begin", self._on_drag_begin)
        self._card.add_controller(drag)

        drop = Gtk.DropTarget.new(GObject.TYPE_STRING, Gdk.DragAction.MOVE)
        drop.connect("drop", self._on_drop)
        self.add_controller(drop)

    def _on_drag_prepare(self, _source, _x, _y) -> Gdk.ContentProvider:
        return Gdk.ContentProvider.new_for_value(self.location.key)

    def _on_drag_begin(self, source: Gtk.DragSource, _drag) -> None:
        source.set_icon(
            Gtk.WidgetPaintable.new(self._card),
            CARD_WIDTH // 2, CARD_HEIGHT // 2,
        )

    def _on_drop(self, _target, value, _x, _y) -> bool:
        return self._dashboard.reorder_card(str(value), self)

    def _on_right_click(self, _gesture, _n, x, y) -> None:
        self._popover.set_pointing_to(Gdk.Rectangle(x=int(x), y=int(y), width=1, height=1))
        self._popover.popup()

    def open(self) -> None:
        """Activate the card, as clicking it would."""
        self._card.emit("clicked")

    def _on_clicked(self, _button) -> None:
        self._dashboard.open_location(self.location, self.bundle)

    def _on_remove(self, _button) -> None:
        self._dashboard.remove_location(self.location)

    def apply_bundle(self, bundle: WeatherBundle) -> None:
        self.bundle = bundle
        self.location = bundle.location
        tz = bundle.location.tz()
        now = datetime.now(timezone.utc)

        sun = astro.sun_times(now, bundle.location.latitude, bundle.location.longitude, tz)
        moon = astro.moon_info(now, bundle.location.latitude, bundle.location.longitude, tz)

        current = bundle.current
        condition = current.condition if current else Condition.UNKNOWN

        self._sky.set_scene(
            condition, now,
            bundle.location.latitude, bundle.location.longitude, tz,
            wind_mph=current.wind_speed if current else None,
        )
        self._icon.set_weather(condition, sun.is_daytime, moon.phase)

        self._name.set_label(bundle.location.name)
        self._state.set_label(bundle.location.state)

        if current and current.temperature is not None:
            self._temp.set_label(f"{current.temperature:.0f}°")
        self._condition.set_label(
            (current.description if current else "") or display_name(condition)
        )

        today = bundle.today
        if today:
            high = f"{today.high:.0f}°" if today.high is not None else "--"
            low = f"{today.low:.0f}°" if today.low is not None else "--"
            local_time = now.astimezone(tz).strftime("%-I:%M %p")
            self._range.set_label(f"H {high}  L {low}  ·  {local_time}")

        # The ring is styled on the card button, which is what draws the frame.
        if bundle.alerts:
            self._card.add_css_class("has-alert")
            self._card.set_tooltip_text(bundle.alerts[0].event)
        else:
            self._card.remove_css_class("has-alert")
            self._card.set_tooltip_text(None)

    def show_error(self, error: Exception) -> None:
        self._condition.set_label("Unavailable")
        self._range.set_label(str(error)[:60])


class SearchResultRow(Gtk.ListBoxRow):
    """One geocoder hit, with a pin toggle."""

    def __init__(self, dashboard: "DashboardPage", location: Location) -> None:
        super().__init__()
        self.location = location
        self._dashboard = dashboard
        self.add_css_class("search-row")

        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)

        pin_icon = Gtk.Image.new_from_icon_name("mark-location-symbolic")
        pin_icon.add_css_class("search-icon")
        box.append(pin_icon)

        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        text.set_hexpand(True)
        text.set_valign(Gtk.Align.CENTER)

        name = Gtk.Label(label=location.name, xalign=0.0)
        name.add_css_class("search-name")
        text.append(name)

        subtitle = Gtk.Label(
            label=f"{location.state}   ·   {location.latitude:.3f}, {location.longitude:.3f}",
            xalign=0.0,
        )
        subtitle.add_css_class("search-subtitle")
        text.append(subtitle)
        box.append(text)

        self._pin = Gtk.ToggleButton()
        self._pin.set_valign(Gtk.Align.CENTER)
        self._pin.add_css_class("flat")
        self._pin.set_active(dashboard.favorites.contains(location))
        self._sync_icon()
        self._pin.connect("toggled", self._on_pin)
        box.append(self._pin)

        self.set_child(box)

    def _sync_icon(self) -> None:
        pinned = self._pin.get_active()
        self._pin.set_icon_name("starred-symbolic" if pinned else "non-starred-symbolic")
        self._pin.set_tooltip_text("Unpin" if pinned else "Pin to dashboard")

    def _on_pin(self, button: Gtk.ToggleButton) -> None:
        store = self._dashboard.favorites
        if button.get_active():
            store.add(self.location)
        else:
            store.remove(self.location)
        self._sync_icon()


class DashboardPage(Adw.NavigationPage):
    """Landing page: a grid of pinned locations and the search interface."""

    def __init__(self, window) -> None:
        super().__init__(title="NimbUS")
        self._window = window
        self._cards: list[LocationCard] = []
        self._search_timer = 0

        self._build()
        window.favorites.connect("changed", lambda *_: self.reload())
        self.connect("showing", lambda *_: self._window.set_active_page(self))

    @property
    def favorites(self):
        return self._window.favorites

    # -- construction -----------------------------------------------------

    def _build(self) -> None:
        toolbar = Adw.ToolbarView()

        header = Adw.HeaderBar()
        # Same treatment as the city page: the bar floats over the sky.
        header.add_css_class("flat")
        header.add_css_class("hero-header")
        header.set_title_widget(Adw.WindowTitle(title="NimbUS"))

        refresh = Gtk.Button.new_from_icon_name("view-refresh-symbolic")
        refresh.set_tooltip_text("Refresh all")
        refresh.connect("clicked", lambda *_: self.reload())
        header.pack_end(refresh)

        menu = Gio.Menu()
        menu.append("About NimbUS", "app.about")
        menu.append("Quit", "app.quit")
        menu_button = Gtk.MenuButton()
        menu_button.set_icon_name("open-menu-symbolic")
        menu_button.set_menu_model(menu)
        header.pack_end(menu_button)

        toolbar.add_top_bar(header)

        # The search box is a permanent part of the dashboard rather than a
        # mode toggled from the header: it is the only way to add a location,
        # so hiding it behind a button just adds a step.
        self._search_entry = Gtk.SearchEntry()
        self._search_entry.set_placeholder_text(
            "Search any US city, town or ZIP code…"
        )
        self._search_entry.set_hexpand(True)
        self._search_entry.connect("search-changed", self._on_search_changed)
        self._search_entry.connect("stop-search", self._on_stop_search)

        search_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        search_row.add_css_class("search-toolbar")
        clamp = Adw.Clamp(maximum_size=560)
        clamp.set_child(self._search_entry)
        clamp.set_hexpand(True)
        search_row.append(clamp)
        toolbar.add_top_bar(search_row)

        self._stack = Gtk.Stack()
        self._stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self._stack.set_transition_duration(180)

        self._stack.add_named(self._build_grid(), "grid")
        self._stack.add_named(self._build_empty(), "empty")
        self._stack.add_named(self._build_results(), "results")

        toolbar.set_content(self._stack)

        credit = Gtk.Label(
            label="Data provided by the National Weather Service and NOAA"
        )
        credit.add_css_class("data-credit")
        toolbar.add_bottom_bar(credit)

        # The live sky backs the whole dashboard, exactly as on a city page;
        # the first pinned city's weather drives the scene.
        self._sky = SkyView(Condition.CLEAR)
        self._sky.set_hexpand(True)
        self._sky.set_vexpand(True)

        overlay = Gtk.Overlay()
        overlay.set_child(self._sky)
        overlay.add_overlay(toolbar)
        # The sky itself has no minimum size; the content dictates it.
        overlay.set_measure_overlay(toolbar, True)
        self.set_child(overlay)

    def _build_grid(self) -> Gtk.Widget:
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        clamp = Adw.Clamp(maximum_size=1680)
        clamp.add_css_class("dashboard-clamp")

        self._flow = Gtk.FlowBox()
        self._flow.set_selection_mode(Gtk.SelectionMode.NONE)
        self._flow.set_homogeneous(True)
        self._flow.set_min_children_per_line(1)
        self._flow.set_max_children_per_line(6)
        self._flow.set_row_spacing(14)
        self._flow.set_column_spacing(14)
        # Centring makes the box take its natural size, so cards keep their
        # square proportions instead of stretching to fill the viewport; the
        # grid floats in the middle of the sky, scrolling once it outgrows it.
        self._flow.set_halign(Gtk.Align.CENTER)
        self._flow.set_valign(Gtk.Align.CENTER)
        self._flow.add_css_class("dashboard-grid")

        clamp.set_child(self._flow)
        scroller.set_child(clamp)
        return scroller

    def _build_empty(self) -> Gtk.Widget:
        status = Adw.StatusPage()
        status.add_css_class("dashboard-empty")
        status.set_icon_name("weather-few-clouds-symbolic")
        status.set_title("No pinned locations")
        status.set_description(
            "Search for any city, town or ZIP code in the United States, "
            "then pin it to see its conditions here."
        )
        button = Gtk.Button(label="Search locations")
        button.add_css_class("suggested-action")
        button.add_css_class("pill")
        button.set_halign(Gtk.Align.CENTER)
        button.connect("clicked", lambda *_: self.focus_search())
        status.set_child(button)
        return status

    def _build_results(self) -> Gtk.Widget:
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        clamp = Adw.Clamp(maximum_size=620)
        clamp.add_css_class("results-clamp")

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)

        self._results_status = Gtk.Label(label="")
        self._results_status.add_css_class("results-status")
        self._results_status.set_visible(False)
        box.append(self._results_status)

        self._results = Gtk.ListBox()
        self._results.set_selection_mode(Gtk.SelectionMode.NONE)
        self._results.add_css_class("boxed-list")
        self._results.connect("row-activated", self._on_result_activated)
        box.append(self._results)

        clamp.set_child(box)
        scroller.set_child(clamp)
        return scroller

    # -- search -----------------------------------------------------------

    def _on_search_changed(self, entry: Gtk.SearchEntry) -> None:
        """Debounce the query.

        This handler must never write to the entry or move focus. It runs on
        every keystroke, so doing either would fight the user as they type --
        setting the text here erases the character that triggered the signal.
        """
        if self._search_timer:
            GLib.source_remove(self._search_timer)
            self._search_timer = 0

        query = entry.get_text().strip()
        if len(query) < 2:
            # Too short to search: drop back to the grid but leave whatever
            # has been typed alone.
            self._window.service.next_token()  # discard any in-flight reply
            self._show_grid_or_empty()
            return

        self._search_timer = GLib.timeout_add(
            SEARCH_DEBOUNCE_MS, self._run_search, query
        )

    def _run_search(self, query: str) -> bool:
        self._search_timer = 0
        self._stack.set_visible_child_name("results")
        self._results_status.set_label("Searching…")
        self._results_status.set_visible(True)

        token = self._window.service.next_token()
        self._window.service.search(
            query,
            lambda results: self._show_results(query, results),
            lambda error: self._show_search_error(error),
            token=token,
        )
        return GLib.SOURCE_REMOVE

    def _show_results(self, query: str, results: list[Location]) -> None:
        self._clear_list(self._results)
        if not results:
            self._results_status.set_label(
                f"No US locations found for “{query}”."
            )
            self._results_status.set_visible(True)
            return

        self._results_status.set_visible(False)
        for location in results:
            self._results.append(SearchResultRow(self, location))

    def _show_search_error(self, error: Exception) -> None:
        self._clear_list(self._results)
        self._results_status.set_label(f"Search failed: {error}")
        self._results_status.set_visible(True)

    def _on_result_activated(self, _list, row: SearchResultRow) -> None:
        self.open_location(row.location, None)

    def _on_stop_search(self, _entry: Gtk.SearchEntry) -> None:
        """Escape in the search box: clearing here is what the user asked for."""
        self._search_entry.set_text("")
        self._show_grid_or_empty()

    @property
    def is_searching(self) -> bool:
        return bool(self._search_entry.get_text().strip())

    def focus_search(self) -> None:
        self._search_entry.grab_focus()

    # -- grid -------------------------------------------------------------

    @staticmethod
    def _clear_list(container) -> None:
        child = container.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            container.remove(child)
            child = nxt

    def _show_grid_or_empty(self) -> None:
        name = "grid" if len(self.favorites) else "empty"
        self._stack.set_visible_child_name(name)

    def reload(self) -> None:
        """Rebuild the card grid and refresh every pinned location."""
        self._clear_list(self._flow)
        self._cards = []

        for location in self.favorites:
            card = LocationCard(self, location)
            self._cards.append(card)
            self._flow.append(card)
            self._load_card(card)

        # Pinning a result rebuilds the grid behind the search view; switching
        # to it here would yank the list out from under the user mid-search.
        if not self.is_searching:
            self._show_grid_or_empty()

    def _load_card(self, card: LocationCard) -> None:
        self._window.service.load_weather(
            card.location,
            lambda bundle: self._on_card_loaded(card, bundle),
            lambda error: card.show_error(error),
        )

    def _on_card_loaded(self, card: LocationCard, bundle: WeatherBundle) -> None:
        card.apply_bundle(bundle)
        # Persist the resolved grid so later refreshes skip the /points call.
        self.favorites.update(bundle.location)
        # The first pinned city's weather is the page-wide backdrop.
        if self._cards and card is self._cards[0]:
            current = bundle.current
            self._sky.set_scene(
                current.condition if current else Condition.UNKNOWN,
                datetime.now(timezone.utc),
                bundle.location.latitude,
                bundle.location.longitude,
                bundle.location.tz(),
                wind_mph=current.wind_speed if current else None,
            )

    def move_card(self, card: LocationCard, offset: int) -> None:
        self.favorites.move(card.location, offset)

    def reorder_card(self, source_key: str, target: LocationCard) -> bool:
        """Drop handler: the dragged card takes the target card's position."""
        source = self.favorites.find(source_key)
        if source is None or source.key == target.location.key:
            return False
        keys = [item.key for item in self.favorites]
        offset = keys.index(target.location.key) - keys.index(source.key)
        self.favorites.move(source, offset)
        return True

    def remove_location(self, location: Location) -> None:
        self.favorites.remove(location)
        self._window.show_toast(f"Removed {location.label}")

    def open_location(self, location: Location, bundle: WeatherBundle | None) -> None:
        self._window.open_location(location, bundle)
