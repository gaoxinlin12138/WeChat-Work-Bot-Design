from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse, RedirectResponse

router = APIRouter(include_in_schema=False)
STATIC_DIR = Path(__file__).parent / "static"


@router.get("/")
async def home() -> RedirectResponse:
    return RedirectResponse("/admin")


@router.get("/admin")
async def admin_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")
