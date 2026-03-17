import re
from fastapi import FastAPI
from models.session import SessionCreate, SessionModel
from database import sessions_collection, redis_client, virtual_state_collection

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


from models.definition import DefinitionCreate, DefinitionModel
from database import definitions_collection

@app.post("/definitions", response_model=DefinitionModel)
async def create_definition(def_in: DefinitionCreate):
    new_def = DefinitionModel(**def_in.model_dump())
    await definitions_collection.insert_one(new_def.model_dump(by_alias=True))
    return new_def


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
    # PRIORITY MATCHING ENGINE
    
    # 1. Ищем в базе правила для текущего метода (и для универсального "ANY")
    # Сортируем по priority по убыванию (-1)
    cursor = definitions_collection.find({"method": {"$in": [request.method, "ANY"]}})
    cursor.sort("priority", -1)
    rules = await cursor.to_list(length=100) # Берем топ-100 правил

    matched_rule = None
    
    # 2. Проверяем регулярные выражения
    for rule in rules:
        # re.match проверяет, подходит ли запрошенный path под шаблон из базы
        if re.match(rule["path_pattern"], path):
            matched_rule = rule
            break # Нашли самое приоритетное совпадение — останавливаемся!

    # 3. Если правило не найдено (тот самый Fallback)
    if not matched_rule:
        raise HTTPException(status_code=404, detail=f"No mock rule found for {request.method} /{path}")
    
    # STATEFUL ЛОГИКА (Работа с виртуальной БД)

    state_logic = matched_rule.get("state_logic")
    
    if state_logic:
        action = state_logic.get("action")
        collection_name = state_logic.get("collection_name")
        
        # СЦЕНАРИЙ А: Сохраняем данные (POST)
        if action == "insert":
            body = await request.json() # Читаем тело запроса от теста
            
            # Формируем документ для виртуальной БД
            virtual_doc = {
                "session_id": session_id,
                "entity_type": collection_name,
                "payload": body
            }
            await virtual_state_collection.insert_one(virtual_doc)
            
            return JSONResponse(status_code=matched_rule["status_code"], content={"message": "Saved successfully", "data": body})

        # СЦЕНАРИЙ Б: Отдаем сохраненные данные (GET)
        elif action == "find":
            # Ищем ТОЛЬКО те данные, которые принадлежат этой сессии и этому типу!
            cursor = virtual_state_collection.find({
                "session_id": session_id,
                "entity_type": collection_name
            })
            
            saved_docs = await cursor.to_list(length=100)
            # Вытаскиваем только payload, чтобы отдать чистые данные
            result_data = [doc["payload"] for doc in saved_docs]
            
            return JSONResponse(status_code=matched_rule["status_code"], content=result_data)

    # 4. Если state_logic нет, работаем по-старому (Stateless)
    return JSONResponse(
        status_code=matched_rule["status_code"],
        content=matched_rule.get("response_payload", {})
    )