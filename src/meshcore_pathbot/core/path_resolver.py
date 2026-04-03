"""Path resolution: hex prefixes to repeater names with geographic disambiguation."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from .geo import has_location, haversine
from .repeater_db import RepeaterDB

if TYPE_CHECKING:
    from ..config.schema import AppConfig


class PathResolver:
    """Resolve raw hex path strings into human-readable repeater names."""

    def __init__(self, db: RepeaterDB, config: AppConfig | None = None):
        self.db = db
        self._config = config

    def _is_home_repeater(self, prefix: str, index: int, total_hops: int) -> bool:
        """Check if a hop should be resolved as the home repeater.

        Returns True when the prefix matches the configured home repeater
        and the hop is within the last 2 positions of the path.
        """
        if not self._config:
            return False
        hr_prefix = self._config.bot.home_repeater_prefix.lower()
        if not hr_prefix or not self._config.bot.home_repeater_name:
            return False
        return prefix.lower() == hr_prefix and index >= total_hops - 2

    def _bot_location(self) -> dict | None:
        """Return a ref-point dict for the bot's configured location, or None."""
        if not self._config:
            return None
        lat, lon = self._config.bot.lat, self._config.bot.lon
        if lat == 0.0 and lon == 0.0:
            return None
        return {"lat": lat, "lon": lon}

    def _home_repeater_entry(self, prefix: str, index: int, total_hops: int) -> dict:
        """Build the synthetic home repeater entry used for configured home hops."""
        home_entry = {
            "prefix": prefix,
            "name": self._config.bot.home_repeater_name,
            "lat": 0,
            "lon": 0,
        }
        # For the final hop, the home repeater should anchor to bot location.
        if index == total_hops - 1:
            bot_loc = self._bot_location()
            if bot_loc:
                home_entry["lat"] = bot_loc["lat"]
                home_entry["lon"] = bot_loc["lon"]
        return home_entry

    @staticmethod
    def normalize_path(raw: str) -> str:
        """Normalize a path string to a plain hex string.

        Accepts multiple formats:
            "fb1f7a"       -> "fb1f7a"   (already clean)
            "fb:1f:7a"     -> "fb1f7a"   (colon-separated)
            "fb,1f,7a"     -> "fb1f7a"   (comma-separated)
            "FB 1F 7A"     -> "fb1f7a"   (space-separated)
            "FB:1F:7A"     -> "fb1f7a"   (uppercase)
        """
        # Strip separators and whitespace, lowercase
        cleaned = re.sub(r"[:\s,]+", "", raw).strip().lower()
        return cleaned

    def resolve(self, raw_path: str, preferred_repeaters: dict[str, str] | None = None) -> str:
        """Resolve a raw hex path into friendly names.

        raw_path: e.g. "fb1f7a" or "fb:1f:7a" or "fb,1f,7a"

        Returns formatted string like:
            "Hilltop-RPT > Valley-RPT > Tower-RPT (3 hops)"
        or with ambiguity markers:
            "Hilltop-RPT? > Valley-RPT > Tower-RPT (3 hops)"
        """
        raw_path = self.normalize_path(raw_path)
        if not raw_path or len(raw_path) < 2 or len(raw_path) % 2 != 0:
            return ""

        prefixes = [raw_path[i : i + 2].lower() for i in range(0, len(raw_path), 2)]
        hop_count = len(prefixes)

        # Find all candidates for each hop
        candidates = []
        home_hits: set[int] = set()
        for idx, p in enumerate(prefixes):
            if self._is_home_repeater(p, idx, hop_count):
                candidates.append([self._home_repeater_entry(p, idx, hop_count)])
                home_hits.add(idx)
            else:
                matches = self.db.get_by_prefix(p)
                if not matches:
                    candidates.append(
                        [{"prefix": p, "name": p.upper(), "lat": 0, "lon": 0}]
                    )
                else:
                    candidates.append(matches)

        preferred_repeaters = preferred_repeaters or {}

        # Resolve ambiguous hops using explicit choices, then geographic proximity
        bot_loc = self._bot_location()
        resolved = []
        manual_hits: set[int] = set()
        for i, options in enumerate(candidates):
            if len(options) == 1:
                resolved.append(options[0])
                continue

            preferred_public_key = preferred_repeaters.get(prefixes[i], "")
            preferred = next(
                (o for o in options if o.get("public_key", "") == preferred_public_key),
                None,
            )
            if preferred:
                manual_hits.add(i)
                resolved.append(preferred)
                continue

            # Gather reference points from neighbours
            ref_points = []
            if i > 0 and has_location(resolved[i - 1]):
                ref_points.append(resolved[i - 1])
            if i + 1 < len(candidates) and len(candidates[i + 1]) == 1:
                if has_location(candidates[i + 1][0]):
                    ref_points.append(candidates[i + 1][0])
            # Bot location acts as the implicit endpoint after the last hop
            if i == len(candidates) - 1 and bot_loc:
                ref_points.append(bot_loc)

            if not ref_points:
                best = max(options, key=lambda o: o.get("last_seen", 0))
                resolved.append(best)
                continue

            def score(node: dict) -> float:
                if not has_location(node):
                    return float("inf")
                return sum(
                    haversine(node["lat"], node["lon"], rp["lat"], rp["lon"])
                    for rp in ref_points
                ) / len(ref_points)

            best = min(options, key=score)
            resolved.append(best)

        # Format output
        parts = []
        for i, node in enumerate(resolved):
            name = node["name"]
            if i not in home_hits and i not in manual_hits and len(candidates[i]) > 1:
                name += "?"
            parts.append(name)

        path_str = " \u2192 ".join(parts)
        return f"{path_str} ({hop_count} hops)"

    def resolve_detailed(
        self,
        raw_path: str,
        preferred_repeaters: dict[str, str] | None = None,
    ) -> list[dict]:
        """Resolve path and return detailed hop information for web display."""
        raw_path = self.normalize_path(raw_path)
        if not raw_path or len(raw_path) < 2 or len(raw_path) % 2 != 0:
            return []

        prefixes = [raw_path[i : i + 2].lower() for i in range(0, len(raw_path), 2)]
        hop_count = len(prefixes)

        candidates = []
        home_hits: set[int] = set()
        for idx, p in enumerate(prefixes):
            if self._is_home_repeater(p, idx, hop_count):
                candidates.append(
                    [{**self._home_repeater_entry(p, idx, hop_count), "resolved": True}]
                )
                home_hits.add(idx)
            else:
                matches = self.db.get_by_prefix(p)
                if not matches:
                    candidates.append(
                        [{"prefix": p, "name": p.upper(), "lat": 0, "lon": 0, "resolved": False}]
                    )
                else:
                    candidates.append([
                        {**m, "resolved": True}
                        for m in matches
                    ])

        preferred_repeaters = preferred_repeaters or {}
        bot_loc = self._bot_location()
        resolved = []
        for i, options in enumerate(candidates):
            if len(options) == 1:
                hop = dict(options[0])
                hop["ambiguous"] = False
                resolved.append(hop)
                continue

            preferred_public_key = preferred_repeaters.get(prefixes[i], "")
            preferred = next(
                (o for o in options if o.get("public_key", "") == preferred_public_key),
                None,
            )
            if preferred:
                best = preferred
                selected_by = "manual"
            else:
                selected_by = "distance"

                ref_points = []
                if i > 0 and has_location(resolved[i - 1]):
                    ref_points.append(resolved[i - 1])
                if i + 1 < len(candidates) and len(candidates[i + 1]) == 1:
                    if has_location(candidates[i + 1][0]):
                        ref_points.append(candidates[i + 1][0])
                if i == len(candidates) - 1 and bot_loc:
                    ref_points.append(bot_loc)

                if not ref_points:
                    selected_by = "last_seen"
                    best = max(options, key=lambda o: o.get("last_seen", 0))
                else:

                    def score(node: dict) -> float:
                        if not has_location(node):
                            return float("inf")
                        return sum(
                            haversine(node["lat"], node["lon"], rp["lat"], rp["lon"])
                            for rp in ref_points
                        ) / len(ref_points)

                    best = min(options, key=score)

            hop = dict(best)
            hop["ambiguous"] = i not in home_hits and selected_by != "manual"
            hop["selected_by"] = selected_by
            hop["candidates"] = len(options)
            if len(options) > 1:
                hop["options"] = sorted(
                    [
                        {
                            **option,
                            "selected": option.get("public_key", "") == hop.get("public_key", ""),
                        }
                        for option in options
                    ],
                    key=lambda option: option.get("name", ""),
                )
            resolved.append(hop)

        # Calculate hop-to-hop distances
        for i in range(len(resolved)):
            if i == 0:
                resolved[i]["distance_from_prev"] = None
            else:
                prev = resolved[i - 1]
                curr = resolved[i]
                if has_location(prev) and has_location(curr):
                    resolved[i]["distance_from_prev"] = round(
                        haversine(prev["lat"], prev["lon"], curr["lat"], curr["lon"]), 1
                    )
                else:
                    resolved[i]["distance_from_prev"] = None

        return resolved

    @classmethod
    def raw(cls, raw_path: str) -> str:
        """Format raw path with hop count, no name resolution."""
        raw_path = cls.normalize_path(raw_path)
        if not raw_path or len(raw_path) < 2 or len(raw_path) % 2 != 0:
            return ""
        hop_count = len(raw_path) // 2
        split = ":".join(raw_path[i : i + 2] for i in range(0, len(raw_path), 2))
        return f"{split} ({hop_count} hops)"

    def lookup_prefixes(self, raw_path: str) -> str:
        """Look up repeater names for hex prefixes using best-guess disambiguation.

        Like resolve() but designed for the 'prefix' channel command.
        Returns a compact string: "fb=Hilltop, 1f=Valley, 7a=Tower"
        Unknown prefixes shown as "xx=?"

        Handles multibyte paths (e.g. "fb1f:7ab2" from a 2-byte-hash trace reply)
        by using the full segment as the lookup key so more bytes eliminate collisions.
        Output labels each hop with its full segment: "fb1f=Hilltop, 7ab2=Valley"
        """
        # Detect multibyte paths before stripping separators.
        # If all colon/space/comma-separated segments are valid even-length hex
        # longer than 2 chars, treat each segment as a full multibyte hop key.
        segments = [s.strip().lower() for s in re.split(r"[:\s,]+", raw_path.strip()) if s.strip()]
        if (
            len(segments) > 1
            and all(re.fullmatch(r"[0-9a-f]+", s) and len(s) > 2 and len(s) % 2 == 0 for s in segments)
        ):
            prefixes = segments  # full segments, e.g. "fb1f" for 2-byte hashes
        else:
            raw_path = self.normalize_path(raw_path)
            if not raw_path or len(raw_path) < 2 or len(raw_path) % 2 != 0:
                return ""
            prefixes = [raw_path[i : i + 2].lower() for i in range(0, len(raw_path), 2)]

        hop_count = len(prefixes)

        # Find all candidates for each prefix
        candidates = []
        home_hits: set[int] = set()
        for idx, p in enumerate(prefixes):
            if self._is_home_repeater(p, idx, hop_count):
                candidates.append([self._home_repeater_entry(p, idx, hop_count)])
                home_hits.add(idx)
            else:
                matches = self.db.get_by_prefix(p)
                if not matches:
                    candidates.append([{"prefix": p, "name": "?", "lat": 0, "lon": 0}])
                else:
                    candidates.append(matches)

        # Disambiguate using geographic proximity (same logic as resolve)
        bot_loc = self._bot_location()
        resolved = []
        for i, options in enumerate(candidates):
            if len(options) == 1:
                resolved.append(options[0])
                continue

            ref_points = []
            if i > 0 and has_location(resolved[i - 1]):
                ref_points.append(resolved[i - 1])
            if i + 1 < len(candidates) and len(candidates[i + 1]) == 1:
                if has_location(candidates[i + 1][0]):
                    ref_points.append(candidates[i + 1][0])
            if i == len(candidates) - 1 and bot_loc:
                ref_points.append(bot_loc)

            if not ref_points:
                best = max(options, key=lambda o: o.get("last_seen", 0))
            else:

                def score(node: dict) -> float:
                    if not has_location(node):
                        return float("inf")
                    return sum(
                        haversine(node["lat"], node["lon"], rp["lat"], rp["lon"])
                        for rp in ref_points
                    ) / len(ref_points)

                best = min(options, key=score)

            resolved.append(best)

        # Format: "fb=Hilltop, 1f=Valley, 7a=?"
        parts = []
        manual_hits: set[int] = set()
        for i, node in enumerate(resolved):
            prefix = prefixes[i]
            name = node["name"]
            if i not in home_hits and i not in manual_hits and len(candidates[i]) > 1:
                name += "?"
            parts.append(f"{prefix}={name}")

        return ", ".join(parts)
