# database.py
from motor.motor_asyncio import AsyncIOMotorClient

# Адрес базы данных (так как мы подняли её в Docker на 27017, стучимся туда)
MONGO_URL = "mongodb://localhost:27017"

client = AsyncIOMotorClient(MONGO_URL)
# Выбираем базу данных (она создастся автоматически при первой записи)
db = client["mock_engine_db"]

# Удобная ссылка на коллекции
sessions_collection = db["sessions"]