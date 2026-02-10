from __future__ import annotations

from meshcore_pathbot.web.routes.overlaps import _annotate_collision_distances


def test_annotate_collision_distances_sets_nearest_and_group_max() -> None:
    collisions = [
        {
            "prefix": "ab",
            "repeaters": [
                {"name": "A", "public_key": "a", "lat": 1.0, "lon": 1.0},
                {"name": "B", "public_key": "b", "lat": 1.0, "lon": 2.0},
                {"name": "C", "public_key": "c", "lat": 1.0, "lon": 3.0},
            ],
        }
    ]

    annotated = _annotate_collision_distances(collisions)

    group = annotated[0]
    assert group["max_distance_km"] is not None
    assert group["max_distance_km"] > 220

    nearest = {r["name"]: r["closest_distance_km"] for r in group["repeaters"]}
    assert nearest["A"] is not None
    assert nearest["B"] is not None
    assert nearest["C"] is not None
    assert abs(nearest["A"] - nearest["B"]) < 1
    assert abs(nearest["C"] - nearest["B"]) < 1


def test_annotate_collision_distances_handles_missing_locations() -> None:
    collisions = [
        {
            "prefix": "cd",
            "repeaters": [
                {"name": "Known", "public_key": "k", "lat": 1.0, "lon": 1.0},
                {"name": "Unknown", "public_key": "u", "lat": 0.0, "lon": 0.0},
            ],
        }
    ]

    annotated = _annotate_collision_distances(collisions)
    group = annotated[0]

    known = next(r for r in group["repeaters"] if r["name"] == "Known")
    unknown = next(r for r in group["repeaters"] if r["name"] == "Unknown")

    assert known["closest_distance_km"] is None
    assert unknown["closest_distance_km"] is None
    assert group["max_distance_km"] is None
