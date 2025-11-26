from fastapi import APIRouter

from app.routers.settings.account import router as account_router
from app.routers.settings.download import router as download_router
from app.routers.settings.notification import router as notification_router
from app.routers.settings.audiobookshelf import router as abs_router
from app.routers.settings.security import router as security_router
from app.routers.settings.users import router as users_router
from app.routers.settings.ai import router as ai_router
from app.routers.settings.mam import router as mam_router
from app.routers.settings.qbittorrent import router as qbittorrent_router


router = APIRouter(prefix="/settings")

router.include_router(account_router)
router.include_router(download_router)
router.include_router(notification_router)
router.include_router(abs_router)
router.include_router(security_router)
router.include_router(users_router)
router.include_router(ai_router)
router.include_router(mam_router)
router.include_router(qbittorrent_router)
