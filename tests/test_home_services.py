"""Tests for the Home page's service cards: which there are, and in what order."""

import re


def card_names(body):
    services = body.split('<div class="svc-grid">')[-1].split("</section>")[0]
    return re.findall(r'<div class="svc-name">([^<]*)</div>', services)


def test_the_cards_run_from_the_live_views_to_the_controller(app_client):
    """Controller is last: it is the way into everything the others do not
    cover. Demo mode, because the cards are only drawn once the radar is
    installed."""
    body = app_client.get("/?demo=1").data.decode()
    assert card_names(body) == ["Passive Radar", "Max-Hold", "ADS-B Map", "Tracker", "Controller"]


def test_the_tracker_card_says_how_much_it_keeps(app_client):
    body = app_client.get("/?demo=1").data.decode()
    tracker = body.split('<div class="svc-name">Tracker</div>')[1].split("</a>")[0]
    assert "up to 4 hours" in tracker
