"""Xweather (Vaisala) confirmation tier — opt-in, user-supplied credentials,
quota-governed; called only at decision moments (DESIGN.md §4.3).
"""


class XweatherConfirmer:
    """Confirms Blitzortung strike distance when budget allows."""

    def confirm(self, lat: float, lon: float) -> float | None:
        """Return confirmed distance_km, or None if disabled/over budget."""
        raise NotImplementedError("Phase 4")
