"""Admin UI routes: search CRUD and health status, server-rendered (no JS)."""
from __future__ import annotations

import datetime
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from watcher import searches as searches_repo
from watcher import storage
from watcher.models import BlocketQuery, Search, TraderaQuery, WatchedModel

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


def _parse_blocket_queries(text: str) -> List[BlocketQuery]:
    result = []
    for line in _lines_to_list(text):
        parts = [p.strip() for p in line.split("|")]
        q = parts[0] if parts else ""
        if not q:
            continue
        category = parts[1] if len(parts) > 1 and parts[1] else None
        sub_category = parts[2] if len(parts) > 2 and parts[2] else None
        result.append(BlocketQuery(q=q, category=category, sub_category=sub_category))
    return result


def _blocket_queries_to_text(items: List[BlocketQuery]) -> str:
    return "\n".join(f"{q.q} | {q.category or ''} | {q.sub_category or ''}" for q in items)


def _parse_tradera_queries(text: str) -> List[TraderaQuery]:
    result = []
    for line in _lines_to_list(text):
        parts = [p.strip() for p in line.split("|")]
        query = parts[0] if parts else ""
        if not query:
            continue
        category_id = parts[1] if len(parts) > 1 and parts[1] else None
        result.append(TraderaQuery(query=query, category_id=category_id))
    return result


def _tradera_queries_to_text(items: List[TraderaQuery]) -> str:
    return "\n".join(f"{q.query} | {q.category_id or ''}" for q in items)


def _search_to_form(search: Optional[Search]) -> dict:
    if search is None:
        return dict(
            name="", enabled=True, scope="local", location="", require_shipping=False, max_price="",
            excluded_models="", excluded_words="", required_keywords="",
            hard_criteria="", soft_criteria="", watched_models="",
            blocket_queries="", tradera_queries="",
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
        blocket_queries=_blocket_queries_to_text(search.blocket_queries),
        tradera_queries=_tradera_queries_to_text(search.tradera_queries),
    )


def _form_to_search(
    name: str,
    enabled: Optional[str],
    scope: str,
    location: str,
    require_shipping: Optional[str],
    max_price: str,
    excluded_models: str,
    excluded_words: str,
    required_keywords: str,
    hard_criteria: str,
    soft_criteria: str,
    watched_models: str,
    blocket_queries: str,
    tradera_queries: str,
) -> Search:
    return Search(
        name=name,
        enabled=enabled is not None,
        scope=scope,
        location=location,
        require_shipping=require_shipping is not None,
        max_price=int(max_price) if max_price.strip() else None,
        excluded_models=_lines_to_list(excluded_models),
        excluded_words=_lines_to_list(excluded_words),
        required_keywords=_lines_to_list(required_keywords),
        hard_criteria=_lines_to_list(hard_criteria),
        soft_criteria=_lines_to_list(soft_criteria),
        watched_models=_parse_watched_models(watched_models),
        blocket_queries=_parse_blocket_queries(blocket_queries),
        tradera_queries=_parse_tradera_queries(tradera_queries),
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
        {"form": _search_to_form(None), "is_edit": False, "action_url": "/searches/new"},
    )


@router.post("/searches/new")
def create_search(
    request: Request,
    name: str = Form(...),
    enabled: Optional[str] = Form(None),
    scope: str = Form("local"),
    location: str = Form(""),
    require_shipping: Optional[str] = Form(None),
    max_price: str = Form(""),
    excluded_models: str = Form(""),
    excluded_words: str = Form(""),
    required_keywords: str = Form(""),
    hard_criteria: str = Form(""),
    soft_criteria: str = Form(""),
    watched_models: str = Form(""),
    blocket_queries: str = Form(""),
    tradera_queries: str = Form(""),
):
    conn = request.app.state.conn
    search = _form_to_search(
        name, enabled, scope, location, require_shipping, max_price,
        excluded_models, excluded_words, required_keywords,
        hard_criteria, soft_criteria, watched_models, blocket_queries, tradera_queries,
    )
    searches_repo.create_search(conn, search)
    return RedirectResponse("/searches", status_code=303)


@router.get("/searches/{search_id}/edit", response_class=HTMLResponse)
def edit_search_form(request: Request, search_id: int):
    conn = request.app.state.conn
    search = searches_repo.get_search(conn, search_id)
    return templates.TemplateResponse(
        request, "search_form.html",
        {"form": _search_to_form(search), "is_edit": True, "action_url": f"/searches/{search_id}/edit"},
    )


@router.post("/searches/{search_id}/edit")
def update_search(
    request: Request,
    search_id: int,
    name: str = Form(...),
    enabled: Optional[str] = Form(None),
    scope: str = Form("local"),
    location: str = Form(""),
    require_shipping: Optional[str] = Form(None),
    max_price: str = Form(""),
    excluded_models: str = Form(""),
    excluded_words: str = Form(""),
    required_keywords: str = Form(""),
    hard_criteria: str = Form(""),
    soft_criteria: str = Form(""),
    watched_models: str = Form(""),
    blocket_queries: str = Form(""),
    tradera_queries: str = Form(""),
):
    conn = request.app.state.conn
    search = _form_to_search(
        name, enabled, scope, location, require_shipping, max_price,
        excluded_models, excluded_words, required_keywords,
        hard_criteria, soft_criteria, watched_models, blocket_queries, tradera_queries,
    )
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
    settings = request.app.state.settings
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
        stale_after = settings.poll_interval_minutes * 3
        if age_minutes > stale_after:
            return JSONResponse(
                {"status": "unhealthy", "reason": f"last run finished {int(age_minutes)} min ago, expected within {stale_after} min"},
                status_code=503,
            )

    return JSONResponse({"status": "ok", "last_run_finished_at": finished_at}, status_code=200)
