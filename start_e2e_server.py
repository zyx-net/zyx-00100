import os
os.environ["APP_DATABASE_URL"] = "sqlite:///./e2e_fresh.db"
from app.db import engine
from app.config import settings
print(f"Using DB: {settings.database_url}")
print(f"Engine URL: {engine.url}")

import uvicorn
uvicorn.run("app.main:app", host="0.0.0.0", port=8005, log_level="info")
