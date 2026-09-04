import pytest
from fastapi.testclient import TestClient
import src.main as main


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DB_FILE", tmp_path / "test.sqlite")
    main.init_db()
    return TestClient(main.app)


def post_outreach(client, **kw):
    body = {
        "company": "Acme",
        "role": "Implementation Specialist",
        "channel": "referral",
        "status": "sent",
    }
    body.update(kw)
    return client.post("/api/outreaches", json=body)


def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert r.json()["version"] == "0.2.0"


def test_application_board_unchanged(client):
    r = client.post(
        "/api/applications",
        json={"company": "Acme", "role": "Engineer", "status": "applied"},
    )
    assert r.status_code == 200
    data = client.get("/api/applications").json()
    assert "applied" in data["board"]
    # legacy reminder text still present
    assert (
        "Follow up with Acme in 5 business days" in data["applications"][0]["follow_up"]
    )


def test_create_outreach_defaults_follow_up_4_days(client):
    r = post_outreach(client, company="Miter")
    assert r.status_code == 200
    o = r.json()
    assert o["follow_up_on"] is not None
    # first follow-up is 4 days after sent_on (today)
    from datetime import date, timedelta

    assert o["follow_up_on"] == (date.today() + timedelta(days=4)).isoformat()
    assert o["follow_ups_done"] == 0
    assert o["due"] is False


def test_create_outreach_draft_has_no_follow_up(client):
    o = post_outreach(client, status="draft").json()
    assert o["follow_up_on"] is None


def test_create_outreach_terminal_status_has_no_follow_up(client):
    o = post_outreach(client, status="rejected").json()
    assert o["follow_up_on"] is None


def test_create_outreach_requires_company(client):
    r = post_outreach(client, company="   ")
    assert r.status_code == 422


def test_create_outreach_invalid_status(client):
    r = post_outreach(client, status="banana")
    assert r.status_code == 422


def test_create_outreach_malformed_date(client):
    r = post_outreach(client, sent_on="2026-13-99")
    assert r.status_code == 422


def test_log_follow_up_advances_cadence_and_caps_at_two(client):
    o = post_outreach(client).json()
    # first logged follow-up -> +5 days from today, done=1
    r1 = client.patch(f"/api/outreaches/{o['id']}", json={"log_follow_up": True})
    assert r1.status_code == 200
    o1 = r1.json()
    assert o1["follow_ups_done"] == 1
    from datetime import date, timedelta

    assert o1["follow_up_on"] == (date.today() + timedelta(days=5)).isoformat()
    # second logged follow-up -> done=2, cadence stops (no follow_up_on)
    o2 = client.patch(f"/api/outreaches/{o['id']}", json={"log_follow_up": True}).json()
    assert o2["follow_ups_done"] == 2
    assert o2["follow_up_on"] is None
    # third log stays capped
    o3 = client.patch(f"/api/outreaches/{o['id']}", json={"log_follow_up": True}).json()
    assert o3["follow_ups_done"] == 2
    assert o3["follow_up_on"] is None


def test_status_change_to_replied_then_screen_clears_follow_up(client):
    o = post_outreach(client).json()
    assert o["follow_up_on"] is not None
    o1 = client.patch(f"/api/outreaches/{o['id']}", json={"status": "screen"}).json()
    assert o1["follow_up_on"] is None


def test_due_filter(client):
    # an old outreach whose follow-up date has passed
    from datetime import date, timedelta

    past = (date.today() - timedelta(days=2)).isoformat()
    post_outreach(
        client,
        company="Past Co",
        sent_on=(date.today() - timedelta(days=10)).isoformat(),
    )
    # force its follow_up_on into the past via patch
    items = client.get("/api/outreaches").json()["outreaches"]
    past_id = [o for o in items if o["company"] == "Past Co"][0]["id"]
    client.patch(f"/api/outreaches/{past_id}", json={"follow_up_on": past})
    due = client.get("/api/outreaches?due=true").json()
    assert len(due["outreaches"]) == 1
    assert due["outreaches"][0]["company"] == "Past Co"
    assert due["outreaches"][0]["due"] is True
    # terminal status is never due even with a past date
    client.patch(f"/api/outreaches/{past_id}", json={"status": "dead"})
    assert client.get("/api/outreaches?due=true").json()["count"] == 0


def test_channel_filter(client):
    post_outreach(client, company="A", channel="referral")
    post_outreach(client, company="B", channel="recruiter")
    post_outreach(client, company="C", channel="referral")
    ref = client.get("/api/outreaches?channel=referral").json()
    assert ref["count"] == 2
    assert {o["company"] for o in ref["outreaches"]} == {"A", "C"}


def test_status_filter(client):
    post_outreach(client, company="A", status="sent")
    post_outreach(client, company="B", status="replied")
    r = client.get("/api/outreaches?status=replied").json()
    assert r["count"] == 1
    assert r["outreaches"][0]["company"] == "B"


def test_patch_notes_and_variant(client):
    o = post_outreach(client).json()
    r = client.patch(
        f"/api/outreaches/{o['id']}",
        json={"notes": "sent migration checklist", "variant": "B"},
    )
    assert r.status_code == 200
    assert r.json()["notes"] == "sent migration checklist"
    assert r.json()["variant"] == "B"


def test_delete_outreach(client):
    o = post_outreach(client).json()
    r = client.delete(f"/api/outreaches/{o['id']}")
    assert r.status_code == 200
    assert client.get("/api/outreaches").json()["count"] == 0


def test_patch_missing_outreach_404(client):
    r = client.patch("/api/outreaches/9999", json={"status": "replied"})
    assert r.status_code == 404


def test_dashboard_channel_stats_and_funnel(client):
    post_outreach(client, company="A", channel="referral", status="sent")
    post_outreach(client, company="B", channel="referral", status="replied")
    post_outreach(
        client, company="C", channel="referral", status="interview"
    )  # counts as reply
    post_outreach(client, company="D", channel="recruiter", status="sent")
    post_outreach(
        client, company="E", channel="cold-apply", status="draft"
    )  # draft not counted as sent
    d = client.get("/api/dashboard").json()
    assert d["total"] == 5
    assert d["by_channel"]["referral"]["sent"] == 3
    assert d["by_channel"]["referral"]["replies"] == 2
    assert d["by_channel"]["referral"]["reply_rate"] == round(2 / 3 * 100, 1)
    assert d["by_channel"]["recruiter"]["sent"] == 1
    assert d["by_channel"]["recruiter"]["replies"] == 0
    assert d["by_channel"]["recruiter"]["reply_rate"] == 0.0
    assert "cold-apply" not in d["by_channel"]  # draft-only channel dropped
    assert d["funnel"]["sent"] == 2  # status='sent' rows only (A and D)
    assert d["funnel"]["replied"] == 1
    assert d["funnel"]["interview"] == 1
    assert d["funnel"]["draft"] == 1
    assert d["sent_30d"] == 4


def test_dashboard_due_count(client):
    from datetime import date, timedelta

    past = (date.today() - timedelta(days=1)).isoformat()
    o = post_outreach(
        client, sent_on=(date.today() - timedelta(days=8)).isoformat()
    ).json()
    client.patch(f"/api/outreaches/{o['id']}", json={"follow_up_on": past})
    d = client.get("/api/dashboard").json()
    assert d["due_count"] == 1


def test_outreach_list_sorted_by_follow_up(client):
    from datetime import date, timedelta

    post_outreach(
        client, company="Late", sent_on=(date.today() - timedelta(days=9)).isoformat()
    )
    post_outreach(client, company="Now", sent_on=date.today().isoformat())
    # force distinct follow-up dates to assert ordering (nulls last)
    items = client.get("/api/outreaches").json()["outreaches"]
    ids = {o["company"]: o["id"] for o in items}
    client.patch(
        f"/api/outreaches/{ids['Now']}",
        json={"follow_up_on": (date.today() + timedelta(days=4)).isoformat()},
    )
    client.patch(
        f"/api/outreaches/{ids['Late']}",
        json={"follow_up_on": (date.today() - timedelta(days=5)).isoformat()},
    )
    ordered = [o["company"] for o in client.get("/api/outreaches").json()["outreaches"]]
    assert ordered == ["Late", "Now"]


def test_home_serves_ui(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "Log an outreach" in r.text
    assert "Due now" in r.text
    assert "Next follow-up" in r.text
    assert "api/outreaches" in r.text


def test_legacy_interview_questions_still_work(client):
    r = client.post("/api/interview-questions", json={"company": "X", "role": "CSM"})
    assert r.status_code == 200
    assert len(r.json()["questions"]) == 3
