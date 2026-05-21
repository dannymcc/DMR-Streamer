"""Local RadioID.net user database cache."""
from __future__ import annotations

import json
import logging
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, text

from db import SessionLocal, engine
from models import Preference, RadioIDUser

log = logging.getLogger("radioid-cache")

RADIOID_USERS_URL = "https://www.radioid.net/static/users.json"
RADIOID_META_KEY = "radioid_users_refreshed_at"


def lookup_radioid_user(dmr_id: int | None) -> tuple[str | None, str | None]:
    if not dmr_id:
        return None, None
    with SessionLocal() as db:
        user = db.get(RadioIDUser, dmr_id)
        if not user:
            return None, None
        return user.callsign or None, user.name or None


def enrich_payload(payload: dict | None) -> dict | None:
    if not payload or not payload.get("src_id"):
        return payload
    callsign, name = lookup_radioid_user(payload.get("src_id"))
    if callsign:
        payload["callsign"] = callsign
    if name:
        payload["name"] = name
    return payload


def refresh_radioid_cache_if_stale(max_age_days: int = 7, *, background: bool = True):
    if not _cache_is_stale(max_age_days):
        return
    if background:
        threading.Thread(target=refresh_radioid_cache, daemon=True, name="radioid-cache").start()
    else:
        refresh_radioid_cache()


def refresh_radioid_cache():
    log.info("refreshing RadioID user cache from %s", RADIOID_USERS_URL)
    try:
        req = urllib.request.Request(RADIOID_USERS_URL, headers={"User-Agent": "dmrstream/1.0"})
        with urllib.request.urlopen(req, timeout=90) as r:
            data = json.loads(r.read().decode("utf-8", errors="replace"))
    except (urllib.error.URLError, OSError, ValueError) as e:
        log.warning("RadioID cache refresh failed: %s", e)
        return

    rows = data.get("users") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        log.warning("RadioID cache refresh returned unexpected payload")
        return

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    batch = []
    total = 0
    with engine.begin() as conn:
        for row in rows:
            if not isinstance(row, dict):
                continue
            dmr_id = row.get("radio_id") or row.get("id")
            try:
                dmr_id = int(dmr_id)
            except (TypeError, ValueError):
                continue
            callsign = _clean(row.get("callsign"), 16)
            name = _clean(row.get("name"), 96) or _name_from_parts(row)
            batch.append({
                "dmr_id": dmr_id,
                "callsign": callsign,
                "name": name,
                "city": _clean(row.get("city"), 96),
                "state": _clean(row.get("state"), 96),
                "country": _clean(row.get("country"), 96),
                "updated_at": now,
            })
            if len(batch) >= 2000:
                _upsert_batch(conn, batch)
                total += len(batch)
                batch.clear()
        if batch:
            _upsert_batch(conn, batch)
            total += len(batch)

    with SessionLocal() as db:
        pref = db.get(Preference, RADIOID_META_KEY)
        if not pref:
            pref = Preference(key=RADIOID_META_KEY)
            db.add(pref)
        pref.value = datetime.now(timezone.utc).isoformat()
        db.commit()
    log.info("RadioID user cache refreshed (%d users)", total)


def radioid_cache_count() -> int:
    with SessionLocal() as db:
        return db.query(func.count(RadioIDUser.dmr_id)).scalar() or 0


def _cache_is_stale(max_age_days: int) -> bool:
    with SessionLocal() as db:
        pref = db.get(Preference, RADIOID_META_KEY)
        if not pref or not pref.value:
            return True
        try:
            refreshed_at = datetime.fromisoformat(pref.value)
        except ValueError:
            return True
        if refreshed_at.tzinfo is None:
            refreshed_at = refreshed_at.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) - refreshed_at > timedelta(days=max_age_days)


def _upsert_batch(conn, batch: list[dict]):
    conn.execute(
        text(
            """
            INSERT INTO radioid_users
                (dmr_id, callsign, name, city, state, country, updated_at)
            VALUES
                (:dmr_id, :callsign, :name, :city, :state, :country, :updated_at)
            ON CONFLICT(dmr_id) DO UPDATE SET
                callsign = excluded.callsign,
                name = excluded.name,
                city = excluded.city,
                state = excluded.state,
                country = excluded.country,
                updated_at = excluded.updated_at
            """
        ),
        batch,
    )


def _clean(value, limit: int) -> str | None:
    if value is None:
        return None
    value = " ".join(str(value).split())
    return value[:limit] or None


def _name_from_parts(row: dict) -> str | None:
    parts = [_clean(row.get("fname"), 48), _clean(row.get("surname"), 48)]
    name = " ".join(p for p in parts if p)
    return name[:96] or None


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
    refresh_radioid_cache()
    print(f"radioid_users={radioid_cache_count()}")
