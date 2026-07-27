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

"""Persistence for pinned locations and app preferences.

State lives in a single JSON file under the XDG config directory, which keeps
the app installable by copy without registering a GSettings schema.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Callable

from gi.repository import GObject

from .model import Location

log = logging.getLogger(__name__)


def _config_dir() -> str:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    path = os.path.join(base, "nimbus-weather")
    os.makedirs(path, exist_ok=True)
    return path


class FavoritesStore(GObject.Object):
    """An ordered, de-duplicated list of pinned locations.

    Emits ``changed`` whenever the collection is modified so views can
    refresh without polling.
    """

    __gsignals__ = {
        "changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self) -> None:
        super().__init__()
        self._path = os.path.join(_config_dir(), "favorites.json")
        self._locations: list[Location] = []
        self._last_viewed: str = ""
        self.load()

    # -- access -----------------------------------------------------------

    @property
    def locations(self) -> list[Location]:
        return list(self._locations)

    @property
    def last_viewed(self) -> str:
        return self._last_viewed

    def __len__(self) -> int:
        return len(self._locations)

    def __iter__(self):
        return iter(self._locations)

    def contains(self, location: Location) -> bool:
        return any(item.key == location.key for item in self._locations)

    def find(self, key: str) -> Location | None:
        for item in self._locations:
            if item.key == key:
                return item
        return None

    # -- mutation ---------------------------------------------------------

    def add(self, location: Location) -> bool:
        """Pin *location*. Returns False when it was already pinned."""
        if self.contains(location):
            return False
        self._locations.append(location)
        self.save()
        self.emit("changed")
        return True

    def remove(self, location: Location) -> bool:
        before = len(self._locations)
        self._locations = [
            item for item in self._locations if item.key != location.key
        ]
        if len(self._locations) == before:
            return False
        self.save()
        self.emit("changed")
        return True

    def toggle(self, location: Location) -> bool:
        """Pin or unpin. Returns True when the location ends up pinned."""
        if self.contains(location):
            self.remove(location)
            return False
        self.add(location)
        return True

    def update(self, location: Location) -> None:
        """Replace a stored entry, e.g. once its NWS grid has been resolved."""
        changed = False
        for index, item in enumerate(self._locations):
            if item.key == location.key:
                if item != location:
                    self._locations[index] = location
                    changed = True
                break
        if changed:
            self.save()

    def move(self, location: Location, offset: int) -> None:
        """Reorder a pinned location by *offset* positions."""
        for index, item in enumerate(self._locations):
            if item.key == location.key:
                target = max(0, min(len(self._locations) - 1, index + offset))
                if target != index:
                    self._locations.insert(target, self._locations.pop(index))
                    self.save()
                    self.emit("changed")
                return

    def set_last_viewed(self, location: Location | None) -> None:
        key = location.key if location else ""
        if key != self._last_viewed:
            self._last_viewed = key
            self.save()

    # -- disk -------------------------------------------------------------

    def load(self) -> None:
        try:
            with open(self._path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            log.warning("could not read favourites: %s", exc)
            return

        raw_locations = payload.get("locations")
        if not isinstance(raw_locations, list):
            raw_locations = []

        loaded: list[Location] = []
        for entry in raw_locations:
            # A hand-edited or truncated file can hold anything at all here,
            # and a crash on load would leave the app unable to start.
            if not isinstance(entry, dict):
                log.warning("skipping malformed favourite: %r", entry)
                continue
            try:
                loaded.append(Location.from_dict(entry))
            except (TypeError, ValueError, KeyError, AttributeError):
                log.warning("skipping malformed favourite: %r", entry)
        self._locations = loaded
        self._last_viewed = payload.get("last_viewed", "")

    def save(self) -> None:
        payload = {
            "version": 1,
            "locations": [item.to_dict() for item in self._locations],
            "last_viewed": self._last_viewed,
        }
        try:
            tmp = f"{self._path}.tmp"
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2)
            os.replace(tmp, self._path)
        except OSError as exc:
            log.warning("could not save favourites: %s", exc)
