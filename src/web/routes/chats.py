from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse

from src import accounts as accounts_mod
from src import storage
from src.web.app import WebDeps, get_deps, templates

router = APIRouter(prefix="/chats", tags=["chats"])


@router.get("", response_class=HTMLResponse)
async def list_chats(
    request: Request,
    account_id: int | None = Query(default=None),
    deps: WebDeps = Depends(get_deps),
) -> HTMLResponse:
    accounts_list = accounts_mod.list_accounts(deps.config.db_path)
    resolved_id = account_id
    if resolved_id is None:
        active = next((a for a in accounts_list if a.is_active), None)
        resolved_id = active.id if active is not None else (
            accounts_list[0].id if accounts_list else None
        )
    contacts: list[dict[str, object]] = []
    selected = None
    if resolved_id is not None:
        contacts = storage.get_all_contacts(
            deps.config.db_path, account_id=resolved_id
        )
        selected = next(
            (a for a in accounts_list if a.id == resolved_id), None
        )
    return templates.TemplateResponse(
        request,
        "chat_list.html",
        {
            "accounts": accounts_list,
            "selected_account": selected,
            "contacts": contacts,
            "status": deps.bot_manager.status(),
        },
    )


@router.get("/{chat_id}", response_class=HTMLResponse)
async def chat_detail(
    chat_id: int,
    request: Request,
    account_id: int = Query(...),
    deps: WebDeps = Depends(get_deps),
) -> HTMLResponse:
    accounts_list = accounts_mod.list_accounts(deps.config.db_path)
    account = next((a for a in accounts_list if a.id == account_id), None)
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")

    messages = storage.get_recent_messages(
        deps.config.db_path, account_id=account_id, chat_id=chat_id, limit=100
    )
    contacts = storage.get_all_contacts(deps.config.db_path, account_id=account_id)
    contact = next((c for c in contacts if c["chat_id"] == chat_id), None)
    state = storage.get_contact_state(deps.config.db_path, account_id, chat_id)
    memory = storage.get_contact_memory(deps.config.db_path, account_id, chat_id)

    return templates.TemplateResponse(
        request,
        "chat_detail.html",
        {
            "account": account,
            "chat_id": chat_id,
            "contact": contact,
            "messages": messages,
            "state": state,
            "memory": memory,
            "status": deps.bot_manager.status(),
        },
    )
