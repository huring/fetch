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
def test_send_health_alert():
    responses.add(responses.POST, WEBHOOK, json={"ok": True}, status=200)

    slack.send_health_alert(WEBHOOK, "blocket", 3, "timeout")

    body = responses.calls[0].request.body.decode()
    assert "blocket" in body
    assert "3" in body


@responses.activate
def test_send_instant_raises_on_non_2xx():
    responses.add(responses.POST, WEBHOOK, body="invalid_payload", status=400)

    try:
        slack.send_instant(WEBHOOK, "C", "T", 100, "u", 8, "r", "p")
        assert False, "expected NotifyError"
    except slack.NotifyError:
        pass
