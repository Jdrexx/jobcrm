from __future__ import annotations

import json
import re
import sqlite3
from collections import defaultdict
from collections.abc import Iterator
from contextlib import asynccontextmanager, contextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

APP_NAME = "Job Application CRM"
APP_VERSION = "0.2.0"
DB_FILE = Path(__file__).resolve().parent.parent / "data" / "app.sqlite"


# Application errors mapped to HTTP responses. Scoped types (not the base
# ValueError/KeyError) so an internal fault - a corrupt legacy payload row, a
# stray KeyError - surfaces as a 500 in the logs instead of masquerading as a
# 422/404 with internals in the body (CWE-209).
class Invalid(ValueError):
    """Caller-supplied data failed validation (422)."""


class NotFound(Exception):
    """Requested entity does not exist (404)."""


# Outreach lifecycle stages (application kanban statuses are separate and untouched).
OUTREACH_STATUSES = (
    "draft",
    "sent",
    "replied",
    "screen",
    "interview",
    "offer",
    "rejected",
    "dead",
)
# Statuses that mean a human responded - counted as "replies" in channel stats.
REPLIED_SET = {"replied", "screen", "interview", "offer"}
# Statuses that end the automated follow-up cadence (terminal or conversation-started).
NO_FOLLOWUP_SET = {"screen", "interview", "offer", "rejected", "dead", "draft"}
# Auto cadence: first follow-up 4 days after send, then 5 days after each logged follow-up, max 2.
FOLLOWUP_DAYS_FIRST = 4
FOLLOWUP_DAYS_NEXT = 5
FOLLOWUP_MAX_ROUNDS = 2


@contextmanager
def db() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(DB_FILE, timeout=30)
    try:
        conn.row_factory = sqlite3.Row
        with conn:
            yield conn
    finally:
        conn.close()


def init_db() -> None:
    DB_FILE.parent.mkdir(exist_ok=True)
    with db() as conn:
        # WAL is persistent in the database header once set here; per-request
        # connections inherit it, so the pragma runs exactly once per file.
        conn.execute("pragma journal_mode=wal")
        conn.execute(
            "create table if not exists records (id integer primary key autoincrement, kind text not null, title text not null, payload text not null, created_at text not null)"
        )
        conn.execute("""create table if not exists outreaches (
            id integer primary key autoincrement,
            company text not null,
            role text not null default '',
            channel text not null default 'cold-apply',
            contact_name text not null default '',
            contact_title text not null default '',
            contact_email text not null default '',
            variant text not null default '',
            status text not null default 'sent',
            sent_on text not null,
            follow_up_on text,
            follow_ups_done integer not null default 0,
            notes text not null default '',
            created_at text not null
        )""")
        conn.execute(
            "create index if not exists idx_outreaches_followup on outreaches (follow_up_on, status)"
        )
        conn.execute(
            "create index if not exists idx_outreaches_channel on outreaches (channel)"
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


# CORS middleware is intentionally absent: the UI is served same-origin by this
# app. If a public deployment ever serves the frontend from another origin, pin
# explicit origins (never "*") and land auth first (see README roadmap).
app = FastAPI(title=APP_NAME, version=APP_VERSION, lifespan=lifespan)


@app.exception_handler(Invalid)
def invalid_handler(request, exc: Invalid):
    return JSONResponse(status_code=422, content={"detail": str(exc)})


@app.exception_handler(NotFound)
def not_found_handler(request, exc: NotFound):
    return JSONResponse(
        status_code=404, content={"detail": f"not found: {exc.args[0]}"}
    )


def save_record(kind: str, title: str, payload: str) -> int:
    with db() as conn:
        cur = conn.execute(
            "insert into records(kind,title,payload,created_at) values (?,?,?,?)",
            (kind, title, payload, datetime.now(UTC).isoformat()),
        )
        return int(cur.lastrowid)


def rows(kind: str | None = None) -> list[dict[str, Any]]:
    with db() as conn:
        data = (
            conn.execute(
                "select * from records where kind=? order by id desc", (kind,)
            ).fetchall()
            if kind
            else conn.execute("select * from records order by id desc").fetchall()
        )
    return [dict(r) for r in data]


def today_iso() -> str:
    """Local calendar day (the day the user is in), canonical YYYY-MM-DD."""
    return datetime.now().astimezone().date().isoformat()


def normalize_iso_date(value: str) -> str:
    """Require a strict YYYY-MM-DD and return it canonical; Invalid otherwise."""
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise Invalid("date must be YYYY-MM-DD")
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        # matched the shape but not a real calendar date (e.g. 2026-13-99)
        raise Invalid("date must be a valid calendar date") from None


def add_days(iso: str, days: int) -> str:
    return (date.fromisoformat(iso) + timedelta(days=days)).isoformat()


def outreach_row(r: sqlite3.Row) -> dict[str, Any]:
    d = dict(r)
    d["due"] = bool(
        d.get("follow_up_on")
        and d["follow_up_on"] <= today_iso()
        and d["status"] in ("sent", "replied")
    )
    return d


# ---------------------------------------------------------------- applications (unchanged)
@app.get("/api/health")
def health():
    with db() as conn:
        records = conn.execute("select count(*) from records").fetchone()[0]
        outreaches = conn.execute("select count(*) from outreaches").fetchone()[0]
    return {
        "ok": True,
        "app": APP_NAME,
        "version": APP_VERSION,
        "records": records,
        "outreaches": outreaches,
    }


class ApplicationRequest(BaseModel):
    company: str
    role: str
    status: str = "saved"
    link: str = ""
    contact: str = ""
    notes: str = ""


@app.post("/api/applications")
def create_application(req: ApplicationRequest):
    payload = req.model_dump()
    payload["follow_up"] = (
        f"Follow up with {req.company} in 5 business days"
        if req.status in ["applied", "interview"]
        else "Apply or qualify this lead"
    )
    app_id = save_record(
        "application", f"{req.company} - {req.role}", json.dumps(payload)
    )
    return {"id": app_id, **payload}


@app.get("/api/applications")
def applications():
    items = [
        json.loads(r["payload"]) | {"id": r["id"], "created_at": r["created_at"]}
        for r in rows("application")
    ]
    board = defaultdict(list)
    for item in items:
        board[item.get("status", "saved")].append(item)
    return {"applications": items, "board": dict(board)}


@app.post("/api/interview-questions")
def interview_questions(req: ApplicationRequest):
    return {
        "questions": [
            f"How would you approach the first 30 days as a {req.role}?",
            f"What experience do you have that maps to {req.company}'s needs?",
            "Describe a time you automated or improved a workflow.",
        ]
    }


# ---------------------------------------------------------------- outreach layer (new in 0.2.0)
class OutreachRequest(BaseModel):
    company: str = Field(min_length=1, max_length=200)
    role: str = Field(default="", max_length=200)
    # Lowercase-hyphen pattern doubles as the XSS guard: it can never carry a
    # character that would break out of an HTML attribute if channel is ever
    # rendered into one (CWE-79/83).
    channel: str = Field(
        default="cold-apply", max_length=40, pattern=r"^[a-z0-9][a-z0-9-]*$"
    )
    contact_name: str = Field(default="", max_length=200)
    contact_title: str = Field(default="", max_length=200)
    contact_email: str = Field(default="", max_length=320)
    variant: str = Field(default="", max_length=80)
    status: str = "sent"
    sent_on: str | None = None
    notes: str = Field(default="", max_length=10_000)


def validate_outreach(req: OutreachRequest) -> str:
    if not req.company.strip():
        raise Invalid("company is required")
    if req.status not in OUTREACH_STATUSES:
        raise Invalid(f"status must be one of {', '.join(OUTREACH_STATUSES)}")
    sent_on = normalize_iso_date(req.sent_on or today_iso())
    if sent_on > today_iso():
        # a future-dated send would sit in the 30-day window forever
        raise Invalid("sent_on cannot be in the future")
    return sent_on


def next_follow_up(status: str, sent_on: str, done: int) -> str | None:
    """Auto cadence: send -> +4d; each logged follow-up -> +5d; cap 2 rounds; terminal/conversation states clear."""
    if status in NO_FOLLOWUP_SET or done >= FOLLOWUP_MAX_ROUNDS:
        return None
    return add_days(sent_on, FOLLOWUP_DAYS_FIRST if done == 0 else FOLLOWUP_DAYS_NEXT)


def rows_outreaches() -> list[dict[str, Any]]:
    with db() as conn:
        data = conn.execute(
            "select * from outreaches order by follow_up_on is null, follow_up_on asc, id desc"
        ).fetchall()
    return [outreach_row(r) for r in data]


@app.post("/api/outreaches")
def create_outreach(req: OutreachRequest):
    sent_on = validate_outreach(req)  # also defaults/normalizes the date
    follow_up_on = next_follow_up(req.status, sent_on, 0)
    with db() as conn:
        cur = conn.execute(
            "insert into outreaches (company, role, channel, contact_name, contact_title, contact_email, variant, status, sent_on, follow_up_on, follow_ups_done, notes, created_at) values (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                req.company.strip(),
                req.role.strip(),
                req.channel.strip(),
                req.contact_name.strip(),
                req.contact_title.strip(),
                req.contact_email.strip(),
                req.variant.strip(),
                req.status,
                sent_on,
                follow_up_on,
                0,
                req.notes.strip(),
                datetime.now(UTC).isoformat(),
            ),
        )
        oid = int(cur.lastrowid)
    return get_outreach(oid)


def get_outreach(oid: int) -> dict[str, Any]:
    with db() as conn:
        row = conn.execute("select * from outreaches where id=?", (oid,)).fetchone()
    if row is None:
        raise NotFound(oid)
    return outreach_row(row)


class OutreachPatch(BaseModel):
    status: str | None = None
    follow_up_on: str | None = None
    notes: str | None = None
    variant: str | None = None
    log_follow_up: bool = False


@app.patch("/api/outreaches/{oid}")
def patch_outreach(oid: int, req: OutreachPatch):
    if req.status is not None and req.status not in OUTREACH_STATUSES:
        raise Invalid(f"status must be one of {', '.join(OUTREACH_STATUSES)}")
    manual_date = (
        normalize_iso_date(req.follow_up_on) if req.follow_up_on is not None else None
    )
    # Read + write share one transaction, and the UPDATE reports how many rows it
    # changed, so a row deleted between the read and the write raises 404 instead
    # of silently succeeding into a phantom response.
    with db() as conn:
        row = conn.execute("select * from outreaches where id=?", (oid,)).fetchone()
        if row is None:
            raise NotFound(oid)
        current = dict(row)
        status = req.status if req.status is not None else current["status"]
        notes = req.notes if req.notes is not None else current["notes"]
        variant = req.variant if req.variant is not None else current["variant"]
        log = int(req.log_follow_up and status in ("sent", "replied"))
        terminal = int(status in NO_FOLLOWUP_SET)
        # One atomic statement: the cadence increment is computed from the row's
        # live value at write time, so concurrent "log follow-up" calls cannot
        # lose an increment to a stale read-then-write.
        cur = conn.execute(
            """
            update outreaches set
                status = :status,
                notes = :notes,
                variant = :variant,
                follow_ups_done = min(follow_ups_done + :log, :cap),
                follow_up_on = case
                    when :terminal then null
                    when :manual is not null then :manual
                    when :log and min(follow_ups_done + :log, :cap) >= :cap then null
                    when :log then :cadence_date
                    else follow_up_on
                end
            where id = :oid
            """,
            {
                "status": status,
                "notes": notes,
                "variant": variant,
                "log": log,
                "cap": FOLLOWUP_MAX_ROUNDS,
                "terminal": terminal,
                "manual": manual_date,
                "cadence_date": add_days(today_iso(), FOLLOWUP_DAYS_NEXT),
                "oid": oid,
            },
        )
        if cur.rowcount == 0:
            raise NotFound(oid)
    # follow_ups_done / follow_up_on were computed inside SQL; re-read to return
    # the canonical row rather than reconstructing the CASE logic in Python.
    return get_outreach(oid)


@app.delete("/api/outreaches/{oid}")
def delete_outreach(oid: int):
    with db() as conn:
        conn.execute("delete from outreaches where id=?", (oid,))
    return {"ok": True, "deleted": oid}


@app.get("/api/outreaches")
def list_outreaches(
    channel: str | None = None, status: str | None = None, due: bool = False
):
    items = rows_outreaches()
    if channel:
        items = [o for o in items if o["channel"] == channel]
    if status:
        items = [o for o in items if o["status"] == status]
    if due:
        items = [o for o in items if o["due"]]
    return {"outreaches": items, "count": len(items)}


@app.get("/api/dashboard")
def dashboard():
    items = rows_outreaches()
    by_channel: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"sent": 0, "replies": 0, "reply_rate": 0.0}
    )
    funnel = {s: 0 for s in OUTREACH_STATUSES}
    sent_30d = 0
    cutoff = add_days(today_iso(), -30)
    due = 0
    for o in items:
        ch = by_channel[o["channel"] or "other"]
        if o["status"] != "draft":
            ch["sent"] += 1
            if o["sent_on"] >= cutoff:
                sent_30d += 1
        if o["status"] in REPLIED_SET:
            ch["replies"] += 1
        # tolerate legacy/hand-edited statuses instead of raising (they used to
        # surface as a misleading 404 via the old blanket KeyError handler)
        if o["status"] in funnel:
            funnel[o["status"]] += 1
        if o["due"]:
            due += 1
    for ch in by_channel.values():
        ch["reply_rate"] = (
            round(ch["replies"] / ch["sent"] * 100, 1) if ch["sent"] else 0.0
        )
    # drop channels with zero sends (draft-only) so the UI never shows 0% noise
    by_channel = {k: v for k, v in by_channel.items() if v["sent"] > 0}
    return {
        "by_channel": dict(by_channel),
        "funnel": funnel,
        "due_count": due,
        "sent_30d": sent_30d,
        "total": len(items),
    }


# ---------------------------------------------------------------- browser UI
@app.get("/", response_class=HTMLResponse)
def home():
    return INDEX_HTML


INDEX_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Job Application CRM</title>
<style>
body{font-family:Inter,Arial,sans-serif;background:#0f172a;color:#e5e7eb;margin:0}
main{max-width:1080px;margin:auto;padding:24px}
h1{font-size:30px;margin:0 0 4px}h2{font-size:17px;margin:0 0 10px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin:14px 0}
.stat{background:#111827;border:1px solid #334155;border-radius:12px;padding:12px 14px}
.stat b{display:block;font-size:22px}.stat span{font-size:12px;color:#94a3b8}
.card{background:#111827;border:1px solid #334155;border-radius:14px;padding:16px;margin:14px 0}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:8px 6px;border-bottom:1px solid #1e293b;vertical-align:top}
th{color:#94a3b8;font-size:11px;text-transform:uppercase;letter-spacing:.04em}
label{display:block;font-size:12px;color:#94a3b8;margin:8px 0 2px}
input,select,textarea{width:100%;box-sizing:border-box;border-radius:8px;border:1px solid #475569;background:#020617;color:#e5e7eb;padding:8px 10px;font-size:13px}
.frow{display:grid;grid-template-columns:2fr 2fr 1.4fr 1.6fr 1fr 1fr 1fr;gap:8px}
button{background:#22c55e;color:#04130a;border:0;border-radius:8px;padding:9px 14px;font-weight:700;cursor:pointer;font-size:13px}
button.ghost{background:transparent;color:#94a3b8;border:1px solid #475569}
button.small{padding:4px 8px;font-size:12px;margin-right:4px}
.pill{display:inline-block;border-radius:999px;padding:3px 9px;font-size:11px;font-weight:700;background:#1e293b;border:1px solid #475569}
.pill.due{background:#7f1d1d;border-color:#ef4444;color:#fecaca}
.pill.sent{background:#1e3a8a;border-color:#3b82f6;color:#bfdbfe}
.pill.replied,.pill.screen,.pill.interview{background:#14532d;border-color:#22c55e;color:#bbf7d0}
.pill.offer{background:#713f12;border-color:#f59e0b;color:#fde68a}
.pill.rejected,.pill.dead{background:#1f2937;border-color:#6b7280;color:#9ca3af}
.duecard{background:#111827;border:1px solid #7f1d1d;border-radius:12px;padding:12px 14px;margin:8px 0;display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap}
.muted{color:#64748b;font-size:12px}
a{color:#60a5fa;text-decoration:none}
.actions button{margin-left:6px}
select.mini{width:auto;padding:3px 6px;font-size:12px}
</style></head><body><main>
<div class="card"><span class="pill">job search productivity</span><h1>Job Application CRM</h1>
<p class="muted">Outreach tracker - contacts, channels, and follow-up cadence. Applications kanban lives on <a href="/api/applications">/api/applications</a>.</p></div>

<div id="stats" class="grid"></div>

<div class="card"><h2>Due now - follow up</h2><div id="due"></div></div>

<div class="card"><h2>Log an outreach</h2>
<div class="frow">
<div><label>Company</label><input id="f_company" placeholder="Miter"></div>
<div><label>Role</label><input id="f_role" placeholder="Launch Operations Specialist"></div>
<div><label>Channel</label><select id="f_channel">
<option value="cold-apply">Cold apply (portal)</option><option value="referral">Referral</option>
<option value="recruiter">Recruiter outreach</option><option value="hiring-manager">Hiring manager email</option>
<option value="linkedin-dm">LinkedIn DM</option><option value="other">Other</option></select></div>
<div><label>Contact</label><input id="f_contact" placeholder="Sarah Kim, Talent Lead"></div>
<div><label>Status</label><select id="f_status"><option value="sent">Sent</option><option value="replied">Replied</option><option value="draft">Draft</option></select></div>
<div><label>Variant (A/B)</label><input id="f_variant" placeholder="A"></div>
<div><label>Sent</label><input id="f_sent" type="date"></div>
</div>
<label>Notes</label><input id="f_notes" placeholder="Value artifact: migration QA checklist sent alongside">
<button id="btn_add" style="margin-top:12px">Add outreach</button></div>

<div class="card"><h2>Outreach log</h2>
<div style="display:flex;gap:8px;margin-bottom:10px;flex-wrap:wrap">
<button class="ghost small" data-f="all">All</button><button class="ghost small" data-f="due">Due</button>
<button class="ghost small" data-f="referral">Referral</button><button class="ghost small" data-f="recruiter">Recruiter</button>
<button class="ghost small" data-f="hiring-manager">Hiring mgr</button><button class="ghost small" data-f="cold-apply">Cold apply</button>
<button class="ghost small" data-f="linkedin-dm">LinkedIn</button></div>
<div style="overflow-x:auto"><table><thead><tr><th>Company / Role</th><th>Channel</th><th>Contact</th><th>Sent</th><th>Next follow-up</th><th>Status</th><th>Notes</th><th></th></tr></thead>
<tbody id="rows"></tbody></table></div></div>

<script>
const $ = (s) => document.querySelector(s);
let FILTER = 'all';
const esc = (s) => { const d = document.createElement('div'); d.textContent = s == null ? '' : String(s); return d.innerHTML; };
const STATUSES = ['draft','sent','replied','screen','interview','offer','rejected','dead'];
function pill(status, due){
  // status is only ever drawn from the whitelisted STATUSES, so the class can
  // never break out of the attribute even if esc() (text-node escaping) lets
  // quotes through. Unknown values degrade to the neutral pill, not a raw class.
  const cls = due ? 'due' : (STATUSES.includes(status) ? status : 'unknown');
  return '<span class="pill ' + esc(cls) + '">' + esc(status) + (due ? ' (due)' : '') + '</span>';
}
async function api(url, opts){
  const r = await fetch(url, opts || {});
  const j = await r.json();
  if (!r.ok) { alert('Error: ' + (j.detail || JSON.stringify(j))); throw new Error(j.detail); }
  return j;
}
async function loadStats(){
  const d = await api('/api/dashboard');
  $('#stats').innerHTML =
    '<div class="stat"><b>' + d.due_count + '</b><span>Due now</span></div>' +
    '<div class="stat"><b>' + d.sent_30d + '</b><span>Sent last 30 days</span></div>' +
    '<div class="stat"><b>' + d.total + '</b><span>Total outreaches</span></div>' +
    channelStats(d.by_channel);
  const dueBox = $('#due');
  if (!d.due_count) { dueBox.innerHTML = '<p class="muted">Nothing due. Log an outreach to start the cadence.</p>'; return; }
  const q = await api('/api/outreaches?due=true');
  dueBox.innerHTML = q.outreaches.map(function(o){
    return '<div class="duecard"><div><b>' + esc(o.company) + '</b> - ' + esc(o.role || 'role n/a') + '<br><span class="muted">' + esc(o.channel) + (o.contact_name ? ' &middot; ' + esc(o.contact_name) : '') + '</span></div>' +
    '<div class="actions"><button class="small" data-a="log" data-id="' + o.id + '">Log follow-up</button>' +
    '<button class="small" data-a="replied" data-id="' + o.id + '">Mark replied</button></div></div>';
  }).join('');
}
function channelStats(bc){
  const order = ['referral','recruiter','hiring-manager','cold-apply','linkedin-dm','other'];
  let html = '';
  order.forEach(function(ch){
    const s = bc[ch];
    if (s) html += '<div class="stat"><b>' + s.reply_rate + '%</b><span>' + ch + ' reply rate (' + s.sent + ' sent)</span></div>';
  });
  return html || '<div class="stat"><b>-</b><span>No channel data yet</span></div>';
}
async function loadRows(){
  const url = FILTER === 'due' ? '/api/outreaches?due=true' : (FILTER === 'all' ? '/api/outreaches' : '/api/outreaches?channel=' + FILTER);
  const d = await api(url);
  const tb = $('#rows');
  tb.innerHTML = d.outreaches.map(function(o){
    return '<tr><td><b>' + esc(o.company) + '</b><br><span class="muted">' + esc(o.role) + '</span></td>' +
    '<td>' + esc(o.channel) + '</td><td>' + esc(o.contact_name) + (o.contact_email ? '<br><span class="muted">' + esc(o.contact_email) + '</span>' : '') + '</td>' +
    '<td>' + esc(o.sent_on) + '</td><td>' + (o.follow_up_on ? esc(o.follow_up_on) : '<span class="muted">-</span>') + '</td>' +
    '<td>' + pill(o.status, o.due) + (o.variant ? '<br><span class="muted">variant ' + esc(o.variant) + '</span>' : '') + '</td>' +
    '<td><span class="muted">' + esc(o.notes) + '</span></td>' +
    '<td class="actions"><button class="small" data-a="log" data-id="' + o.id + '">+f/u</button>' +
    '<button class="small" data-a="replied" data-id="' + o.id + '">replied</button>' +
    '<select class="mini" data-a="status" data-id="' + o.id + '">' + STATUSES.map(function(s){ return '<option value="' + s + '"' + (s === o.status ? ' selected' : '') + '>' + s + '</option>'; }).join('') + '</select></td></tr>';
  }).join('');
  if (!d.outreaches.length) tb.innerHTML = '<tr><td colspan="8" class="muted">No outreaches.</td></tr>';
}
async function refresh(){ await loadStats(); await loadRows(); }
document.body.addEventListener('click', async function(ev){
  const el = ev.target.closest('button[data-a]');
  if (!el) return;
  const id = el.dataset.id, a = el.dataset.a;
  try {
    if (a === 'log') await api('/api/outreaches/' + id, {method:'PATCH', headers:{'Content-Type':'application/json'}, body:JSON.stringify({log_follow_up:true})});
    if (a === 'replied') await api('/api/outreaches/' + id, {method:'PATCH', headers:{'Content-Type':'application/json'}, body:JSON.stringify({status:'replied'})});
    await refresh();
  } catch (e) {}
});
document.body.addEventListener('change', async function(ev){
  const el = ev.target.closest('select[data-a="status"]');
  if (!el) return;
  await api('/api/outreaches/' + el.dataset.id, {method:'PATCH', headers:{'Content-Type':'application/json'}, body:JSON.stringify({status:el.value})});
  await refresh();
});
document.querySelectorAll('button[data-f]').forEach(function(b){
  b.addEventListener('click', function(){ FILTER = b.dataset.f; refresh(); });
});
$('#btn_add').addEventListener('click', async function(){
  const payload = {
    company: $('#f_company').value, role: $('#f_role').value, channel: $('#f_channel').value,
    contact_name: $('#f_contact').value.split(',')[0].trim(),
    contact_title: ($('#f_contact').value.indexOf(',') > -1 ? $('#f_contact').value.split(',').slice(1).join(',').trim() : ''),
    status: $('#f_status').value, variant: $('#f_variant').value, notes: $('#f_notes').value,
    sent_on: $('#f_sent').value || undefined
  };
  if (!payload.company) { alert('Company is required'); return; }
  try {
    await api('/api/outreaches', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload)});
    ['f_company','f_role','f_contact','f_variant','f_notes'].forEach(function(id){ $('#' + id).value = ''; });
    $('#f_sent').value = '';
    await refresh();
  } catch (e) {}
});
$('#f_sent').value = new Date().toISOString().slice(0, 10);
refresh();
</script></main></body></html>"""
