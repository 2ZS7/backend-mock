from fastapi import FastAPI

from models.session import SessionCreate, SessionModel

app = FastAPI(title="Stateful Mock Engine")

@app.post("/sessions", response_model=SessionModel)
async def create_session(session_in: SessionCreate):
    # Превращаем входные данные в полноценную модель БД
    new_session = SessionModel(**session_in.model_dump())
    
    # TODO: Позже мы добавим сохранение new_session.model_dump(by_alias=True) в MongoDB
    # TODO: Позже мы добавим сохранение кэша в Redis
    
    return new_session