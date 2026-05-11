from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from src import accounts as accounts_mod
from src.accounts import AccountsError
from src.web.app import WebDeps, get_deps, templates

router = APIRouter(prefix="/accounts", tags=["accounts"])


@router.get("", response_class=HTMLResponse)
async def list_accounts(request: Request, deps: WebDeps = Depends(get_deps)) -> HTMLResponse:
    rows = accounts_mod.list_accounts(deps.config.db_path)
    status = deps.bot_manager.status()
    return templates.TemplateResponse(
        request,
        "accounts_list.html",
        {
            "accounts": rows,
            "status": status,
        },
    )


@router.get("/new", response_class=HTMLResponse)
async def new_account_form(request: Request, _: WebDeps = Depends(get_deps)) -> HTMLResponse:
    return templates.TemplateResponse(request, "account_add.html", {})


@router.post("")
async def create_account(
    label: str = Form(..., min_length=1, max_length=64),
    api_id: int = Form(...),
    api_hash: str = Form(..., min_length=1),
    phone: str = Form(..., min_length=1),
    deps: WebDeps = Depends(get_deps),
) -> RedirectResponse:
    try:
        account_id = accounts_mod.create_account(
            deps.config.db_path,
            deps.config.session_encryption_key,
            label=label.strip(),
            api_id=api_id,
            api_hash=api_hash.strip(),
            phone=phone.strip(),
        )
    except AccountsError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RedirectResponse(url=f"/accounts/{account_id}/auth", status_code=303)


@router.get("/{account_id}", response_class=HTMLResponse)
async def account_detail(
    account_id: int,
    request: Request,
    deps: WebDeps = Depends(get_deps),
) -> HTMLResponse:
    account = accounts_mod.get_account(
        deps.config.db_path, deps.config.session_encryption_key, account_id
    )
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")
    from src import storage

    events = storage.get_recent_events(
        deps.config.db_path, limit=50, account_id=account_id
    )
    contacts = storage.get_all_contacts(deps.config.db_path, account_id=account_id)
    return templates.TemplateResponse(
        request,
        "account_detail.html",
        {
            "account": account,
            "events": events,
            "contacts": contacts,
            "status": deps.bot_manager.status(),
        },
    )


@router.get("/{account_id}/auth", response_class=HTMLResponse)
async def auth_page(
    account_id: int,
    request: Request,
    deps: WebDeps = Depends(get_deps),
) -> HTMLResponse:
    account = accounts_mod.get_account(
        deps.config.db_path, deps.config.session_encryption_key, account_id
    )
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")
    return templates.TemplateResponse(
        request,
        "account_auth.html",
        {
            "account": account,
            "status": deps.bot_manager.status(),
        },
    )


@router.post("/{account_id}/activate")
async def activate(
    account_id: int,
    deps: WebDeps = Depends(get_deps),
) -> RedirectResponse:
    account = accounts_mod.get_account(
        deps.config.db_path, deps.config.session_encryption_key, account_id
    )
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")

    # If unauthenticated, defer to the auth page which kicks off activation
    # in the background and polls the manager.
    if not account.has_session:
        return RedirectResponse(url=f"/accounts/{account_id}/auth", status_code=303)

    asyncio.create_task(_activate_bg(deps, account_id))
    return RedirectResponse(url="/accounts", status_code=303)


@router.post("/{account_id}/auth/start")
async def start_auth(
    account_id: int,
    deps: WebDeps = Depends(get_deps),
) -> RedirectResponse:
    """Kick off activation in the background; UI polls for code prompts."""
    asyncio.create_task(_activate_bg(deps, account_id))
    return RedirectResponse(url=f"/accounts/{account_id}/auth", status_code=303)


@router.post("/{account_id}/auth/code")
async def submit_code(
    account_id: int,
    code: str = Form(..., min_length=1),
    deps: WebDeps = Depends(get_deps),
) -> RedirectResponse:
    if deps.bot_manager.active_account_id != account_id:
        raise HTTPException(status_code=400, detail="Account is not awaiting code")
    if not deps.bot_manager.submit_code(code.strip()):
        raise HTTPException(status_code=400, detail="No code prompt is open")
    return RedirectResponse(url=f"/accounts/{account_id}/auth", status_code=303)


@router.post("/{account_id}/auth/password")
async def submit_password(
    account_id: int,
    password: str = Form(..., min_length=1),
    deps: WebDeps = Depends(get_deps),
) -> RedirectResponse:
    if deps.bot_manager.active_account_id != account_id:
        raise HTTPException(status_code=400, detail="Account is not awaiting password")
    if not deps.bot_manager.submit_password(password):
        raise HTTPException(status_code=400, detail="No password prompt is open")
    return RedirectResponse(url=f"/accounts/{account_id}/auth", status_code=303)


@router.post("/{account_id}/deactivate")
async def deactivate(
    _account_id: int,
    deps: WebDeps = Depends(get_deps),
) -> RedirectResponse:
    asyncio.create_task(deps.bot_manager.deactivate())
    return RedirectResponse(url="/accounts", status_code=303)


@router.post("/{account_id}/delete")
async def delete_account(
    account_id: int,
    deps: WebDeps = Depends(get_deps),
) -> RedirectResponse:
    if deps.bot_manager.active_account_id == account_id:
        await deps.bot_manager.deactivate()
    accounts_mod.delete_account(deps.config.db_path, account_id)
    return RedirectResponse(url="/accounts", status_code=303)


async def _activate_bg(deps: WebDeps, account_id: int) -> None:
    try:
        await deps.bot_manager.activate(account_id)
    except Exception:  # noqa: BLE001
        # Surface in status(); already logged + alerted inside the manager.
        return
