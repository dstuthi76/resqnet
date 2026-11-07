"""Settings, read from environment variables or a `.env` file in the repo root."""
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")


def _load_dotenv():
    path = os.path.join(ROOT, ".env")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_dotenv()


def env(name, default=""):
    return os.environ.get(name, default)


DB_PATH = env("DB_PATH", os.path.join(ROOT, "resqnet.db"))

# AI
GEMINI_API_KEY = env("GEMINI_API_KEY")
GEMINI_MODEL = env("GEMINI_MODEL", "gemini-2.5-flash")
GEMINI_EMBED_MODEL = env("GEMINI_EMBED_MODEL", "gemini-embedding-001")
GEMINI_BATCH = int(env("GEMINI_BATCH", "25"))

# Blockchain: "sim" (built-in simulator, no keys) or "sepolia"
CHAIN_MODE = env("CHAIN_MODE", "sim").lower()
SEPOLIA_RPC_URL = env("SEPOLIA_RPC_URL")
ORACLE_PRIVATE_KEY = env("ORACLE_PRIVATE_KEY")          # deployer / backend oracle wallet
CONTRACT_ADDRESS = env("CONTRACT_ADDRESS")
NGO_PRIVATE_KEYS = [k.strip() for k in env("NGO_PRIVATE_KEYS").split(",") if k.strip()]
EXPLORER_TX_URL = env("EXPLORER_TX_URL", "https://sepolia.etherscan.io/tx/")
EXPLORER_ADDR_URL = env("EXPLORER_ADDR_URL", "https://sepolia.etherscan.io/address/")

# Payout rule (must match the contract)
PAYOUT_WEI = int(env("PAYOUT_WEI", str(10**15)))         # 0.001 ETH
HOURS_THRESHOLD = int(env("HOURS_THRESHOLD", "48"))
CONFIRMATIONS_REQUIRED = int(env("CONFIRMATIONS_REQUIRED", "2"))
WAITING_PERIOD_S = int(env("WAITING_PERIOD_S", "0"))  # must match contract waitingPeriod
DEFAULT_EVENT_ID = env("DEFAULT_EVENT_ID", "HYD-2020-10")

# Privacy
ID_SALT = env("ID_SALT", "change-me-in-production")

# Optional integrations
PINATA_JWT = env("PINATA_JWT")
ADMIN_TOKEN = env("ADMIN_TOKEN")  # if set, demo reset/load endpoints require ?token=
