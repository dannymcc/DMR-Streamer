"""Public-facing web UI for a self-hosted BrandMeister DMR audio stream."""
import json
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from config import settings
from db import SessionLocal, init_db
from models import FavouriteTG, ListenEvent, Preference, RadioIDUser
from radioid_cache import (
    RADIOID_META_KEY,
    enrich_payload,
    lookup_radioid_user,
    refresh_radioid_cache_if_stale,
)

BASE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
APP_VERSION = "2026.05.21.7"
templates.env.globals["app_version"] = APP_VERSION

limiter = Limiter(key_func=get_remote_address, default_limits=["60/minute"])


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    refresh_radioid_cache_if_stale()
    yield


app = FastAPI(title="DMR Stream", lifespan=lifespan, docs_url=None, redoc_url=None)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


@app.get("/apple-touch-icon.png")
@app.head("/apple-touch-icon.png")
@app.get("/apple-touch-icon-precomposed.png")
@app.head("/apple-touch-icon-precomposed.png")
@app.get("/apple-touch-icon-180x180.png")
@app.head("/apple-touch-icon-180x180.png")
@app.get("/apple-touch-icon-180x180-precomposed.png")
@app.head("/apple-touch-icon-180x180-precomposed.png")
@app.get("/favicon.ico")
@app.head("/favicon.ico")
async def apple_touch_icon():
    response = FileResponse(BASE_DIR / "static" / "apple-touch-icon.png", media_type="image/png")
    response.headers["Cache-Control"] = "no-cache, max-age=0, must-revalidate"
    return response


# ----------------- UI Routes -----------------

@app.get("/", response_class=HTMLResponse)
@limiter.limit("30/minute")
async def index(request: Request):
    response = templates.TemplateResponse(
        request,
        "index.html",
        {
            "stream_url": f"{settings.icecast_public_url}{settings.icecast_mount}",
            "current_tgs": current_monitored_tgs(),
            "callsign": settings.app_callsign,
        },
    )
    response.headers["Cache-Control"] = "no-store, max-age=0"
    return response


@app.get("/settings", response_class=HTMLResponse)
@limiter.limit("30/minute")
async def settings_page(request: Request):
    with SessionLocal() as db:
        favs = db.query(FavouriteTG).order_by(FavouriteTG.sort_order).all()
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "favourites": favs,
            "current_tgs": current_monitored_tgs(),
        },
    )


@app.get("/diagnostics", response_class=HTMLResponse)
@limiter.limit("20/minute")
async def diagnostics_page(request: Request):
    radioid_records, radioid_refreshed_at = radioid_cache_status()
    receiver = read_receiver_heartbeat()
    stream = await read_icecast_status()
    sidecars = {
        "active_call": read_sidecar_meta(ACTIVE_CALL_FILE),
        "tg_activity": read_sidecar_meta(TG_ACTIVITY_FILE),
        "tg_mutes": read_sidecar_meta(TG_MUTES_FILE),
        "tg_prefs": read_sidecar_meta(TG_PREFS_FILE),
        "tg_extra": read_sidecar_meta(TG_EXTRA_FILE),
        "receiver_heartbeat": read_sidecar_meta(RECEIVER_HEARTBEAT_FILE),
    }
    response = templates.TemplateResponse(
        request,
        "diagnostics.html",
        {
            "app_version": APP_VERSION,
            "service_worker_version": APP_VERSION,
            "monitored_tgs": [{"tg": tg, "name": tg_label(tg)} for tg in current_monitored_tgs()],
            "extra_tgs": read_extra_tgs(),
            "priority_tgs": sorted(read_priority_tgs()),
            "muted_tgs": sorted(read_muted_tgs()),
            "stream": stream,
            "receiver": receiver,
            "radioid_records": radioid_records,
            "radioid_refreshed_at": radioid_refreshed_at,
            "sidecars": sidecars,
        },
    )
    response.headers["Cache-Control"] = "no-store, max-age=0"
    return response


# ----------------- API Routes (HTMX partials) -----------------

@app.get("/api/lastheard", response_class=HTMLResponse)
@limiter.limit("60/minute")
async def last_heard(request: Request):
    """Most recent transmission per monitored TG, ordered by recency.

    We monitor multiple TGs but the UI wants a compact one-row-per-TG view of
    the most-recent activity. Walk the listen_events rows newest-first and
    keep the first hit for each TG.
    """
    with SessionLocal() as db:
        rows = (
            db.query(ListenEvent)
            .order_by(ListenEvent.heard_at.desc())
            .limit(200)
            .all()
        )
    seen: set[int] = set()
    transmissions = []
    for e in rows:
        if e.tg in seen:
            continue
        seen.add(e.tg)
        callsign, name = resolve_caller(e.src_id, e.callsign, e.name)
        transmissions.append({
            "tg": e.tg,
            "tg_name": TG_NAMES.get(e.tg),
            "src_id": e.src_id,
            "callsign": callsign,
            "name": name,
            "duration": e.duration_seconds or 0,
            "time": e.heard_at.isoformat() if e.heard_at else "",
        })
        if len(transmissions) >= 8:
            break
    return templates.TemplateResponse(
        request, "_lastheard.html", {"transmissions": transmissions}
    )


ACTIVE_CALL_FILE = "/data/active_call.json"
TG_MUTES_FILE = "/data/tg_mutes.json"
TG_ACTIVITY_FILE = "/data/tg_activity.json"
TG_PREFS_FILE = "/data/tg_prefs.json"
TG_TEMP_MUTES_FILE = "/data/tg_temp_mutes.json"
TG_EXTRA_FILE = "/data/tg_extra.json"
RECEIVER_HEARTBEAT_FILE = "/data/receiver_heartbeat.json"

# Friendly names for the TGs we monitor. Could be replaced with a periodic
# fetch from BM's talkgroup API or a CSV import; hardcoded is fine while the
# subscription list is small.
TG_NAMES = {
    9: "Local",
    91: "Worldwide",
    92: "Europe",
    235: "UK Call",
    2350: "UK Wide",
    3100: "USA Nationwide",
    2351: "UK Chat 1",
    2352: "UK Chat 2",
    2353: "UK Chat 3",
    23520: "UK North West",
    23526: "Hubnet UK",
    23531: "RAYNET UK",
    23562: "M62 Corridor",
    235175: "NW Allstar",
}

UK_CHAT_TGS = {2351, 2352, 2353}

TG_PRESETS = {
    "all": {"label": "All", "enabled": None},
    "uk": {"label": "UK", "enabled": {235, 2350, 2351, 2352, 2353, 23520, 23531, 23562}},
    "us": {"label": "US", "enabled": {3100}},
    "nw": {"label": "NW", "enabled": {23520, 23531, 23562, 235175}},
    "hubnet_off": {"label": "Hubnet off", "muted": {23526}},
}


@app.get("/api/active-call", response_class=HTMLResponse)
@limiter.limit("120/minute")
async def active_call(request: Request):
    """HTMX fragment describing the call currently in progress (if any) and the
    monitored-TG chip strip with the active one highlighted.

    The banner lingers after a call ends to match audio
    buffer/transport delay between dmr-rx and the listener's phone — otherwise
    the banner clears while you're still hearing the tail of the transmission.
    """
    return render_active_call_partial(request)


@app.get("/api/playback-call", response_class=HTMLResponse)
@limiter.limit("240/minute")
async def playback_call(request: Request, delay_ms: int = 1000):
    delay_ms = max(0, min(delay_ms, 30000))
    target = datetime.now(timezone.utc) - timedelta(milliseconds=delay_ms)
    return render_active_call_partial(
        request,
        include_live=False,
        payload=playback_payload_at(target),
    )


@app.get("/api/tg-selector", response_class=HTMLResponse)
@limiter.limit("60/minute")
async def tg_selector(request: Request):
    return render_active_call_partial(request, include_live=False)


@app.get("/api/temp-tg-chips", response_class=HTMLResponse)
@limiter.limit("60/minute")
async def temp_tg_chips(request: Request):
    return templates.TemplateResponse(
        request,
        "_temp_tg_chips.html",
        {"temp_monitor_tgs": [(row["tg"], row["label"]) for row in read_extra_tgs()]},
    )


def render_active_call_partial(
    request: Request,
    *,
    include_live: bool = True,
    payload: dict | None = None,
):
    if payload is None and include_live:
        payload = current_active_payload()

    extra_tgs = read_extra_tgs()
    extra_tg_values = {row["tg"] for row in extra_tgs}
    tgs = current_monitored_tgs(extra_tgs)
    tg_activity = read_tg_activity()
    temp_muted_tgs = read_temp_muted_tgs(tg_activity)
    muted_tgs = read_muted_tgs() | set(temp_muted_tgs)
    priority_tgs = read_priority_tgs()
    ordered_tgs = sorted(tgs, key=lambda tg: (tg not in priority_tgs, tgs.index(tg)))
    monitored_tgs = [
        (tg, tg_label(tg, extra_tgs)) for tg in ordered_tgs if tg not in extra_tg_values
    ]
    temp_monitor_tgs = [
        (tg, tg_label(tg, extra_tgs)) for tg in ordered_tgs if tg in extra_tg_values
    ]
    display_payload = None if payload and payload.get("tg") in muted_tgs else payload
    response = templates.TemplateResponse(
        request,
        "_active_call.html",
        {
            "active": display_payload,
            "monitored_tgs": [
                (tg, tg_name) for tg, tg_name in monitored_tgs if tg not in UK_CHAT_TGS
            ],
            "uk_chat_tgs": [
                (tg, tg_name) for tg, tg_name in monitored_tgs if tg in UK_CHAT_TGS
            ],
            "temp_monitor_tgs": temp_monitor_tgs,
            "muted_tgs": muted_tgs,
            "temp_muted_tgs": temp_muted_tgs,
            "priority_tgs": priority_tgs,
            "tg_activity": tg_activity,
            "presets": [(key, value["label"]) for key, value in TG_PRESETS.items()],
            "active_tg_name": TG_NAMES.get(display_payload["tg"]) if display_payload else None,
        },
    )
    response.headers["Cache-Control"] = "no-store, max-age=0"
    return response


def playback_payload_at(target: datetime) -> dict | None:
    """Return the call that was active at target time.

    The browser hears delayed audio, so the banner must be selected from the
    receiver timeline, not from whichever live HTML snapshot happened to be
    fetched earlier.
    """
    if target.tzinfo is None:
        target = target.replace(tzinfo=timezone.utc)

    current = current_active_payload()
    if current:
        started_at = parse_dt(current.get("started_at"))
        if started_at and started_at <= target:
            current = dict(current)
            current["lingering"] = (datetime.now(timezone.utc) - target).total_seconds() > 1.5
            return current

    with SessionLocal() as db:
        rows = (
            db.query(ListenEvent)
            .order_by(ListenEvent.heard_at.desc())
            .limit(20)
            .all()
        )
    for row in rows:
        ended_at = parse_dt(row.heard_at)
        if not ended_at:
            continue
        duration = max(0, int(row.duration_seconds or 0))
        started_at = ended_at - timedelta(seconds=duration)
        if started_at - timedelta(seconds=0.5) <= target <= ended_at + timedelta(seconds=1.5):
            callsign, name = resolve_caller(row.src_id, row.callsign, row.name)
            return {
                "active": True,
                "lingering": True,
                "tg": row.tg,
                "src_id": row.src_id,
                "callsign": callsign,
                "name": name,
                "started_at": started_at.isoformat(),
                "ended_at": ended_at.isoformat(),
            }
    return None


def parse_dt(value) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def resolve_caller(
    src_id: int | None,
    callsign: str | None,
    name: str | None,
) -> tuple[str | None, str | None]:
    """Prefer the local RadioID cache over Talker Alias fragments when present."""
    if src_id:
        cache_callsign, cache_name = lookup_radioid_user(src_id)
        callsign = cache_callsign or callsign
        name = cache_name or name
    return callsign, name


def current_active_payload() -> dict | None:
    now = datetime.now(timezone.utc)
    payload = None
    try:
        st = os.stat(ACTIVE_CALL_FILE)
        if (now.timestamp() - st.st_mtime) < 5:
            with open(ACTIVE_CALL_FILE) as f:
                payload = json.load(f)
            payload = enrich_payload(payload)
    except (FileNotFoundError, ValueError, OSError):
        payload = None
    if not payload or not payload.get("active"):
        return None
    activity = read_tg_activity().get(payload.get("tg"))
    if not activity or not activity.get("active"):
        return None
    if payload.get("src_id") and activity.get("src_id") != payload.get("src_id"):
        return None
    return payload


@app.post("/api/tg-mutes/{tg}", response_class=HTMLResponse)
@limiter.limit("60/minute")
async def toggle_tg_mute(request: Request, tg: int):
    tgs = current_monitored_tgs()
    if tg not in tgs:
        raise HTTPException(status_code=400, detail="TG is not monitored")
    muted_tgs = read_muted_tgs()
    if tg in muted_tgs:
        muted_tgs.remove(tg)
    else:
        muted_tgs.add(tg)
    write_muted_tgs(muted_tgs)
    return render_active_call_partial(request)


@app.post("/api/tg-temp-mutes/{tg}", response_class=HTMLResponse)
@limiter.limit("60/minute")
async def mute_tg_until_quiet(request: Request, tg: int):
    if tg not in current_monitored_tgs():
        raise HTTPException(status_code=400, detail="TG is not monitored")
    activity = read_tg_activity().get(tg)
    if not activity or not activity.get("active"):
        return render_active_call_partial(request)
    temp_mutes = read_temp_mutes_raw()
    temp_mutes[str(tg)] = {
        "tg": tg,
        "src_id": activity.get("src_id"),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    write_json_file(TG_TEMP_MUTES_FILE, {"tgs": temp_mutes})
    return render_active_call_partial(request)


@app.delete("/api/tg-temp-mutes/{tg}", response_class=HTMLResponse)
@limiter.limit("60/minute")
async def clear_tg_temp_mute(request: Request, tg: int):
    temp_mutes = read_temp_mutes_raw()
    temp_mutes.pop(str(tg), None)
    write_json_file(TG_TEMP_MUTES_FILE, {"tgs": temp_mutes})
    return render_active_call_partial(request)


@app.post("/api/tg-priority/{tg}", response_class=HTMLResponse)
@limiter.limit("60/minute")
async def toggle_tg_priority(request: Request, tg: int):
    if tg not in current_monitored_tgs():
        raise HTTPException(status_code=400, detail="TG is not monitored")
    priority_tgs = read_priority_tgs()
    if tg in priority_tgs:
        priority_tgs.remove(tg)
    else:
        priority_tgs.add(tg)
    write_json_file(TG_PREFS_FILE, {"priority_tgs": sorted(priority_tgs)})
    return render_active_call_partial(request)


@app.post("/api/tg-presets/{preset}", response_class=HTMLResponse)
@limiter.limit("30/minute")
async def apply_tg_preset(request: Request, preset: str):
    if preset not in TG_PRESETS:
        raise HTTPException(status_code=400, detail="unknown preset")
    tgs = set(current_monitored_tgs())
    spec = TG_PRESETS[preset]
    if spec.get("enabled") is None:
        muted_tgs = set()
    elif "enabled" in spec:
        muted_tgs = tgs - set(spec["enabled"])
    else:
        muted_tgs = set(spec.get("muted", set())) & tgs
    write_muted_tgs(muted_tgs)
    return render_active_call_partial(request)


@app.post("/api/tg-extra", response_class=HTMLResponse)
@limiter.limit("20/minute")
async def add_extra_tg(request: Request, tg: int = Form(...)):
    if not (1 <= tg <= 9999999):
        raise HTTPException(status_code=400, detail="invalid TG")
    rows = read_extra_tgs()
    base_tgs = set(settings.current_tgs())
    if tg not in base_tgs and tg not in {row["tg"] for row in rows}:
        rows.append({
            "tg": tg,
            "label": TG_NAMES.get(tg) or f"TG {tg}",
            "created_at": datetime.now(timezone.utc).isoformat(),
        })
        write_extra_tgs(rows)
    return render_active_call_partial(request)


@app.delete("/api/tg-extra/{tg}", response_class=HTMLResponse)
@limiter.limit("30/minute")
async def remove_extra_tg(request: Request, tg: int):
    rows = [row for row in read_extra_tgs() if row["tg"] != tg]
    write_extra_tgs(rows)
    return render_active_call_partial(request)


def current_monitored_tgs(extra_tgs: list[dict] | None = None) -> list[int]:
    tgs = list(settings.current_tgs())
    for row in extra_tgs if extra_tgs is not None else read_extra_tgs():
        tg = row["tg"]
        if tg not in tgs:
            tgs.append(tg)
    return tgs


def tg_label(tg: int, extra_tgs: list[dict] | None = None) -> str | None:
    if tg in TG_NAMES:
        return TG_NAMES[tg]
    for row in extra_tgs if extra_tgs is not None else read_extra_tgs():
        if row["tg"] == tg:
            return row.get("label")
    return None


def read_extra_tgs() -> list[dict]:
    try:
        with open(TG_EXTRA_FILE) as f:
            data = json.load(f)
    except (FileNotFoundError, ValueError, OSError):
        return []
    rows = data.get("tgs") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        return []
    seen: set[int] = set()
    cleaned = []
    for row in rows:
        if isinstance(row, dict):
            value = row.get("tg")
            label = (row.get("label") or "").strip()[:32]
            created_at = row.get("created_at")
        else:
            value = row
            label = ""
            created_at = None
        try:
            tg = int(value)
        except (TypeError, ValueError):
            continue
        if not (1 <= tg <= 9999999) or tg in seen or tg in settings.current_tgs():
            continue
        seen.add(tg)
        cleaned.append({
            "tg": tg,
            "label": label or TG_NAMES.get(tg) or f"TG {tg}",
            "created_at": created_at or "",
        })
    return cleaned


def write_extra_tgs(rows: list[dict]):
    write_json_file(TG_EXTRA_FILE, {"tgs": rows})


def read_muted_tgs() -> set[int]:
    try:
        with open(TG_MUTES_FILE) as f:
            data = json.load(f)
    except FileNotFoundError:
        return settings.default_muted_tg_set()
    except (ValueError, OSError):
        return set()
    values = data.get("muted_tgs") if isinstance(data, dict) else data
    if not isinstance(values, list):
        return set()
    return {int(tg) for tg in values if str(tg).isdigit()}


def read_priority_tgs() -> set[int]:
    try:
        with open(TG_PREFS_FILE) as f:
            data = json.load(f)
    except (FileNotFoundError, ValueError, OSError):
        return set()
    values = data.get("priority_tgs") if isinstance(data, dict) else []
    if not isinstance(values, list):
        return set()
    return {int(tg) for tg in values if str(tg).isdigit()}


def read_temp_mutes_raw() -> dict[str, dict]:
    try:
        with open(TG_TEMP_MUTES_FILE) as f:
            data = json.load(f)
    except (FileNotFoundError, ValueError, OSError):
        return {}
    rows = data.get("tgs") if isinstance(data, dict) else None
    return rows if isinstance(rows, dict) else {}


def read_temp_muted_tgs(tg_activity: dict[int, dict] | None = None) -> set[int]:
    tg_activity = tg_activity if tg_activity is not None else read_tg_activity()
    rows = read_temp_mutes_raw()
    kept: dict[str, dict] = {}
    muted: set[int] = set()
    for key, row in rows.items():
        if not isinstance(row, dict):
            continue
        try:
            tg = int(key)
        except ValueError:
            continue
        activity = tg_activity.get(tg)
        if not activity or not activity.get("active"):
            continue
        if row.get("src_id") and activity.get("src_id") != row.get("src_id"):
            continue
        kept[str(tg)] = row
        muted.add(tg)
    if kept != rows:
        write_json_file(TG_TEMP_MUTES_FILE, {"tgs": kept})
    return muted


def read_tg_activity() -> dict[int, dict]:
    try:
        with open(TG_ACTIVITY_FILE) as f:
            data = json.load(f)
    except (FileNotFoundError, ValueError, OSError):
        return {}
    rows = data.get("tgs") if isinstance(data, dict) else None
    if not isinstance(rows, dict):
        return {}
    now = datetime.now(timezone.utc)
    activity: dict[int, dict] = {}
    for key, row in rows.items():
        if not isinstance(row, dict):
            continue
        try:
            tg = int(key)
            updated_at = datetime.fromisoformat(row.get("updated_at", ""))
        except (TypeError, ValueError):
            continue
        if updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=timezone.utc)
        age = (now - updated_at).total_seconds()
        if 0 <= age <= 90:
            row = dict(row)
            if row.get("active") and age > 8:
                row["active"] = False
            activity[tg] = row
    return activity


def write_muted_tgs(muted_tgs: set[int]):
    write_json_file(TG_MUTES_FILE, {"muted_tgs": sorted(muted_tgs)})


def write_json_file(path: str, payload: dict):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(payload, f)
    os.replace(tmp, path)


@app.post("/api/favourites", response_class=HTMLResponse)
@limiter.limit("10/minute")
async def add_favourite(request: Request, tg: int = Form(...), label: str = Form(...)):
    label = label.strip()[:64]
    if not (1 <= tg <= 9999999):
        raise HTTPException(status_code=400, detail="invalid TG")
    with SessionLocal() as db:
        existing = db.query(FavouriteTG).filter(FavouriteTG.tg == tg).first()
        if not existing:
            max_order = db.query(FavouriteTG).count()
            db.add(FavouriteTG(tg=tg, label=label, sort_order=max_order))
            db.commit()
        favs = db.query(FavouriteTG).order_by(FavouriteTG.sort_order).all()
    return templates.TemplateResponse(request, "_favourites_list.html", {"favourites": favs})


@app.delete("/api/favourites/{fav_id}", response_class=HTMLResponse)
@limiter.limit("20/minute")
async def remove_favourite(request: Request, fav_id: int):
    with SessionLocal() as db:
        fav = db.query(FavouriteTG).filter(FavouriteTG.id == fav_id).first()
        if fav:
            db.delete(fav)
            db.commit()
        favs = db.query(FavouriteTG).order_by(FavouriteTG.sort_order).all()
    return templates.TemplateResponse(request, "_favourites_list.html", {"favourites": favs})


# ----------------- Health / Status -----------------

@app.get("/healthz")
async def health():
    return {"status": "ok", "ts": datetime.now(timezone.utc).isoformat()}


@app.get("/api/stream-status")
@limiter.limit("30/minute")
async def stream_status(request: Request):
    active = current_active_payload()
    activity = read_tg_activity()
    receiver = read_receiver_heartbeat()
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.get(f"{settings.icecast_internal}/status-json.xsl")
            if r.status_code == 200:
                data = r.json()
                source = data.get("icestats", {}).get("source")
                if source:
                    if isinstance(source, list):
                        source = source[0]
                    return {
                        "online": True,
                        "listeners": source.get("listeners", 0),
                        "bitrate": source.get("bitrate", 0),
                        "active": active,
                        "activity_count": sum(1 for row in activity.values() if row.get("active")),
                        "receiver": receiver,
                    }
        return {"online": False, "active": active, "activity_count": 0, "receiver": receiver}
    except Exception as e:
        return {"online": False, "error": str(e), "active": active, "activity_count": 0, "receiver": receiver}


@app.get("/api/debug/radioid")
@limiter.limit("30/minute")
async def radioid_debug(request: Request, dmr_id: int | None = None):
    with SessionLocal() as db:
        count = db.query(func.count(RadioIDUser.dmr_id)).scalar() or 0
        pref = db.get(Preference, RADIOID_META_KEY)
        lookup = None
        if dmr_id:
            user = db.get(RadioIDUser, dmr_id)
            lookup = {
                "dmr_id": dmr_id,
                "found": bool(user),
                "callsign": user.callsign if user else None,
                "name": user.name if user else None,
                "city": user.city if user else None,
                "state": user.state if user else None,
                "country": user.country if user else None,
            }
    return {
        "records": count,
        "refreshed_at": pref.value if pref else None,
        "lookup": lookup,
    }


@app.get("/api/debug/sync")
@limiter.limit("30/minute")
async def sync_debug(request: Request, delay_ms: int = 1000):
    return build_sync_debug(delay_ms)


def build_sync_debug(delay_ms: int = 1000) -> dict:
    delay_ms = max(0, min(int(delay_ms), 30000))
    now = datetime.now(timezone.utc)
    playback_target = now - timedelta(milliseconds=delay_ms)
    active_mtime = None
    active_age = None
    try:
        st = os.stat(ACTIVE_CALL_FILE)
        active_mtime = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat()
        active_age = round(now.timestamp() - st.st_mtime, 3)
    except OSError:
        pass
    return {
        "app": "ok",
        "app_version": APP_VERSION,
        "delay_ms": delay_ms,
        "playback_target": playback_target.isoformat(),
        "playback": playback_payload_at(playback_target),
        "active_file_age_s": active_age,
        "active_file_mtime": active_mtime,
        "active": current_active_payload(),
        "tg_activity": read_tg_activity(),
        "receiver": read_receiver_heartbeat(),
        "muted_tgs": sorted(read_muted_tgs()),
        "temp_muted_tgs": sorted(read_temp_muted_tgs()),
        "extra_tgs": read_extra_tgs(),
        "priority_tgs": sorted(read_priority_tgs()),
    }


@app.get("/api/debug/calls")
@limiter.limit("30/minute")
async def call_history_debug(request: Request, limit: int = 20):
    return {"calls": build_call_history(limit)}


@app.get("/api/debug/bundle")
@limiter.limit("15/minute")
async def diagnostics_bundle(request: Request, delay_ms: int = 1000, limit: int = 20):
    radioid_records, radioid_refreshed_at = radioid_cache_status()
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "app_version": APP_VERSION,
        "monitored_tgs": [
            {"tg": tg, "name": tg_label(tg)}
            for tg in current_monitored_tgs()
        ],
        "stream": await read_icecast_status(),
        "sync": build_sync_debug(delay_ms),
        "calls": build_call_history(limit),
        "radioid": {
            "records": radioid_records,
            "refreshed_at": radioid_refreshed_at,
        },
        "sidecars": {
            "active_call": read_sidecar_meta(ACTIVE_CALL_FILE),
            "tg_activity": read_sidecar_meta(TG_ACTIVITY_FILE),
            "tg_mutes": read_sidecar_meta(TG_MUTES_FILE),
            "tg_prefs": read_sidecar_meta(TG_PREFS_FILE),
            "tg_extra": read_sidecar_meta(TG_EXTRA_FILE),
            "receiver_heartbeat": read_sidecar_meta(RECEIVER_HEARTBEAT_FILE),
        },
    }


def build_call_history(limit: int = 20) -> list[dict]:
    limit = max(1, min(limit, 100))
    with SessionLocal() as db:
        rows = (
            db.query(ListenEvent)
            .order_by(ListenEvent.heard_at.desc())
            .limit(limit)
            .all()
        )
    calls = []
    for row in rows:
        ended_at = parse_dt(row.heard_at)
        duration = max(0, int(row.duration_seconds or 0))
        started_at = ended_at - timedelta(seconds=duration) if ended_at else None
        callsign, name = resolve_caller(row.src_id, row.callsign, row.name)
        calls.append({
            "tg": row.tg,
            "tg_name": TG_NAMES.get(row.tg),
            "src_id": row.src_id,
            "callsign": callsign,
            "name": name,
            "duration_s": duration,
            "started_at": started_at.isoformat() if started_at else None,
            "ended_at": ended_at.isoformat() if ended_at else None,
        })
    return calls


async def read_icecast_status() -> dict:
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.get(f"{settings.icecast_internal}/status-json.xsl")
            if r.status_code == 200:
                data = r.json()
                source = data.get("icestats", {}).get("source")
                if isinstance(source, list):
                    source = source[0] if source else None
                if source:
                    return {
                        "online": True,
                        "listeners": source.get("listeners", 0),
                        "bitrate": source.get("bitrate", 0),
                    }
        return {"online": False}
    except Exception as e:
        return {"online": False, "error": str(e)}


def radioid_cache_status() -> tuple[int, str | None]:
    with SessionLocal() as db:
        count = db.query(func.count(RadioIDUser.dmr_id)).scalar() or 0
        pref = db.get(Preference, RADIOID_META_KEY)
    return count, pref.value if pref else None


def read_sidecar_meta(path: str) -> dict:
    try:
        st = os.stat(path)
    except OSError:
        return {"exists": False}
    age = datetime.now(timezone.utc).timestamp() - st.st_mtime
    return {
        "exists": True,
        "mtime": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(),
        "age_s": round(age, 1),
        "size": st.st_size,
    }


def read_receiver_heartbeat() -> dict:
    try:
        with open(RECEIVER_HEARTBEAT_FILE) as f:
            data = json.load(f)
    except (FileNotFoundError, ValueError, OSError):
        return {"online": False}
    if not isinstance(data, dict):
        return {"online": False}
    updated_at = parse_dt(data.get("updated_at"))
    if not updated_at:
        return {"online": False}
    age = (datetime.now(timezone.utc) - updated_at).total_seconds()
    data = dict(data)
    data["age_s"] = round(age, 1)
    data["online"] = 0 <= age <= 20
    return data


# ----------------- PWA -----------------

@app.get("/manifest.webmanifest")
async def manifest():
    response = JSONResponse(
        {
            "name": "DMR Stream",
            "short_name": "DMR",
            "start_url": f"/?v={APP_VERSION}",
            "id": "/",
            "scope": "/",
            "display": "standalone",
            "display_override": ["standalone", "minimal-ui", "browser"],
            "background_color": "#0a0a0a",
            "theme_color": "#0a0a0a",
            "orientation": "portrait-primary",
            "launch_handler": {"client_mode": "focus-existing"},
            "categories": ["utilities", "music"],
            "description": settings.app_description,
            "icons": [
                {"src": f"/apple-touch-icon.png?v={APP_VERSION}", "sizes": "180x180", "type": "image/png", "purpose": "any"},
                {"src": f"/static/icon.svg?v={APP_VERSION}", "sizes": "any", "type": "image/svg+xml", "purpose": "any"},
                {"src": f"/static/icon-192.png?v={APP_VERSION}", "sizes": "192x192", "type": "image/png", "purpose": "any maskable"},
                {"src": f"/static/icon-512.png?v={APP_VERSION}", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"},
            ],
            "shortcuts": [
                {
                    "name": "Connect",
                    "short_name": "Connect",
                    "description": "Open the stream and connect audio.",
                    "url": f"/?action=connect&v={APP_VERSION}",
                    "icons": [{"src": f"/static/icon-192.png?v={APP_VERSION}", "sizes": "192x192"}],
                },
                {
                    "name": "Temporary talkgroup",
                    "short_name": "Temp TG",
                    "description": "Open the temporary talkgroup input.",
                    "url": f"/?action=temp-tg&v={APP_VERSION}",
                    "icons": [{"src": f"/static/icon-192.png?v={APP_VERSION}", "sizes": "192x192"}],
                },
                {
                    "name": "UK talkgroups",
                    "short_name": "UK TGs",
                    "description": "Switch to the UK talkgroup preset.",
                    "url": f"/?action=preset-uk&v={APP_VERSION}",
                    "icons": [{"src": f"/static/icon-192.png?v={APP_VERSION}", "sizes": "192x192"}],
                },
                {
                    "name": "US talkgroups",
                    "short_name": "US TGs",
                    "description": "Switch to the US talkgroup preset.",
                    "url": f"/?action=preset-us&v={APP_VERSION}",
                    "icons": [{"src": f"/static/icon-192.png?v={APP_VERSION}", "sizes": "192x192"}],
                },
            ],
        },
        media_type="application/manifest+json",
    )
    response.headers["Cache-Control"] = "no-cache, max-age=0, must-revalidate"
    return response


@app.get("/sw.js")
async def service_worker():
    sw_js = f"""
const VERSION = "{APP_VERSION}";
const CACHE = "dmrstream-" + VERSION;
const APP_SHELL = [
  "/",
  "/manifest.webmanifest",
  "/apple-touch-icon.png",
  "/apple-touch-icon-precomposed.png",
  "/apple-touch-icon-180x180.png",
  "/favicon.ico",
  "/static/icon.svg",
  "/static/icon-192.png",
  "/static/icon-512.png",
  "/static/apple-touch-icon.png",
  "/static/splash.svg",
  "/static/vendor/htmx.min.js",
  "/static/vendor/tailwindcss.js"
];
self.addEventListener('install', e => {{
  e.waitUntil(caches.open(CACHE).then(cache => cache.addAll(APP_SHELL)).finally(() => self.skipWaiting()));
}});
self.addEventListener('activate', e => {{
  e.waitUntil(Promise.all([
    caches.keys().then(keys => Promise.all(keys.filter(key => key !== CACHE).map(key => caches.delete(key)))),
    self.clients.claim()
  ]));
}});
self.addEventListener('fetch', e => {{
  const url = new URL(e.request.url);
  if (url.pathname.includes('/dmr.opus') || e.request.url.includes('icecast')) return;
  if (e.request.method !== 'GET') return;
  if (url.pathname.startsWith('/api/')) {{
    e.respondWith(fetch(e.request, {{ cache: 'no-store' }}).catch(() => new Response('', {{status: 503}})));
    return;
  }}
  if (e.request.mode === 'navigate') {{
    e.respondWith(
      fetch(e.request, {{ cache: 'no-store' }})
        .then(response => {{
          if (url.pathname === "/" && response.ok) {{
            const copy = response.clone();
            caches.open(CACHE).then(cache => cache.put("/", copy));
          }}
          return response;
        }})
        .catch(() => caches.match("/") || new Response('Offline', {{status: 503}}))
    );
    return;
  }}
  e.respondWith(
    caches.match(e.request).then(cached => cached || fetch(e.request).then(response => {{
      const copy = response.clone();
      caches.open(CACHE).then(cache => cache.put(e.request, copy));
      return response;
    }}).catch(() => cached || new Response('', {{status: 503}})))
  );
}});
"""
    response = Response(content=sw_js, media_type="application/javascript")
    response.headers["Cache-Control"] = "no-store, max-age=0"
    return response
