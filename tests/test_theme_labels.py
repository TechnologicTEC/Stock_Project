"""app/_theme.move_label: what a quote's day change covers."""
from datetime import datetime, timezone

from app import _theme


def test_a_day_change_is_today_only_while_the_us_market_is_open():
    open_ = datetime(2026, 11, 2, 15, 0, tzinfo=timezone.utc)        # Mon 10:00 New York
    weekend = datetime(2026, 11, 1, 22, 50, tzinfo=timezone.utc)     # Sun; NZ Monday morning
    overnight = datetime(2026, 11, 3, 2, 0, tzinfo=timezone.utc)     # Mon 21:00 New York
    assert _theme.move_label(open_) == "Today"
    assert _theme.move_label(weekend) == "Last session"
    assert _theme.move_label(overnight) == "Last session"
