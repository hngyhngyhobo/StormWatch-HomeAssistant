"""Persistent monthly quota governor for Xweather calls (DESIGN.md §4.3).

Counter survives restarts in /config/quota.json and resets on the 1st.
On ceiling hit: fall back to Blitzortung, publish state, never stop working.
"""


class QuotaGovernor:
    """Tracks and limits metered API calls per calendar month."""

    def allow_call(self) -> bool:
        """Return True if budget and minimum-interval rules permit a call."""
        raise NotImplementedError("Phase 4")

    def record_call(self) -> None:
        """Persist one consumed call."""
        raise NotImplementedError("Phase 4")
