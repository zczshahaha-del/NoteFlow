from fastapi import APIRouter

from app.services import diagnostics

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict[str, bool]:
    postgres = await diagnostics.check_postgres()
    return {"ok": postgres["status"] == "ok"}
