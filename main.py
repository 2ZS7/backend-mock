# main.py
from fastapi import FastAPI
from models.session import SessionCreate, SessionModel
from database import sessions_collection, redis_client

app = FastAPI(title="Stateful Mock Engine")

@app.post("/sessions", response_model=SessionModel)
async def create_session(session_in: SessionCreate):
    # 1. Создаем объект модели
    new_session = SessionModel(**session_in.model_dump())
    
    # 2. Сохраняем в MongoDB
    session_dict = new_session.model_dump(by_alias=True)
    await sessions_collection.insert_one(session_dict)
    
    # 3. Сохраняем в Redis "горячий" флаг активности (Ключ, Время жизни в секундах, Значение)
    redis_key = f"session:{new_session.id}:active"
    await redis_client.setex(redis_key, 7200, "true")
    
    # 4. Возвращаем модель клиенту
    return new_session