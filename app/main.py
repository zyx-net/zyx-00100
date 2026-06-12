from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import logging
import sys

from .db import init_db, SessionLocal
from .config import settings
from .api.routes import router as api_router
from .seed import seed_users

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="共享会议室预订冲突仲裁 API",
    description=(
        "基于事件溯源的会议室预订系统。支持预订、审批、改期、取消、签到、释放和仲裁。"
        "所有状态变化写入事件历史，通过版本号阻止并发旧写入。当前日程可由事件重放得到。"
        f"\n\n当前规则版本: **{settings.rule_version}**"
    ),
    version=settings.rule_version,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*", "X-Actor-Id", "X-Actor-Role", "X-Actor-Name"],
)


@app.on_event("startup")
def on_startup():
    init_db()
    db = SessionLocal()
    try:
        created = seed_users(db)
        if created > 0:
            logger.info(f"已初始化 {created} 个用户")
    finally:
        db.close()
    logger.info(f"会议室预订系统启动完成，规则版本: {settings.rule_version}")
    logger.info(f"已配置 {len(settings.rooms)} 个房间")


@app.get("/", tags=["根"])
def root():
    return {
        "name": "共享会议室预订冲突仲裁 API",
        "version": settings.rule_version,
        "docs": "/docs",
        "openapi": "/openapi.json",
        "rooms_count": len(settings.rooms),
    }


app.include_router(api_router)
