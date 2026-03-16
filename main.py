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


from fastapi import Request, HTTPException
from fastapi.responses import JSONResponse

# Этот роут ловит любые пути и любые методы, которые не совпали с ручками выше (типа /sessions)
@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy_engine(request: Request, path: str):
    
    # 1. Извлекаем заголовок (в FastAPI ключи заголовков всегда приводятся к нижнему регистру)
    session_id = request.headers.get("x-session-id")
    
    if not session_id:
        # Если автотест забыл передать сессию — сразу отбиваем
        raise HTTPException(status_code=401, detail="X-Session-ID header is missing")

    # 2. Проверяем валидность сессии в Redis (тот самый Слой Надежности)
    redis_key = f"session:{session_id}:active"
    is_active = await redis_client.get(redis_key) # Вернет "true" или None
    
    if is_active is None:
        # Если в Redis пусто (nil) — сессия протухла или не существует
        raise HTTPException(status_code=401, detail="Session is invalid or expired")

    # 3. Если мы здесь, значит сессия валидна! 
    # В будущем тут будет поход в MongoDB за правилами и сохранением состояния (Шаги 6-12 с диаграммы).
    # Пока вернем тестовый ответ:
    return JSONResponse(
        status_code=200,
        content={
            "message": "Прокси-сервер успешно проверил сессию в Redis!",
            "requested_path": f"/{path}",
            "method": request.method,
            "session_id": session_id
        }
    )