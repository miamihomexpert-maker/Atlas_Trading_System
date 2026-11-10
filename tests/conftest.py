import os

import pytest


# Paper sessions under pytest must not download history, headlines, or company
# facts unless a test asks for that fetch directly.
os.environ.setdefault("ATLAS_AUTO_TRAIN", "false")
os.environ.setdefault("ATLAS_COMPANY_FACTS", "false")
os.environ.setdefault("ATLAS_BEHAVIOR", "false")


@pytest.fixture(autouse=True)
def _reset_yahoo_pause():
    """A rate-limit pause is process-wide. One test must not silence the next."""
    from atlas.market_data import clear_feed_pause

    clear_feed_pause()
    yield
    clear_feed_pause()
