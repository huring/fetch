"""Admin UI routes: search CRUD, marketplace config, and health status -
server-rendered (no JS)."""
from __future__ import annotations

import datetime
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from watcher import marketplace_configs as marketplace_configs_repo
from watcher import searches as searches_repo
from watcher import storage
from watcher import watched_items as watched_items_repo
from watcher.marketplaces import MARKETPLACES
from watcher.marketplaces import get as get_marketplace
from watcher.models import Search, WatchedItem, WatchedModel

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _lines_to_list(text: str) -> List[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


def _list_to_lines(items: List[str]) -> str:
    return "\n".join(items)


def _parse_watched_models(text: str) -> List[WatchedModel]:
    result = []
    for line in _lines_to_list(text):
        parts = [p.strip() for p in line.split("|")]
        pattern = parts[0] if parts else ""
        if not pattern:
            continue
        note = parts[1] if len(parts) > 1 else ""
        good_price = parts[2] if len(parts) > 2 else ""
        is_ideal = bool(parts[3]) if len(parts) > 3 else False
        result.append(WatchedModel(pattern=pattern, note=note, good_price=good_price, is_ideal=is_ideal))
    return result


def _watched_models_to_text(items: List[WatchedModel]) -> str:
    return "\n".join(
        f"{wm.pattern} | {wm.note} | {wm.good_price} | {'ideal' if wm.is_ideal else ''}" for wm in items
    )


def _search_to_form(search: Optional[Search]) -> dict:
    if search is None:
        return dict(
            name="", enabled=True, scope="local", location="", require_shipping=False, max_price="",
            min_price="", scoring_mode="plain", instant_alert_price="", digest_style="summary_link",
            excluded_models="", excluded_words="", required_keywords="",
            hard_criteria="", soft_criteria="", watched_models="",
            search_phrases="", marketplaces=[],
        )
    return dict(
        name=search.name,
        enabled=search.enabled,
        scope=search.scope,
        location=search.location,
        require_shipping=search.require_shipping,
        max_price=search.max_price if search.max_price is not None else "",
        min_price=search.min_price if search.min_price is not None else "",
        scoring_mode=search.scoring_mode,
        instant_alert_price=search.instant_alert_price if search.instant_alert_price is not None else "",
        digest_style=search.digest_style,
        excluded_models=_list_to_lines(search.excluded_models),
        excluded_words=_list_to_lines(search.excluded_words),
        required_keywords=_list_to_lines(search.required_keywords),
        hard_criteria=_list_to_lines(search.hard_criteria),
        soft_criteria=_list_to_lines(search.soft_criteria),
        watched_models=_watched_models_to_text(search.watched_models),
        search_phrases=_list_to_lines(search.search_phrases),
        marketplaces=search.marketplaces,
    )


def _form_to_search(form_data) -> Search:
    max_price = form_data.get("max_price", "")
    min_price = form_data.get("min_price", "")
    instant_alert_price = form_data.get("instant_alert_price", "")
    scoring_mode = "rated" if form_data.get("scoring_mode") == "rated" else "plain"
    digest_style = "summary_link" if form_data.get("digest_style") == "summary_link" else "itemized"
    marketplaces = [key for key in form_data.getlist("marketplaces") if key in MARKETPLACES]
    return Search(
        name=form_data.get("name", ""),
        enabled=form_data.get("enabled") is not None,
        scope=form_data.get("scope", "local"),
        location=form_data.get("location", ""),
        require_shipping=form_data.get("require_shipping") is not None,
        max_price=int(max_price) if max_price.strip() else None,
        min_price=int(min_price) if min_price.strip() else None,
        scoring_mode=scoring_mode,
        instant_alert_price=int(instant_alert_price) if instant_alert_price.strip() else None,
        digest_style=digest_style,
        excluded_models=_lines_to_list(form_data.get("excluded_models", "")),
        excluded_words=_lines_to_list(form_data.get("excluded_words", "")),
        required_keywords=_lines_to_list(form_data.get("required_keywords", "")),
        hard_criteria=_lines_to_list(form_data.get("hard_criteria", "")),
        soft_criteria=_lines_to_list(form_data.get("soft_criteria", "")),
        watched_models=_parse_watched_models(form_data.get("watched_models", "")),
        search_phrases=_lines_to_list(form_data.get("search_phrases", "")),
        marketplaces=marketplaces,
    )


@router.get("/", include_in_schema=False)
def index() -> RedirectResponse:
    return RedirectResponse("/searches")


@router.get("/searches", response_class=HTMLResponse)
def list_searches(request: Request, ran: Optional[int] = None):
    conn = request.app.state.conn
    settings = request.app.state.settings
    all_searches = searches_repo.list_searches(conn)
    counts = storage.get_search_bucket_counts(conn, settings.score_digest_min, settings.score_instant_threshold)
    overview = storage.get_overview_stats(conn, settings.score_digest_min, settings.score_instant_threshold)
    top_listings = storage.get_top_listings(conn)
    ran_search_name = next((s.name for s in all_searches if s.id == ran), None) if ran is not None else None
    return templates.TemplateResponse(
        request, "searches_list.html",
        {
            "searches": all_searches, "counts": counts, "overview": overview, "top_listings": top_listings,
            "ran_search_name": ran_search_name,
        },
    )


def _age_days(first_seen_at: Optional[str]) -> Optional[int]:
    if not first_seen_at:
        return None
    first_seen = datetime.datetime.strptime(first_seen_at, "%Y-%m-%d %H:%M:%S")
    return (datetime.datetime.utcnow() - first_seen).days


@router.get("/searches/{search_id}/listings", response_class=HTMLResponse)
def search_listings(request: Request, search_id: int, bucket: str = "found"):
    conn = request.app.state.conn
    settings = request.app.state.settings
    search = searches_repo.get_search(conn, search_id)
    if search is None or bucket not in storage.BUCKETS:
        return RedirectResponse("/searches", status_code=303)
    rows = storage.list_bucket_listings(
        conn, search_id, bucket, settings.score_digest_min, settings.score_instant_threshold
    )
    listings = [dict(row, age_days=_age_days(row["first_seen_at"])) for row in rows]
    return templates.TemplateResponse(
        request, "search_listings.html",
        {
            "search": search, "bucket": bucket, "bucket_label": storage.BUCKET_LABELS[bucket],
            "listings": listings,
        },
    )


@router.get("/feed", response_class=HTMLResponse)
def feed(request: Request, bucket: str = "daily_roundup", search_id: str = ""):
    conn = request.app.state.conn
    settings = request.app.state.settings
    if bucket not in ("daily_roundup", "instant_alert"):
        bucket = "daily_roundup"
    # The "All searches" <option> submits search_id="" (empty string, not
    # absent) - FastAPI would reject that against an Optional[int] param, so
    # it's taken as a plain string here and parsed by hand instead.
    selected_search_id = int(search_id) if search_id.strip() else None
    rows = storage.list_feed_listings(
        conn, bucket, settings.score_digest_min, settings.score_instant_threshold, search_id=selected_search_id
    )
    listings = [dict(row, age_days=_age_days(row["first_seen_at"])) for row in rows]
    all_searches = searches_repo.list_searches(conn)
    return templates.TemplateResponse(
        request, "feed.html",
        {
            "listings": listings, "bucket": bucket, "bucket_label": storage.BUCKET_LABELS[bucket],
            "searches": all_searches, "selected_search_id": selected_search_id,
        },
    )


@router.get("/searches/new", response_class=HTMLResponse)
def new_search_form(request: Request):
    return templates.TemplateResponse(
        request, "search_form.html",
        {
            "form": _search_to_form(None), "is_edit": False, "action_url": "/searches/new",
            "marketplaces": list(MARKETPLACES.values()),
        },
    )


@router.post("/searches/new")
async def create_search(request: Request):
    conn = request.app.state.conn
    form_data = await request.form()
    search = _form_to_search(form_data)
    searches_repo.create_search(conn, search)
    return RedirectResponse("/searches", status_code=303)


@router.get("/searches/{search_id}/edit", response_class=HTMLResponse)
def edit_search_form(request: Request, search_id: int):
    conn = request.app.state.conn
    search = searches_repo.get_search(conn, search_id)
    return templates.TemplateResponse(
        request, "search_form.html",
        {
            "form": _search_to_form(search), "is_edit": True, "action_url": f"/searches/{search_id}/edit",
            "marketplaces": list(MARKETPLACES.values()),
        },
    )


@router.post("/searches/{search_id}/edit")
async def update_search(request: Request, search_id: int):
    conn = request.app.state.conn
    form_data = await request.form()
    search = _form_to_search(form_data)
    searches_repo.update_search(conn, search_id, search)
    return RedirectResponse("/searches", status_code=303)


@router.post("/searches/{search_id}/toggle")
def toggle_search(request: Request, search_id: int):
    conn = request.app.state.conn
    search = searches_repo.get_search(conn, search_id)
    if search is not None:
        searches_repo.set_enabled(conn, search_id, not search.enabled)
    return RedirectResponse("/searches", status_code=303)


@router.post("/searches/{search_id}/delete")
def delete_search(request: Request, search_id: int):
    conn = request.app.state.conn
    searches_repo.delete_search(conn, search_id)
    return RedirectResponse("/searches", status_code=303)


@router.post("/searches/{search_id}/run")
def run_search_now(request: Request, search_id: int):
    if searches_repo.get_search(request.app.state.conn, search_id) is None:
        return RedirectResponse("/searches", status_code=303)
    request.app.state.trigger_search_run(search_id)
    return RedirectResponse(f"/searches?ran={search_id}", status_code=303)


def _watched_item_to_form(item: Optional[WatchedItem]) -> dict:
    if item is None:
        return dict(name="", url="", enabled=True, target_price="", check_frequency="daily", find_used=False)
    return dict(
        name=item.name,
        url=item.url,
        enabled=item.enabled,
        target_price=item.target_price if item.target_price is not None else "",
        check_frequency=item.check_frequency,
        find_used=item.find_used,
    )


def _form_to_watched_item(form_data) -> WatchedItem:
    target_price = form_data.get("target_price", "")
    check_frequency = form_data.get("check_frequency", "daily")
    if check_frequency not in ("daily", "weekly", "monthly"):
        check_frequency = "daily"
    return WatchedItem(
        name=form_data.get("name", ""),
        url=form_data.get("url", ""),
        enabled=form_data.get("enabled") is not None,
        target_price=int(target_price) if target_price.strip() else None,
        check_frequency=check_frequency,
        find_used=form_data.get("find_used") is not None,
    )


@router.get("/watched-items", response_class=HTMLResponse)
def list_watched_items(request: Request, checked: Optional[int] = None):
    conn = request.app.state.conn
    items = watched_items_repo.list_watched_items(conn)
    return templates.TemplateResponse(request, "watched_items_list.html", {"items": items, "checked": checked})


@router.get("/watched-items/new", response_class=HTMLResponse)
def new_watched_item_form(request: Request):
    return templates.TemplateResponse(
        request, "watched_item_form.html",
        {"form": _watched_item_to_form(None), "is_edit": False, "action_url": "/watched-items/new", "item": None},
    )


@router.post("/watched-items/new")
async def create_watched_item(request: Request):
    conn = request.app.state.conn
    form_data = await request.form()
    item = _form_to_watched_item(form_data)
    watched_items_repo.create_watched_item(conn, item)
    return RedirectResponse("/watched-items", status_code=303)


@router.get("/watched-items/{item_id}/edit", response_class=HTMLResponse)
def edit_watched_item_form(request: Request, item_id: int):
    conn = request.app.state.conn
    item = watched_items_repo.get_watched_item(conn, item_id)
    return templates.TemplateResponse(
        request, "watched_item_form.html",
        {"form": _watched_item_to_form(item), "is_edit": True, "action_url": f"/watched-items/{item_id}/edit", "item": item},
    )


@router.post("/watched-items/{item_id}/edit")
async def update_watched_item(request: Request, item_id: int):
    conn = request.app.state.conn
    form_data = await request.form()
    item = _form_to_watched_item(form_data)
    existing = watched_items_repo.get_watched_item(conn, item_id)
    watched_items_repo.update_watched_item(conn, item_id, item)
    # find_used turned off: stop the linked search from polling (kept, not
    # deleted, so its history and a re-enable both still work); turned back
    # on: re-enable rather than leaving create_watched_item's lazy
    # (title-not-known-yet) path to spin up a duplicate.
    if existing is not None and existing.linked_search_id is not None and existing.find_used != item.find_used:
        searches_repo.set_enabled(conn, existing.linked_search_id, item.find_used)
    return RedirectResponse("/watched-items", status_code=303)


@router.post("/watched-items/{item_id}/toggle")
def toggle_watched_item(request: Request, item_id: int):
    conn = request.app.state.conn
    item = watched_items_repo.get_watched_item(conn, item_id)
    if item is not None:
        watched_items_repo.set_enabled(conn, item_id, not item.enabled)
    return RedirectResponse("/watched-items", status_code=303)


@router.post("/watched-items/{item_id}/delete")
def delete_watched_item(request: Request, item_id: int):
    conn = request.app.state.conn
    watched_items_repo.delete_watched_item(conn, item_id)
    return RedirectResponse("/watched-items", status_code=303)


@router.post("/watched-items/{item_id}/check")
def check_watched_item_now(request: Request, item_id: int):
    if watched_items_repo.get_watched_item(request.app.state.conn, item_id) is None:
        return RedirectResponse("/watched-items", status_code=303)
    request.app.state.trigger_watched_item_check(item_id)
    return RedirectResponse("/watched-items?checked=" + str(item_id), status_code=303)


@router.get("/marketplaces", response_class=HTMLResponse)
def list_marketplaces(request: Request):
    conn = request.app.state.conn
    configs = {c.key: c for c in marketplace_configs_repo.list_configs(conn)}
    rows = []
    for key, marketplace in MARKETPLACES.items():
        config = configs.get(key)
        rows.append(
            {
                "key": key,
                "display_name": marketplace.display_name,
                "poll_interval_minutes": config.poll_interval_minutes if config else marketplace.default_poll_interval_minutes,
                "request_delay_seconds": config.request_delay_seconds if config else marketplace.default_request_delay_seconds,
                "has_auth_fields": bool(marketplace.auth_fields),
                "auth_configured": bool(config and config.auth),
                "is_auction": marketplace.is_auction,
            }
        )
    return templates.TemplateResponse(request, "marketplaces_list.html", {"marketplaces": rows})


@router.get("/marketplaces/{key}/edit", response_class=HTMLResponse)
def edit_marketplace_form(request: Request, key: str):
    conn = request.app.state.conn
    marketplace = get_marketplace(key)
    if marketplace is None:
        return RedirectResponse("/marketplaces", status_code=303)
    config = marketplace_configs_repo.get_config(conn, key)
    return templates.TemplateResponse(
        request, "marketplace_form.html",
        {"marketplace": marketplace, "config": config, "action_url": f"/marketplaces/{key}/edit"},
    )


@router.post("/marketplaces/{key}/edit")
async def update_marketplace(request: Request, key: str):
    conn = request.app.state.conn
    marketplace = get_marketplace(key)
    if marketplace is None:
        return RedirectResponse("/marketplaces", status_code=303)
    form_data = await request.form()
    poll_interval_minutes = int(form_data.get("poll_interval_minutes") or marketplace.default_poll_interval_minutes)
    request_delay_seconds = float(form_data.get("request_delay_seconds") or marketplace.default_request_delay_seconds)
    auth_updates = {f.key: form_data.get(f"auth__{f.key}", "") for f in marketplace.auth_fields}
    marketplace_configs_repo.update_config(conn, key, poll_interval_minutes, request_delay_seconds, auth_updates)
    return RedirectResponse("/marketplaces", status_code=303)


@router.get("/health", response_class=HTMLResponse)
def health_page(request: Request):
    conn = request.app.state.conn
    last_run = storage.get_last_run(conn)
    monthly_cost = storage.monthly_cost_summary(conn)
    awaiting_scoring = storage.count_listings_awaiting_scoring(conn)
    in_progress_scoring = storage.count_listings_in_progress_scoring(conn)
    return templates.TemplateResponse(
        request,
        "health.html",
        {
            "last_run": last_run,
            "monthly_cost": monthly_cost,
            "awaiting_scoring": awaiting_scoring,
            "in_progress_scoring": in_progress_scoring,
        },
    )


@router.post("/health/clear")
def clear_data(request: Request):
    conn = request.app.state.conn
    storage.clear_operational_data(conn)
    return RedirectResponse("/health", status_code=303)


@router.get("/healthz")
def healthz(request: Request) -> JSONResponse:
    """Machine-readable health check for Docker's HEALTHCHECK.

    Looks at the last *completed* run, not just the most recent run row - a
    run can legitimately take several minutes (sequential, rate-limited
    fetches across every search), and that shouldn't register as unhealthy
    while it's still in progress.
    """
    conn = request.app.state.conn
    last_run = storage.get_last_completed_run(conn)

    if last_run is None:
        return JSONResponse({"status": "starting"}, status_code=200)
    if last_run["status"] != "ok":
        return JSONResponse(
            {"status": "unhealthy", "reason": "last run failed", "error": last_run["error_message"]},
            status_code=503,
        )

    finished_at = last_run["finished_at"]
    if finished_at:
        finished_dt = datetime.datetime.strptime(finished_at, "%Y-%m-%d %H:%M:%S")
        age_minutes = (datetime.datetime.utcnow() - finished_dt).total_seconds() / 60
        configs = marketplace_configs_repo.list_configs(conn)
        stale_after = max((c.poll_interval_minutes for c in configs), default=240) * 3
        if age_minutes > stale_after:
            return JSONResponse(
                {"status": "unhealthy", "reason": f"last run finished {int(age_minutes)} min ago, expected within {stale_after} min"},
                status_code=503,
            )

    return JSONResponse({"status": "ok", "last_run_finished_at": finished_at}, status_code=200)
