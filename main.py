# main.py
from fastapi import FastAPI
from models.session import SessionCreate, SessionModel
from database import sessions_collection

app = FastAPI(title="Stateful Mock Engine")

@app.post("/sessions", response_model=SessionModel)
async def create_session(session_in: SessionCreate):
    # 1. Создаем объект модели
    new_session = SessionModel(**session_in.model_dump())
    
    # 2. Превращаем модель в словарь для MongoDB (by_alias=True меняет 'id' на '_id')
    session_dict = new_session.model_dump(by_alias=True)
    
    # 3. Асинхронно записываем в базу
    await sessions_collection.insert_one(session_dict)
    
    # 4. Возвращаем модель клиенту
    return new_session