"""The per-location page: hero, hourly strip, and the expandable panel."""

from __future__ import annotations

import math
from datetime import datetime, timezone

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk, Pango  # noqa: E402

from .. import astro
from ..conditions import display_name
from ..model import Alert, Condition, DayEntry, Location, WeatherBundle
from ..sky import GlyphIcon, SkyView
from .charts import DayRangeBar, HourlyStrip, MoonDial, SunArc, add_drag_to_pan
from .radar import RadarCard

HERO_HEIGHT = 340


def _fmt_time(moment: datetime | None, tz) -> str:
    if moment is None:
        return "--"
    return moment.astimezone(tz).strftime("%-I:%M %p")


def _fmt_temp(value: float | None) -> str:
    return "--" if value is None else f"{value:.0f}°"


def _fmt(value: float | None, suffix: str = "", digits: int = 0) -> str:
    if value is None:
        return "--"
    return f"{value:.{digits}f}{suffix}"


def _duration(delta) -> str:
    if delta is None:
        return "--"
    total = int(delta.total_seconds())
    return f"{total // 3600}h {total % 3600 // 60}m"


class StatTile(Gtk.Box):
    """A single labelled statistic in the details grid."""

    def __init__(self, title: str, value: str = "--", caption: str = "") -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self.add_css_class("stat-tile")

        self._title = Gtk.Label(label=title.upper(), xalign=0.0)
        self._title.add_css_class("stat-title")

        self._value = Gtk.Label(label=value, xalign=0.0)
        self._value.add_css_class("stat-value")
        self._value.set_wrap(False)
        self._value.set_ellipsize(Pango.EllipsizeMode.END)

        self._caption = Gtk.Label(label=caption, xalign=0.0)
        self._caption.add_css_class("stat-caption")
        self._caption.set_wrap(True)
        self._caption.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self._caption.set_visible(bool(caption))

        self.append(self._title)
        self.append(self._value)
        self.append(self._caption)

    def update(self, value: str, caption: str = "") -> None:
        self._value.set_label(value)
        self._caption.set_label(caption)
        self._caption.set_visible(bool(caption))


class AlertCard(Gtk.Button):
    """A tappable summary of one active alert, expanding to the full text."""

    def __init__(self, alert: Alert, tz) -> None:
        super().__init__()
        self._alert = alert
        self._tz = tz
        self.add_css_class("alert-card")
        self.add_css_class(f"severity-{alert.severity.lower()}")
        self.set_hexpand(True)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        icon = Gtk.Image.new_from_icon_name("dialog-warning-symbolic")
        icon.add_css_class("alert-icon")
        header.append(icon)

        title = Gtk.Label(label=alert.event, xalign=0.0)
        title.add_css_class("alert-title")
        title.set_hexpand(True)
        header.append(title)

        if alert.expires:
            until = Gtk.Label(label=f"until {_fmt_time(alert.expires, tz)}", xalign=1.0)
            until.add_css_class("alert-until")
            header.append(until)

        outer.append(header)

        if alert.headline:
            headline = Gtk.Label(label=alert.headline, xalign=0.0)
            headline.add_css_class("alert-headline")
            headline.set_wrap(True)
            headline.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
            outer.append(headline)

        self.set_child(outer)
        self.connect("clicked", self._on_clicked)

    def _on_clicked(self, _button) -> None:
        """Open the full alert text in its own resizable window.

        A real window rather than a dialog: NWS alert bodies run to hundreds
        of lines and are laid out for a fixed-width display -- county lists
        are column-aligned -- so the text is shown in a monospace face with
        its original line breaks intact, and the user gets to size the window
        to suit it.
        """
        alert = self._alert

        body = alert.description or alert.headline
        if alert.instruction:
            body = (
                f"{body}\n\n"
                f"PRECAUTIONARY/PREPAREDNESS ACTIONS\n\n"
                f"{alert.instruction}"
            )

        window = Adw.Window()
        window.set_title(alert.event)
        window.set_default_size(860, 660)
        root = self.get_root()
        if isinstance(root, Gtk.Window):
            window.set_transient_for(root)

        toolbar = Adw.ToolbarView()

        subtitle_parts = [alert.severity]
        if alert.onset:
            subtitle_parts.append(f"issued {_fmt_time(alert.onset, self._tz)}")
        if alert.expires:
            subtitle_parts.append(f"until {_fmt_time(alert.expires, self._tz)}")

        header = Adw.HeaderBar()
        header.set_title_widget(
            Adw.WindowTitle(title=alert.event, subtitle="  ·  ".join(subtitle_parts))
        )
        toolbar.add_top_bar(header)

        text = Gtk.Label(label=body, xalign=0.0, yalign=0.0)
        text.set_wrap(True)
        # Wrap on whole words only; breaking mid-word would mangle the
        # column-aligned sections when the window is made narrow.
        text.set_wrap_mode(Pango.WrapMode.WORD)
        text.set_selectable(True)
        text.set_vexpand(True)
        text.add_css_class("alert-body")

        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroller.set_child(text)
        # Take the initial focus away from the label. A selectable GtkLabel
        # selects its entire contents when focused, so left to itself the
        # window would open with every line highlighted.
        scroller.set_focusable(True)
        toolbar.set_content(scroller)

        window.set_content(toolbar)

        # Escape closes, as it would for a dialog.
        shortcuts = Gtk.ShortcutController()
        shortcuts.add_shortcut(
            Gtk.Shortcut.new(
                Gtk.ShortcutTrigger.parse_string("Escape"),
                Gtk.CallbackAction.new(lambda *_: window.close() or True),
            )
        )
        window.add_controller(shortcuts)

        window.present()

        # Focus assignment happens as the window maps, so this has to follow
        # present() to stick.
        window.set_focus(scroller)
        text.select_region(0, 0)


class DayRow(Gtk.Box):
    """One row of the multi-day forecast."""

    def __init__(self, day: DayEntry, is_today: bool) -> None:
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=14)
        self.add_css_class("day-row")

        name = "Today" if is_today else day.date.strftime("%A")
        label = Gtk.Label(label=name, xalign=0.0)
        label.add_css_class("day-name")
        label.set_size_request(96, -1)
        self.append(label)

        icon = GlyphIcon(day.condition, size=30)
        self.append(icon)

        precip = Gtk.Label(label=f"{day.precip_chance:.0f}%" if day.precip_chance >= 5 else "", xalign=0.0)
        precip.add_css_class("day-precip")
        precip.set_size_request(42, -1)
        self.append(precip)

        low = Gtk.Label(label=_fmt_temp(day.low), xalign=1.0)
        low.add_css_class("day-low")
        low.set_size_request(40, -1)
        self.append(low)

        self.bar = DayRangeBar()
        self.append(self.bar)

        high = Gtk.Label(label=_fmt_temp(day.high), xalign=0.0)
        high.add_css_class("day-high")
        high.set_size_request(40, -1)
        self.append(high)

        self.set_tooltip_text(day.detailed_forecast or day.short_forecast)


class LocationPage(Adw.NavigationPage):
    """Weather for one location, with a collapsed and an expanded state."""

    def __init__(self, window, location: Location) -> None:
        super().__init__(title=location.label)
        # Tagging by location key lets the window pop back to an already-open
        # page instead of pushing a duplicate.
        self.set_tag(location.key)
        self._window = window
        self._location = location
        self._bundle: WeatherBundle | None = None
        self._expanded = False

        self._build()
        self.connect("showing", lambda *_: self._window.set_active_page(self))

    # -- construction -----------------------------------------------------

    def _build(self) -> None:
        toolbar = Adw.ToolbarView()
        toolbar.set_extend_content_to_top_edge(True)

        header = Adw.HeaderBar()
        header.add_css_class("flat")
        header.add_css_class("hero-header")
        self._header = header

        self._pin_button = Gtk.ToggleButton()
        self._pin_button.set_icon_name("non-starred-symbolic")
        self._pin_button.set_tooltip_text("Pin to dashboard")
        self._pin_button.connect("toggled", self._on_pin_toggled)
        header.pack_start(self._pin_button)

        refresh = Gtk.Button.new_from_icon_name("view-refresh-symbolic")
        refresh.set_tooltip_text("Refresh")
        refresh.connect("clicked", lambda *_: self.refresh(force=True))
        header.pack_end(refresh)

        self._expand_button = Gtk.ToggleButton()
        self._expand_button.set_child(
            Gtk.Image.new_from_icon_name("go-down-symbolic")
        )
        self._expand_button.set_tooltip_text("Show forecast details")
        self._expand_button.connect("toggled", self._on_expand_toggled)
        header.pack_end(self._expand_button)

        toolbar.add_top_bar(header)

        self._scroller = Gtk.ScrolledWindow()
        self._scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self._scroller.set_vexpand(True)

        column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        column.append(self._build_hero())
        column.append(self._build_body())
        self._scroller.set_child(column)

        # The header floats transparently over the sky, so it needs a solid
        # background once the hero has scrolled out from under it.
        self._scroller.get_vadjustment().connect("value-changed", self._on_scrolled)

        toolbar.set_content(self._scroller)
        self.set_child(toolbar)

    def _on_scrolled(self, adjustment: Gtk.Adjustment) -> None:
        past_hero = adjustment.get_value() > HERO_HEIGHT - 78
        if past_hero == self._header.has_css_class("scrolled"):
            return
        if past_hero:
            self._header.add_css_class("scrolled")
            self._header.remove_css_class("flat")
        else:
            self._header.remove_css_class("scrolled")
            self._header.add_css_class("flat")

    def _build_hero(self) -> Gtk.Widget:
        overlay = Gtk.Overlay()
        overlay.add_css_class("hero")

        self._sky = SkyView(Condition.CLEAR)
        self._sky.set_size_request(-1, HERO_HEIGHT)
        overlay.set_child(self._sky)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        content.set_valign(Gtk.Align.CENTER)
        content.add_css_class("hero-content")

        self._place_label = Gtk.Label(label=self._location.label)
        self._place_label.add_css_class("hero-place")
        content.append(self._place_label)

        temp_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=14)
        temp_row.set_halign(Gtk.Align.CENTER)

        self._temp_label = Gtk.Label(label="--°")
        self._temp_label.add_css_class("hero-temp")
        temp_row.append(self._temp_label)

        self._hero_icon = GlyphIcon(Condition.CLEAR, size=76, on_sky=True)
        self._hero_icon.set_valign(Gtk.Align.CENTER)
        temp_row.append(self._hero_icon)
        content.append(temp_row)

        self._condition_label = Gtk.Label(label="Loading…")
        self._condition_label.add_css_class("hero-condition")
        content.append(self._condition_label)

        self._detail_label = Gtk.Label(label="")
        self._detail_label.add_css_class("hero-detail")
        content.append(self._detail_label)

        overlay.add_overlay(content)
        return overlay

    def _build_body(self) -> Gtk.Widget:
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        body.add_css_class("page-body")

        self._alert_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self._alert_box.set_visible(False)
        body.append(self._alert_box)

        # -- hourly ---------------------------------------------------
        # No padding and no separate heading: the strip paints its own
        # background edge to edge, and draws the heading and the date of each
        # day inside it so they sit on that background.
        hourly_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        hourly_card.add_css_class("card-panel")
        hourly_card.add_css_class("card-panel-flush")
        hourly_card.set_overflow(Gtk.Overflow.HIDDEN)

        hourly_scroll = Gtk.ScrolledWindow()
        hourly_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.NEVER)
        hourly_scroll.set_kinetic_scrolling(True)
        hourly_scroll.add_css_class("hourly-scroll")
        self._hourly = HourlyStrip()
        hourly_scroll.set_child(self._hourly)
        add_drag_to_pan(hourly_scroll)
        hourly_card.append(hourly_scroll)
        body.append(hourly_card)

        # -- expandable panel -----------------------------------------
        self._revealer = Gtk.Revealer()
        self._revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
        self._revealer.set_transition_duration(280)

        expanded = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        expanded.append(self._build_today_card())
        expanded.append(self._build_radar_card())
        expanded.append(self._build_week_card())
        expanded.append(self._build_details_card())
        self._revealer.set_child(expanded)
        body.append(self._revealer)

        self._reveal_hint = Gtk.Button(label="Show forecast details")
        self._reveal_hint.add_css_class("reveal-hint")
        self._reveal_hint.set_halign(Gtk.Align.CENTER)
        self._reveal_hint.connect(
            "clicked", lambda *_: self._expand_button.set_active(not self._expanded)
        )
        body.append(self._reveal_hint)

        return body

    def _build_today_card(self) -> Gtk.Widget:
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        card.add_css_class("card-panel")
        card.append(_section_title("Today's forecast"))

        self._today_period = Gtk.Label(xalign=0.0)
        self._today_period.add_css_class("forecast-period")
        card.append(self._today_period)

        self._today_text = Gtk.Label(xalign=0.0)
        self._today_text.add_css_class("forecast-text")
        self._today_text.set_wrap(True)
        self._today_text.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self._today_text.set_selectable(True)
        card.append(self._today_text)

        self._tonight_period = Gtk.Label(xalign=0.0)
        self._tonight_period.add_css_class("forecast-period")
        card.append(self._tonight_period)

        self._tonight_text = Gtk.Label(xalign=0.0)
        self._tonight_text.add_css_class("forecast-text")
        self._tonight_text.set_wrap(True)
        self._tonight_text.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self._tonight_text.set_selectable(True)
        card.append(self._tonight_text)

        return card

    def _build_radar_card(self) -> Gtk.Widget:
        self._radar = RadarCard()
        return self._radar

    def _build_week_card(self) -> Gtk.Widget:
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        card.add_css_class("card-panel")
        card.append(_section_title("7-day forecast"))
        self._week_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        card.append(self._week_box)
        return card

    def _build_details_card(self) -> Gtk.Widget:
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        card.add_css_class("card-panel")
        card.append(_section_title("Details & statistics"))

        # Sun arc across the top of the panel.
        self._sun_arc = SunArc()
        card.append(self._sun_arc)

        grid = Gtk.FlowBox()
        grid.set_selection_mode(Gtk.SelectionMode.NONE)
        grid.set_homogeneous(True)
        grid.set_min_children_per_line(2)
        grid.set_max_children_per_line(4)
        grid.set_row_spacing(6)
        grid.set_column_spacing(6)
        grid.add_css_class("stat-grid")

        self._tiles: dict[str, StatTile] = {}
        for key, title in (
            ("feels", "Feels like"),
            ("humidity", "Humidity"),
            ("dewpoint", "Dew point"),
            ("wind", "Wind"),
            ("pressure", "Pressure"),
            ("visibility", "Visibility"),
            ("sunrise", "Sunrise"),
            ("sunset", "Sunset"),
            ("daylength", "Day length"),
            ("moonrise", "Moonrise"),
            ("moonset", "Moonset"),
            ("station", "Station"),
        ):
            tile = StatTile(title)
            self._tiles[key] = tile
            grid.append(tile)
        card.append(grid)

        # Moon phase gets its own row with the drawn dial.
        moon_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=18)
        moon_row.add_css_class("moon-row")

        self._moon_dial = MoonDial(size=88)
        moon_row.append(self._moon_dial)

        moon_text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        moon_text.set_valign(Gtk.Align.CENTER)

        moon_title = Gtk.Label(label="MOON PHASE", xalign=0.0)
        moon_title.add_css_class("stat-title")
        moon_text.append(moon_title)

        self._moon_name = Gtk.Label(label="--", xalign=0.0)
        self._moon_name.add_css_class("stat-value")
        moon_text.append(self._moon_name)

        self._moon_caption = Gtk.Label(label="", xalign=0.0)
        self._moon_caption.add_css_class("stat-caption")
        moon_text.append(self._moon_caption)

        moon_row.append(moon_text)
        card.append(moon_row)

        return card

    # -- interaction ------------------------------------------------------

    def _on_expand_toggled(self, button: Gtk.ToggleButton) -> None:
        self._expanded = button.get_active()
        self._revealer.set_reveal_child(self._expanded)
        # Radar imagery is only fetched once the panel is open, so a collapsed
        # page costs nothing.
        if self._expanded:
            self._radar.configure(self._window.service, self._location)
        self._reveal_hint.set_label(
            "Hide forecast details" if self._expanded else "Show forecast details"
        )
        icon = "go-up-symbolic" if self._expanded else "go-down-symbolic"
        button.set_child(Gtk.Image.new_from_icon_name(icon))
        button.set_tooltip_text(
            "Hide forecast details" if self._expanded else "Show forecast details"
        )
        self._window.note_expanded(self._expanded)

    def _on_pin_toggled(self, button: Gtk.ToggleButton) -> None:
        store = self._window.favorites
        pinned = store.contains(self._location)
        if button.get_active() and not pinned:
            store.add(self._location)
        elif not button.get_active() and pinned:
            store.remove(self._location)
        self._sync_pin_icon()

    def _sync_pin_icon(self) -> None:
        pinned = self._window.favorites.contains(self._location)
        self._pin_button.set_icon_name(
            "starred-symbolic" if pinned else "non-starred-symbolic"
        )
        self._pin_button.set_tooltip_text(
            "Unpin from dashboard" if pinned else "Pin to dashboard"
        )
        if self._pin_button.get_active() != pinned:
            self._pin_button.handler_block_by_func(self._on_pin_toggled)
            self._pin_button.set_active(pinned)
            self._pin_button.handler_unblock_by_func(self._on_pin_toggled)

    # -- data -------------------------------------------------------------

    def refresh(self, force: bool = False) -> None:
        self._window.service.load_weather(
            self._location, self.apply_bundle, self._on_error
        )

    def _on_error(self, error: Exception) -> None:
        self._condition_label.set_label("Could not load weather")
        self._detail_label.set_label(str(error))
        self._window.show_toast(f"{self._location.label}: {error}")

    def apply_bundle(self, bundle: WeatherBundle) -> None:
        self._bundle = bundle
        self._location = bundle.location
        self.set_title(bundle.location.label)
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

        self._place_label.set_label(bundle.location.label)
        self._hero_icon.set_weather(condition, sun.is_daytime, moon.phase)

        if current:
            self._temp_label.set_label(_fmt_temp(current.temperature))
            self._condition_label.set_label(
                current.description or display_name(condition)
            )
            parts = []
            if current.feels_like is not None and current.temperature is not None:
                if abs(current.feels_like - current.temperature) >= 1.5:
                    parts.append(f"Feels like {current.feels_like:.0f}°")
            today = bundle.today
            if today:
                if today.high is not None:
                    parts.append(f"High {today.high:.0f}°")
                if today.low is not None:
                    parts.append(f"Low {today.low:.0f}°")
            self._detail_label.set_label("   ·   ".join(parts))

        self._update_alerts(bundle, tz)
        self._hourly.set_hours(
            bundle.hourly, tz, moon.phase,
            bundle.location.latitude, bundle.location.longitude,
        )
        self._update_today(bundle)
        self._update_week(bundle)
        self._update_details(bundle, sun, moon, tz, now)
        if self._expanded:
            self._radar.configure(self._window.service, bundle.location)
        self._sync_pin_icon()

    def _update_alerts(self, bundle: WeatherBundle, tz) -> None:
        child = self._alert_box.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self._alert_box.remove(child)
            child = nxt

        for alert in bundle.alerts:
            self._alert_box.append(AlertCard(alert, tz))
        self._alert_box.set_visible(bool(bundle.alerts))

    def _update_today(self, bundle: WeatherBundle) -> None:
        today = bundle.today
        if today is None:
            return
        self._today_period.set_label(today.name or "Today")
        self._today_text.set_label(today.detailed_forecast or today.short_forecast)

        has_night = bool(today.night_detailed_forecast)
        self._tonight_period.set_visible(has_night)
        self._tonight_text.set_visible(has_night)
        if has_night:
            self._tonight_period.set_label("Tonight")
            self._tonight_text.set_label(today.night_detailed_forecast)

    def _update_week(self, bundle: WeatherBundle) -> None:
        child = self._week_box.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self._week_box.remove(child)
            child = nxt

        days = bundle.daily[:7]
        if not days:
            return

        highs = [d.high for d in days if d.high is not None]
        lows = [d.low for d in days if d.low is not None]
        week_high = max(highs) if highs else 100.0
        week_low = min(lows) if lows else 0.0
        if week_high - week_low < 5:
            week_high, week_low = week_high + 3, week_low - 3

        now_temp = bundle.current.temperature if bundle.current else None

        for index, day in enumerate(days):
            row = DayRow(day, is_today=index == 0)
            low = day.low if day.low is not None else week_low
            high = day.high if day.high is not None else week_high
            row.bar.set_range(
                low, high, week_low, week_high,
                is_today=index == 0, now_temp=now_temp,
            )
            self._week_box.append(row)

    def _update_details(self, bundle, sun, moon, tz, now) -> None:
        current = bundle.current

        def put(key: str, value: str, caption: str = "") -> None:
            self._tiles[key].update(value, caption)

        if current:
            put("feels", _fmt_temp(current.feels_like))
            put("humidity", _fmt(current.humidity, "%"))
            put("dewpoint", _fmt_temp(current.dewpoint))
            gust = (
                f"gusting {current.wind_gust:.0f} mph"
                if current.wind_gust is not None
                else ""
            )
            if current.wind_speed is None:
                wind_value = "--"
            elif current.wind_speed < 1:
                wind_value = "Calm"
            else:
                # Some stations report a speed with no direction; showing a
                # bare "--" beside it reads as broken.
                bearing = (
                    f" {current.wind_cardinal}"
                    if current.wind_direction is not None
                    else ""
                )
                wind_value = f"{current.wind_speed:.0f} mph{bearing}"
            put("wind", wind_value, gust)
            put("pressure", _fmt(current.pressure, " inHg", digits=2))
            put("visibility", _fmt(current.visibility, " mi"))
            put("station", current.station_name or "--",
                "Nearest reporting station" if current.station_name else "")

        put("sunrise", _fmt_time(sun.sunrise, tz), f"dawn {_fmt_time(sun.dawn, tz)}")
        put("sunset", _fmt_time(sun.sunset, tz), f"dusk {_fmt_time(sun.dusk, tz)}")
        put("daylength", _duration(sun.day_length),
            f"solar noon {_fmt_time(sun.solar_noon, tz)}")
        put("moonrise", _fmt_time(moon.moonrise, tz))
        put("moonset", _fmt_time(moon.moonset, tz))

        self._moon_dial.set_phase(moon.phase)
        self._moon_name.set_label(moon.name)
        self._moon_caption.set_label(
            f"{moon.illumination * 100:.0f}% illuminated · "
            f"{moon.age_days:.1f} days old"
        )

        self._sun_arc.set_arc(
            astro.day_progress(sun, now),
            sun.is_daytime,
            _fmt_time(sun.sunrise, tz),
            _fmt_time(sun.sunset, tz),
        )


def _section_title(text: str) -> Gtk.Label:
    label = Gtk.Label(label=text, xalign=0.0)
    label.add_css_class("section-title")
    return label
