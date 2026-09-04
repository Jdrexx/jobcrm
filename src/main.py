from __future__ import annotations
import json
import sqlite3
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

APP_NAME = "Job Application CRM"
APP_VERSION = "0.2.0"
DB_FILE = Path(__file__).resolve().parent.parent / "data" / "app.sqlite"
DB_FILE.parent.mkdir(exist_ok=True)
app = FastAPI(title=APP_NAME, version=APP_VERSION)
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)


@app.exception_handler(ValueError)
def value_error_handler(request, exc: ValueError):
    return JSONResponse(status_code=422, content={"detail": str(exc)})


@app.exception_handler(KeyError)
def key_error_handler(request, exc: KeyError):
    return JSONResponse(
        status_code=404, content={"detail": f"not found: {exc.args[0]}"}
    )


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


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    conn.execute("pragma journal_mode=wal")
    return conn


def init_db() -> None:
    with db() as conn:
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


@app.on_event("startup")
def on_startup() -> None:
    init_db()


def save_record(kind: str, title: str, payload: str) -> int:
    with db() as conn:
        cur = conn.execute(
            "insert into records(kind,title,payload,created_at) values (?,?,?,?)",
            (kind, title, payload, datetime.now(timezone.utc).isoformat()),
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
    return date.today().isoformat()


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
    return {
        "ok": True,
        "app": APP_NAME,
        "version": APP_VERSION,
        "records": len(rows()),
        "outreaches": len(rows_outreaches()),
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
    company: str
    role: str = ""
    channel: str = "cold-apply"
    contact_name: str = ""
    contact_title: str = ""
    contact_email: str = ""
    variant: str = ""
    status: str = "sent"
    sent_on: str | None = None
    notes: str = ""


def validate_outreach(req: OutreachRequest) -> None:
    if not req.company.strip():
        raise ValueError("company is required")
    if req.status not in OUTREACH_STATUSES:
        raise ValueError(f"status must be one of {', '.join(OUTREACH_STATUSES)}")
    date.fromisoformat(req.sent_on)  # raises ValueError if malformed


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
    req.sent_on = req.sent_on or today_iso()
    validate_outreach(req)
    follow_up_on = next_follow_up(req.status, req.sent_on, 0)
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
                req.sent_on,
                follow_up_on,
                0,
                req.notes.strip(),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        oid = int(cur.lastrowid)
    return get_outreach(oid)


def get_outreach(oid: int) -> dict[str, Any]:
    with db() as conn:
        row = conn.execute("select * from outreaches where id=?", (oid,)).fetchone()
    if row is None:
        raise KeyError(oid)
    return outreach_row(row)


class OutreachPatch(BaseModel):
    status: str | None = None
    follow_up_on: str | None = None
    notes: str | None = None
    variant: str | None = None
    log_follow_up: bool = False


@app.patch("/api/outreaches/{oid}")
def patch_outreach(oid: int, req: OutreachPatch):
    with db() as conn:
        row = conn.execute("select * from outreaches where id=?", (oid,)).fetchone()
        if row is None:
            raise KeyError(oid)
        current = dict(row)
    status = req.status if req.status is not None else current["status"]
    if req.status is not None and req.status not in OUTREACH_STATUSES:
        raise ValueError(f"status must be one of {', '.join(OUTREACH_STATUSES)}")
    done = current["follow_ups_done"]
    follow_up_on = current["follow_up_on"]
    if req.log_follow_up and status in ("sent", "replied"):
        done = min(done + 1, FOLLOWUP_MAX_ROUNDS)
        follow_up_on = next_follow_up(status, today_iso(), done)
    if req.follow_up_on is not None:
        follow_up_on = req.follow_up_on
    if status in NO_FOLLOWUP_SET:
        follow_up_on = None
    fields = {"status": status, "follow_ups_done": done, "follow_up_on": follow_up_on}
    if req.notes is not None:
        fields["notes"] = req.notes
    if req.variant is not None:
        fields["variant"] = req.variant
    with db() as conn:
        conn.execute(
            "update outreaches set status=?, follow_ups_done=?, follow_up_on=?, notes=?, variant=? where id=?",
            (
                fields["status"],
                fields["follow_ups_done"],
                fields["follow_up_on"],
                fields.get("notes", current["notes"]),
                fields.get("variant", current["variant"]),
                oid,
            ),
        )
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
  const cls = due ? 'due' : status;
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
