"""Shared email preference endpoints; the desktop bridge uses the same service."""
from fastapi import APIRouter, Depends, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from sqlalchemy.orm import Session
from app.models.base import get_db

from app.services.email_settings_service import (
    EmailPreferencesUpdate, public_config, save_config, test_saved_config,
)

class EmailPreferencesRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def without_credential_echo(request):
            try:
                return await handler(request)
            except RequestValidationError:
                # FastAPI's default validation detail includes the submitted input.
                raise HTTPException(
                    status_code=422,
                    detail="邮箱配置格式不正确，请检查邮箱服务、端口和授权码长度",
                ) from None

        return without_credential_echo


router = APIRouter(route_class=EmailPreferencesRoute)


@router.get("")
def get_email_preferences(db: Session = Depends(get_db)):
    return public_config(db)


@router.put("")
def update_email_preferences(request: EmailPreferencesUpdate, db: Session = Depends(get_db)):
    try:
        return save_config(db, request)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/test")
def test_email_preferences(db: Session = Depends(get_db)):
    return test_saved_config(db)
