"""Path resolution: hex prefixes to repeater names with geographic disambiguation."""

from __future__ import annotations

from .geo import has_location, haversine
from .repeater_db import RepeaterDB


class PathResolver:
    """Resolve raw hex path strings into human-readable repeater names."""

    def __init__(self, db: RepeaterDB):
        self.db = db

    def resolve(self, raw_path: str) -> str:
        """Resolve a raw hex path into friendly names.

        raw_path: e.g. "fb1f7a" -> 3 hops: fb, 1f, 7a

        Returns formatted string like:
            "Hilltop-RPT > Valley-RPT > Tower-RPT (3 hops)"
        or with ambiguity markers:
            "Hilltop-RPT? > Valley-RPT > Tower-RPT (3 hops)"
        """
        if not raw_path or len(raw_path) < 2 or len(raw_path) % 2 != 0:
            return ""

        prefixes = [raw_path[i : i + 2].lower() for i in range(0, len(raw_path), 2)]
        hop_count = len(prefixes)

        # Find all candidates for each hop
        candidates = []
        for p in prefixes:
            matches = self.db.get_by_prefix(p)
            if not matches:
                candidates.append(
                    [{"prefix": p, "name": p.upper(), "lat": 0, "lon": 0}]
                )
            else:
                candidates.append(matches)

        # Resolve ambiguous hops using geographic proximity
        resolved = []
        for i, options in enumerate(candidates):
            if len(options) == 1:
                resolved.append(options[0])
                continue

            # Gather reference points from neighbours
            ref_points = []
            if i > 0 and has_location(resolved[i - 1]):
                ref_points.append(resolved[i - 1])
            if i + 1 < len(candidates) and len(candidates[i + 1]) == 1:
                if has_location(candidates[i + 1][0]):
                    ref_points.append(candidates[i + 1][0])

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
            if len(candidates[i]) > 1:
                name += "?"
            parts.append(name)

        path_str = " \u2192 ".join(parts)
        return f"{path_str} ({hop_count} hops)"

    def resolve_detailed(self, raw_path: str) -> list[dict]:
        """Resolve path and return detailed hop information for web display."""
        if not raw_path or len(raw_path) < 2 or len(raw_path) % 2 != 0:
            return []

        prefixes = [raw_path[i : i + 2].lower() for i in range(0, len(raw_path), 2)]

        candidates = []
        for p in prefixes:
            matches = self.db.get_by_prefix(p)
            if not matches:
                candidates.append(
                    [{"prefix": p, "name": p.upper(), "lat": 0, "lon": 0, "resolved": False}]
                )
            else:
                for m in matches:
                    m["resolved"] = True
                candidates.append(matches)

        resolved = []
        for i, options in enumerate(candidates):
            if len(options) == 1:
                hop = dict(options[0])
                hop["ambiguous"] = False
                resolved.append(hop)
                continue

            ref_points = []
            if i > 0 and has_location(resolved[i - 1]):
                ref_points.append(resolved[i - 1])
            if i + 1 < len(candidates) and len(candidates[i + 1]) == 1:
                if has_location(candidates[i + 1][0]):
                    ref_points.append(candidates[i + 1][0])

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

            hop = dict(best)
            hop["ambiguous"] = True
            hop["candidates"] = len(options)
            resolved.append(hop)

        return resolved

    @staticmethod
    def raw(raw_path: str) -> str:
        """Format raw path with hop count, no name resolution."""
        if not raw_path or len(raw_path) < 2 or len(raw_path) % 2 != 0:
            return ""
        hop_count = len(raw_path) // 2
        split = ":".join(raw_path[i : i + 2] for i in range(0, len(raw_path), 2))
        return f"{split} ({hop_count} hops)"
