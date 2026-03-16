# database.py
from motor.motor_asyncio import AsyncIOMotorClient
import redis.asyncio as redis # Используем современный асинхронный клиент

# --- MongoDB ---
MONGO_URL = "mongodb://localhost:27017"
mongo_client = AsyncIOMotorClient(MONGO_URL)
db = mongo_client["mock_engine_db"]
sessions_collection = db["sessions"]
definitions_collection = db["definitions"]

# --- Redis ---
REDIS_URL = "redis://localhost:6379"
# decode_responses=True означает, что Redis будет возвращать обычные строки, а не байты
redis_client = redis.from_url(REDIS_URL, decode_responses=True)