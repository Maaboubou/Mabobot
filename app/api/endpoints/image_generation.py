"""Image configuration console; image calls themselves run on the host."""
import base64
import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.models.base import get_db
from app.services.image_generation_config import (
    ImageConfigError, ImageConfigConflict, read_settings, public_settings, update_settings,
)
from app.services.image_generation_service import ImageRequest, ImageGenerationError, generate_images
from app.services.image_generation_history import ImageReceipt, read_history
from app.services.image_generation_media import image_mime
from app.services.image_model_discovery import DiscoveryRequest, discover_models

class ImageConfigRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def without_credential_echo(request):
            try:
                return await handler(request)
            except RequestValidationError:
                # Validation errors can include entire submitted connection values.
                raise HTTPException(422, "图片配置格式不正确，请检查地址、密钥和参数") from None

        return without_credential_echo


router = APIRouter(route_class=ImageConfigRoute)


@router.get("")
def get_config(db: Session = Depends(get_db)):
    try:
        return public_settings(read_settings(db))
    except ImageConfigError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.put("")
def save_config(payload: dict, db: Session = Depends(get_db)):
    try:
        return update_settings(db, payload)
    except ImageConfigConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except ImageConfigError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/history")
def history(db: Session = Depends(get_db)):
    return {"runs": read_history(db)}


@router.post("/models/discover")
def model_catalog(request: DiscoveryRequest, db: Session = Depends(get_db)):
    try:
        return discover_models(request, db)
    except ImageConfigError as exc:
        raise HTTPException(422, str(exc)) from exc


class TestRequest(BaseModel):
    prompt: str = Field(default="A small blue ceramic cup on a white background", min_length=1, max_length=32000)


@router.post("/{channel_id}/test")
def test_channel(channel_id: str, request: TestRequest, db: Session = Depends(get_db)):
    channel = next((c for c in read_settings(db)["channels"] if c["id"] == channel_id), None)
    if channel is None:
        raise HTTPException(404, "渠道不存在")
    receipt = ImageReceipt("控制台测试")
    try:
        with tempfile.TemporaryDirectory(prefix="mabobot-image-test-") as directory:
            result = generate_images({"mode": "api", "channels": [{**channel, "enabled": True}]},
                                     ImageRequest(request.prompt, count=1, timeout=channel["timeout"]), Path(directory),
                                     progress=receipt.progress)
            path = result.paths[0]
            receipt.finish("completed")
            return {"status": "completed", "attempts": result.attempts,
                    "preview": f"data:{image_mime(path.suffix[1:])};base64," + base64.b64encode(path.read_bytes()).decode()}
    except ImageGenerationError as exc:
        receipt.finish("failed")
        raise HTTPException(422, {"message": str(exc), "attempts": exc.attempts}) from exc
