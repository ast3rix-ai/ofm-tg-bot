from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from src import storage
from src.web.app import WebDeps, get_deps, templates

router = APIRouter(prefix="/alerts", tags=["alerts"])


@router.get("", response_class=HTMLResponse)
async def list_alerts(
    request: Request,
    account_id: int | None = Query(default=None),
    only_unack: bool = Query(default=False),
    type_filter: str | None = Query(default=None, alias="type"),
    deps: WebDeps = Depends(get_deps),
) -> HTMLResponse:
    rows = storage.list_operator_alerts(
        deps.config.db_path,
        account_id=account_id,
        only_unacknowledged=only_unack,
        limit=200,
    )
    if type_filter:
        rows = [r for r in rows if r["alert_type"] == type_filter]
    types_seen = sorted({r["alert_type"] for r in rows})
    return templates.TemplateResponse(
        request,
        "alerts_list.html",
        {
            "alerts": rows,
            "account_id": account_id,
            "only_unack": only_unack,
            "type_filter": type_filter,
            "types": types_seen,
            "status": deps.bot_manager.status(),
        },
    )


@router.post("/{alert_id}/ack")
async def acknowledge(
    alert_id: int,
    deps: WebDeps = Depends(get_deps),
) -> RedirectResponse:
    storage.acknowledge_operator_alert(deps.config.db_path, alert_id)
    return RedirectResponse(url="/alerts", status_code=303)
