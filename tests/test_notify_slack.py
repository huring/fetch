import responses

from watcher.notify import slack

WEBHOOK = "https://hooks.slack.com/services/T000/B000/XXXX"


@responses.activate
def test_send_instant_posts_formatted_text():
    responses.add(responses.POST, WEBHOOK, json={"ok": True}, status=200)

    slack.send_instant(WEBHOOK, "Vardagsrummet AV-receiver", "Onkyo TX-NR656", 2000, "https://x/1", 9, "great match", "good price")

    sent = responses.calls[0].request
    assert "TX-NR656" in sent.body.decode()
    assert "Score 9/10" in sent.body.decode()


def test_send_instant_dry_run_does_not_post():
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        slack.send_instant(WEBHOOK, "C", "T", 100, "u", 8, "r", "p", dry_run=True)
        assert len(rsps.calls) == 0


@responses.activate
def test_send_digest_groups_by_search():
    responses.add(responses.POST, WEBHOOK, json={"ok": True}, status=200)

    entries = {
        "Stugan hifi": [("Leak Stereo 130", 1500, "https://x/2", 6, "nice retro amp")],
        "Vardagsrummet speakers": [("Dali Oberon 3", 4000, "https://x/3", 7, "clear upgrade")],
    }
    slack.send_digest(WEBHOOK, entries)

    body = responses.calls[0].request.body.decode()
    assert "Stugan hifi" in body
    assert "Vardagsrummet speakers" in body
    assert "Leak Stereo 130" in body


def test_send_digest_skips_when_empty():
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        slack.send_digest(WEBHOOK, {})
        assert len(rsps.calls) == 0


@responses.activate
def test_send_digest_includes_plain_entries_without_a_score():
    responses.add(responses.POST, WEBHOOK, json={"ok": True}, status=200)

    plain_entries = {"Vinyl hunt": [("Kind of Blue (LP)", 250, "https://x/4", None)]}
    slack.send_digest(WEBHOOK, {}, plain_entries)

    body = responses.calls[0].request.body.decode()
    assert "Vinyl hunt" in body
    assert "Kind of Blue (LP)" in body
    assert "/10" not in body  # no score for a plain entry


def test_send_digest_sends_when_only_plain_entries_present():
    with responses.RequestsMock() as rsps:
        rsps.add(responses.POST, WEBHOOK, json={"ok": True}, status=200)
        slack.send_digest(WEBHOOK, {}, {"Vinyl hunt": [("Kind of Blue (LP)", 250, "https://x/4", None)]})
        assert len(rsps.calls) == 1


@responses.activate
def test_send_plain_instant_posts_formatted_text():
    responses.add(responses.POST, WEBHOOK, json={"ok": True}, status=200)

    slack.send_plain_instant(WEBHOOK, "Vinyl hunt", "Kind of Blue (LP)", 250, "https://x/4")

    body = responses.calls[0].request.body.decode()
    assert "Kind of Blue (LP)" in body
    assert "Price alert" in body
    assert "Vinyl hunt" in body


def test_auction_suffix_none_when_no_ends_at():
    assert slack._auction_suffix(None) == ""


def test_auction_suffix_ended_when_in_the_past():
    import datetime
    past = (datetime.datetime.utcnow() - datetime.timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
    assert slack._auction_suffix(past) == " (auction ended)"


def test_auction_suffix_days_when_far_in_the_future():
    # A small buffer keeps this away from the floor-rounding edge - formatting
    # to second precision and the brief delay before _auction_suffix's own
    # utcnow() call both shave a little off the raw delta.
    import datetime
    future = (datetime.datetime.utcnow() + datetime.timedelta(days=3, minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
    assert slack._auction_suffix(future) == " (auction ends in 3d)"


def test_auction_suffix_hours_when_under_a_day():
    import datetime
    future = (datetime.datetime.utcnow() + datetime.timedelta(hours=5, minutes=2)).strftime("%Y-%m-%d %H:%M:%S")
    assert slack._auction_suffix(future) == " (auction ends in 5h)"


@responses.activate
def test_send_plain_instant_includes_auction_suffix():
    import datetime
    responses.add(responses.POST, WEBHOOK, json={"ok": True}, status=200)
    future = (datetime.datetime.utcnow() + datetime.timedelta(days=2, minutes=5)).strftime("%Y-%m-%d %H:%M:%S")

    slack.send_plain_instant(WEBHOOK, "Auction hunt", "Tandberg Receiver", 1500, "https://x/6", auction_ends_at=future)

    body = responses.calls[0].request.body.decode()
    assert "auction ends in 2d" in body


@responses.activate
def test_format_digest_plain_entry_includes_auction_suffix():
    import datetime
    responses.add(responses.POST, WEBHOOK, json={"ok": True}, status=200)
    future = (datetime.datetime.utcnow() + datetime.timedelta(days=1, hours=1, minutes=5)).strftime("%Y-%m-%d %H:%M:%S")

    plain_entries = {"Auction hunt": [("Tandberg Receiver", 1500, "https://x/6", future)]}
    slack.send_digest(WEBHOOK, {}, plain_entries)

    body = responses.calls[0].request.body.decode()
    assert "auction ends in 1d" in body


def test_send_plain_instant_dry_run_does_not_post():
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        slack.send_plain_instant(WEBHOOK, "Vinyl hunt", "Kind of Blue (LP)", 250, "https://x/4", dry_run=True)
        assert len(rsps.calls) == 0


@responses.activate
def test_send_health_alert():
    responses.add(responses.POST, WEBHOOK, json={"ok": True}, status=200)

    slack.send_health_alert(WEBHOOK, "blocket", 3, "timeout")

    body = responses.calls[0].request.body.decode()
    assert "blocket" in body
    assert "3" in body


@responses.activate
def test_send_price_watch_instant_posts_formatted_text():
    responses.add(responses.POST, WEBHOOK, json={"ok": True}, status=200)

    slack.send_price_watch_instant(WEBHOOK, "VU meter", "VU Meter Pro", 900, "https://x/5", 1000)

    body = responses.calls[0].request.body.decode()
    assert "VU meter" in body
    assert "VU Meter Pro" in body
    assert "900 SEK" in body
    assert "target: 1000 SEK" in body


def test_send_price_watch_instant_dry_run_does_not_post():
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        slack.send_price_watch_instant(WEBHOOK, "VU meter", "VU Meter Pro", 900, "https://x/5", 1000, dry_run=True)
        assert len(rsps.calls) == 0


@responses.activate
def test_send_instant_raises_on_non_2xx():
    responses.add(responses.POST, WEBHOOK, body="invalid_payload", status=400)

    try:
        slack.send_instant(WEBHOOK, "C", "T", 100, "u", 8, "r", "p")
        assert False, "expected NotifyError"
    except slack.NotifyError:
        pass
