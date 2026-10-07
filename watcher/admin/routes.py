"""Admin UI routes: search CRUD, marketplace config, and health status -
server-rendered (no JS)."""
from __future__ import annotations

import datetime
from pathlib import Path
from typing import Dict, List, Optional

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from watcher import marketplace_configs as marketplace_configs_repo
from watcher import searches as searches_repo
from watcher import storage
from watcher.marketplaces import MARKETPLACES
from watcher.marketplaces import get as get_marketplace
from watcher.models import Search, WatchedModel

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
        result.append(WatchedModel(pattern=pattern, note=note, good_price=good_price))
    return result


def _watched_models_to_text(items: List[WatchedModel]) -> str:
    return "\n".join(f"{wm.pattern} | {wm.note} | {wm.good_price}" for wm in items)


def _parse_marketplace_queries(form_data) -> Dict[str, List[dict]]:
    result: Dict[str, List[dict]] = {}
    for key, marketplace in MARKETPLACES.items():
        text = form_data.get(f"marketplace_queries__{key}", "")
        queries = []
        for line in _lines_to_list(text):
            parts = [p.strip() for p in line.split("|")]
            parsed = marketplace.parse_query_line(parts)
            if parsed is not None:
                queries.append(parsed.model_dump(exclude_none=True))
        if queries:
            result[key] = queries
    return result


def _marketplace_queries_to_form(marketplace_queries: Dict[str, List[dict]]) -> Dict[str, str]:
    result = {}
    for key, marketplace in MARKETPLACES.items():
        lines = [marketplace.query_to_line(marketplace.query_model(**raw)) for raw in marketplace_queries.get(key, [])]
        result[key] = "\n".join(lines)
    return result


def _search_to_form(search: Optional[Search]) -> dict:
    if search is None:
        return dict(
            name="", enabled=True, scope="local", location="", require_shipping=False, max_price="",
            excluded_models="", excluded_words="", required_keywords="",
            hard_criteria="", soft_criteria="", watched_models="",
            marketplace_queries=_marketplace_queries_to_form({}),
        )
    return dict(
        name=search.name,
        enabled=search.enabled,
        scope=search.scope,
        location=search.location,
        require_shipping=search.require_shipping,
        max_price=search.max_price if search.max_price is not None else "",
        excluded_models=_list_to_lines(search.excluded_models),
        excluded_words=_list_to_lines(search.excluded_words),
        required_keywords=_list_to_lines(search.required_keywords),
        hard_criteria=_list_to_lines(search.hard_criteria),
        soft_criteria=_list_to_lines(search.soft_criteria),
        watched_models=_watched_models_to_text(search.watched_models),
        marketplace_queries=_marketplace_queries_to_form(search.marketplace_queries),
    )


def _form_to_search(form_data) -> Search:
    max_price = form_data.get("max_price", "")
    return Search(
        name=form_data.get("name", ""),
        enabled=form_data.get("enabled") is not None,
        scope=form_data.get("scope", "local"),
        location=form_data.get("location", ""),
        require_shipping=form_data.get("require_shipping") is not None,
        max_price=int(max_price) if max_price.strip() else None,
        excluded_models=_lines_to_list(form_data.get("excluded_models", "")),
        excluded_words=_lines_to_list(form_data.get("excluded_words", "")),
        required_keywords=_lines_to_list(form_data.get("required_keywords", "")),
        hard_criteria=_lines_to_list(form_data.get("hard_criteria", "")),
        soft_criteria=_lines_to_list(form_data.get("soft_criteria", "")),
        watched_models=_parse_watched_models(form_data.get("watched_models", "")),
        marketplace_queries=_parse_marketplace_queries(form_data),
    )


@router.get("/", include_in_schema=False)
def index() -> RedirectResponse:
    return RedirectResponse("/searches")


@router.get("/searches", response_class=HTMLResponse)
def list_searches(request: Request):
    conn = request.app.state.conn
    all_searches = searches_repo.list_searches(conn)
    return templates.TemplateResponse(request, "searches_list.html", {"searches": all_searches})


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
    return templates.TemplateResponse(request, "health.html", {"last_run": last_run, "monthly_cost": monthly_cost})


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
