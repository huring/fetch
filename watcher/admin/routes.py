"""Admin UI routes: container CRUD and health status, server-rendered (no JS)."""
from __future__ import annotations

import datetime
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from watcher import containers as containers_repo
from watcher import storage
from watcher.models import BlocketQuery, Container, TraderaQuery, WatchedModel

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


def _container_to_form(container: Optional[Container]) -> dict:
    if container is None:
        return dict(
            name="", enabled=True, scope="local", location="", require_shipping=False, max_price="",
            excluded_models="", excluded_words="", required_keywords="",
            hard_criteria="", soft_criteria="", watched_models="",
            blocket_queries="", tradera_queries="",
        )
    return dict(
        name=container.name,
        enabled=container.enabled,
        scope=container.scope,
        location=container.location,
        require_shipping=container.require_shipping,
        max_price=container.max_price if container.max_price is not None else "",
        excluded_models=_list_to_lines(container.excluded_models),
        excluded_words=_list_to_lines(container.excluded_words),
        required_keywords=_list_to_lines(container.required_keywords),
        hard_criteria=_list_to_lines(container.hard_criteria),
        soft_criteria=_list_to_lines(container.soft_criteria),
        watched_models=_watched_models_to_text(container.watched_models),
        blocket_queries=_blocket_queries_to_text(container.blocket_queries),
        tradera_queries=_tradera_queries_to_text(container.tradera_queries),
    )


def _form_to_container(
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
) -> Container:
    return Container(
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
    return RedirectResponse("/containers")


@router.get("/containers", response_class=HTMLResponse)
def list_containers(request: Request):
    conn = request.app.state.conn
    all_containers = containers_repo.list_containers(conn)
    return templates.TemplateResponse(request, "containers_list.html", {"containers": all_containers})


@router.get("/containers/new", response_class=HTMLResponse)
def new_container_form(request: Request):
    return templates.TemplateResponse(
        request, "container_form.html",
        {"form": _container_to_form(None), "is_edit": False, "action_url": "/containers/new"},
    )


@router.post("/containers/new")
def create_container(
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
    container = _form_to_container(
        name, enabled, scope, location, require_shipping, max_price,
        excluded_models, excluded_words, required_keywords,
        hard_criteria, soft_criteria, watched_models, blocket_queries, tradera_queries,
    )
    containers_repo.create_container(conn, container)
    return RedirectResponse("/containers", status_code=303)


@router.get("/containers/{container_id}/edit", response_class=HTMLResponse)
def edit_container_form(request: Request, container_id: int):
    conn = request.app.state.conn
    container = containers_repo.get_container(conn, container_id)
    return templates.TemplateResponse(
        request, "container_form.html",
        {"form": _container_to_form(container), "is_edit": True, "action_url": f"/containers/{container_id}/edit"},
    )


@router.post("/containers/{container_id}/edit")
def update_container(
    request: Request,
    container_id: int,
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
    container = _form_to_container(
        name, enabled, scope, location, require_shipping, max_price,
        excluded_models, excluded_words, required_keywords,
        hard_criteria, soft_criteria, watched_models, blocket_queries, tradera_queries,
    )
    containers_repo.update_container(conn, container_id, container)
    return RedirectResponse("/containers", status_code=303)


@router.post("/containers/{container_id}/toggle")
def toggle_container(request: Request, container_id: int):
    conn = request.app.state.conn
    container = containers_repo.get_container(conn, container_id)
    if container is not None:
        containers_repo.set_enabled(conn, container_id, not container.enabled)
    return RedirectResponse("/containers", status_code=303)


@router.post("/containers/{container_id}/delete")
def delete_container(request: Request, container_id: int):
    conn = request.app.state.conn
    containers_repo.delete_container(conn, container_id)
    return RedirectResponse("/containers", status_code=303)


@router.get("/health", response_class=HTMLResponse)
def health_page(request: Request):
    conn = request.app.state.conn
    last_run = storage.get_last_run(conn)
    monthly_cost = storage.monthly_cost_summary(conn)
    return templates.TemplateResponse(request, "health.html", {"last_run": last_run, "monthly_cost": monthly_cost})


@router.get("/healthz")
def healthz(request: Request) -> JSONResponse:
    """Machine-readable health check for Docker's HEALTHCHECK."""
    conn = request.app.state.conn
    settings = request.app.state.settings
    last_run = storage.get_last_run(conn)

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
