"""Admin UI routes: search CRUD, marketplace config, and health status -
server-rendered (no JS)."""
from __future__ import annotations

import datetime
import json
import sqlite3
import uuid
from pathlib import Path
from typing import Dict, List, Optional

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
from watcher.scoring import search_builder
from watcher.scoring.search_preview import estimate_result_counts

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
    top_listings = [
        dict(row, platform_name=getattr(get_marketplace(row["source"]), "display_name", row["source"]))
        for row in storage.get_top_listings(conn, settings.score_instant_threshold)
    ]
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
            "built_from_prompt": bool(search and search.creation_prompt),
            "search_id": search_id,
        },
    )


@router.post("/searches/{search_id}/edit")
async def update_search(request: Request, search_id: int):
    conn = request.app.state.conn
    form_data = await request.form()
    search = _form_to_search(form_data)
    searches_repo.update_search(conn, search_id, search)
    return RedirectResponse("/searches", status_code=303)


# --- NLP search builder (backlog #21) -----------------------------------
#
# A short back-and-forth driven turn-by-turn across page loads, same as
# every other route in this server-rendered (no JS) app - the running
# transcript and (mid-draft) the generated draft itself are round-tripped as
# hidden form fields rather than kept in any server-side session state. See
# watcher/scoring/search_builder.py's module docstring for why this is a
# handful of structured-output calls rather than Anthropic tool-use.

def _marketplace_names() -> Dict[str, str]:
    return {m.key: m.display_name for m in MARKETPLACES.values()}


def _transcript_from_form(form_data) -> List[Dict[str, str]]:
    """The first submission on a fresh wizard has no transcript yet - just
    the prompt textarea. Every later submission (answering a question) has
    one, plus the question being answered (carried separately as
    pending_question_json, since the "ask" screen only displays its text,
    not its JSON) and either a typed answer or the "skip" button."""
    transcript_json = form_data.get("transcript", "").strip()
    if not transcript_json:
        return [{"role": "user", "content": form_data.get("prompt", "").strip()}]
    transcript = json.loads(transcript_json)
    pending_question_json = form_data.get("pending_question_json", "")
    if pending_question_json:
        transcript.append({"role": "assistant", "content": pending_question_json})
    answer = (
        "(no preference - use your best judgement)"
        if form_data.get("skip")
        else form_data.get("answer", "").strip()
    )
    transcript.append({"role": "user", "content": answer})
    return transcript


def build_wizard_turn_context(
    conn, client, settings, search_id: Optional[int], transcript: List[Dict[str, str]]
) -> Dict[str, object]:
    """Runs one turn of the builder and returns the template context for
    whatever screen comes next (ask/draft/error) - a plain dict, not a
    response, since this now runs inside a background scheduler job (see
    app.py's trigger_wizard_turn) rather than directly in a request handler.
    That split is what actually fixes the 504: a slow Claude call no longer
    ties up an HTTP response long enough for a reverse proxy in front of
    this app to give up on it - the triggering request gets an immediate
    redirect to a status page instead (see _start_wizard_turn), which polls
    (plain meta-refresh, no JS) until this function's result is ready."""
    if client is None:
        return {"stage": "error", "error": "ANTHROPIC_API_KEY isn't configured, so the search builder can't call Claude.", "search_id": search_id}

    round_count = sum(1 for turn in transcript if turn["role"] == "assistant")
    force_propose = round_count >= search_builder.MAX_QUESTION_ROUNDS
    try:
        turn = search_builder.run_turn(conn, client, settings, transcript, force_propose=force_propose)
    except search_builder.SearchBuilderError as exc:
        return {"stage": "error", "error": str(exc), "search_id": search_id}

    if turn.action == "ask_user":
        return {
            "stage": "ask", "question": turn.ask_user.question, "search_id": search_id,
            "transcript_json": json.dumps(transcript), "pending_question_json": turn.model_dump_json(),
        }

    draft = turn.propose_search
    return {
        "stage": "draft", "draft": draft, "draft_json": draft.model_dump_json(),
        "transcript_json": json.dumps(transcript), "counts": None, "name_error": None,
        "name_value": draft.name, "checked_indices": set(range(len(draft.search_phrases))),
        "search_id": search_id, "marketplace_names": _marketplace_names(),
    }


def _start_wizard_turn(request: Request, search_id: Optional[int], transcript: List[Dict[str, str]]) -> HTMLResponse:
    if request.app.state.client is None:
        return templates.TemplateResponse(
            request, "search_prompt_wizard.html",
            {
                "stage": "error", "search_id": search_id,
                "error": "ANTHROPIC_API_KEY isn't configured, so the search builder can't call Claude.",
            },
        )
    job_id = uuid.uuid4().hex
    request.app.state.wizard_jobs[job_id] = {"status": "pending"}
    request.app.state.trigger_wizard_turn(job_id, search_id, transcript)
    return RedirectResponse(f"/searches/wizard/{job_id}", status_code=303)


@router.get("/searches/wizard/{job_id}", response_class=HTMLResponse)
def wizard_status(request: Request, job_id: str):
    job = request.app.state.wizard_jobs.get(job_id)
    if job is None:
        return RedirectResponse("/searches", status_code=303)
    if job["status"] == "pending":
        return templates.TemplateResponse(request, "search_prompt_wizard.html", {"stage": "generating"})
    # Consumed on first successful read - this is in-memory, request-scoped
    # state for one drafting session, not anything worth keeping around
    # (and the "ask"/"draft" screen that follows carries everything needed
    # to continue in its own hidden fields, not this job_id).
    request.app.state.wizard_jobs.pop(job_id, None)
    return templates.TemplateResponse(request, "search_prompt_wizard.html", job["context"])


def _handle_draft_action(request: Request, form_data, search_id: Optional[int]) -> HTMLResponse:
    conn = request.app.state.conn
    settings = request.app.state.settings
    draft = search_builder.ProposeSearch.model_validate_json(form_data.get("draft_json"))
    name = (form_data.get("name") or draft.name).strip() or draft.name
    checked_indices = {int(i) for i in form_data.getlist("phrase_included")}
    included_phrases = [p.text for i, p in enumerate(draft.search_phrases) if i in checked_indices]
    transcript_json = form_data.get("transcript", "[]")

    search = search_builder.draft_to_search(draft, included_phrases=included_phrases)
    search.name = name

    if form_data.get("do") == "preview":
        counts = estimate_result_counts(settings, search)
        return templates.TemplateResponse(
            request, "search_prompt_wizard.html",
            {
                "stage": "draft", "draft": draft, "draft_json": form_data.get("draft_json"),
                "transcript_json": transcript_json, "counts": counts, "name_error": None,
                "name_value": name, "checked_indices": checked_indices,
                "search_id": search_id, "marketplace_names": _marketplace_names(),
            },
        )

    search.creation_prompt = search_builder.collapse_transcript_to_prompt(json.loads(transcript_json))
    try:
        if search_id is None:
            searches_repo.create_search(conn, search)
        else:
            searches_repo.update_search(conn, search_id, search)
    except sqlite3.IntegrityError:
        return templates.TemplateResponse(
            request, "search_prompt_wizard.html",
            {
                "stage": "draft", "draft": draft, "draft_json": form_data.get("draft_json"),
                "transcript_json": transcript_json, "counts": None,
                "name_error": f'A search named "{name}" already exists - choose a different name.',
                "name_value": name, "checked_indices": checked_indices,
                "search_id": search_id, "marketplace_names": _marketplace_names(),
            },
        )
    return RedirectResponse("/searches", status_code=303)


@router.get("/searches/new-from-prompt", response_class=HTMLResponse)
def new_search_from_prompt_form(request: Request):
    return templates.TemplateResponse(
        request, "search_prompt_wizard.html", {"stage": "start", "search_id": None, "existing_prompt": ""}
    )


@router.post("/searches/new-from-prompt")
async def new_search_from_prompt_turn(request: Request):
    form_data = await request.form()
    return _start_wizard_turn(request, None, _transcript_from_form(form_data))


@router.post("/searches/new-from-prompt/draft")
async def new_search_from_prompt_draft(request: Request):
    form_data = await request.form()
    return _handle_draft_action(request, form_data, None)


@router.get("/searches/{search_id}/edit-from-prompt", response_class=HTMLResponse)
def edit_search_from_prompt_form(request: Request, search_id: int):
    conn = request.app.state.conn
    search = searches_repo.get_search(conn, search_id)
    if search is None:
        return RedirectResponse("/searches", status_code=303)
    return templates.TemplateResponse(
        request, "search_prompt_wizard.html",
        {"stage": "start", "search_id": search_id, "existing_prompt": search.creation_prompt or ""},
    )


@router.post("/searches/{search_id}/edit-from-prompt")
async def edit_search_from_prompt_turn(request: Request, search_id: int):
    form_data = await request.form()
    return _start_wizard_turn(request, search_id, _transcript_from_form(form_data))


@router.post("/searches/{search_id}/edit-from-prompt/draft")
async def edit_search_from_prompt_draft(request: Request, search_id: int):
    form_data = await request.form()
    return _handle_draft_action(request, form_data, search_id)


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
