"""Env-driven config. Fails fast on missing required vars."""
import os
import logging
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

REQUIRED = ["VENDORHUB_USERNAME", "VENDORHUB_PASSWORD", "VENDORHUB_BASE_URL"]
missing = [k for k in REQUIRED if not os.getenv(k)]
if missing:
    raise RuntimeError(f"Missing required env vars: {missing}")

USERNAME = os.environ["VENDORHUB_USERNAME"]
PASSWORD = os.environ["VENDORHUB_PASSWORD"]
BASE_URL = os.environ["VENDORHUB_BASE_URL"].rstrip("/")
SESSION_STATE_PATH = Path(os.getenv("SESSION_STATE_PATH", "./secrets/session_state.json"))
SESSION_ENC_KEY = os.getenv("SESSION_ENC_KEY", "")
HEADLESS = os.getenv("HEADLESS", "true").lower() == "true"
CAPSOLVER_API_KEY = os.getenv("CAPSOLVER_API_KEY", "").strip()
LOOKBACK_DAYS = int(os.getenv("LOOKBACK_DAYS", "60"))

TENANT_ID = os.getenv("TENANT_ID", "")
CLIENT_ID = os.getenv("CLIENT_ID", "")
CLIENT_SECRET = os.getenv("CLIENT_SECRET", "")
SCOPE = os.getenv("SCOPE", "https://graph.microsoft.com/.default")
MAILBOX_USER = os.getenv("MAILBOX_USER", "")
NOTIFICATION_EMAIL = os.getenv(
    "NOTIFICATION_EMAIL",
    "siddhant.pardhe@glidebrands.in,Shahana@glidebrands.in,Rahul@glidebrands.in,Akash.Jaiswar@glidebrands.in",
)

DB_CONFIG = dict(
    host=os.getenv("DB_HOST", "holistique-middleware.c9wdjmzy25ra.ap-south-1.rds.amazonaws.com"),
    user=os.getenv("DB_USER", "Siddhanth"),
    password=os.getenv("DB_PASSWORD", "Siddhanth@#4321"),
    database=os.getenv("DB_NAME", "Holistique"),
    charset="utf8mb4",
    autocommit=False,
    connect_timeout=30,
    read_timeout=300,
    write_timeout=300,
)

SESSION_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
