import re
from urllib import request
from fastapi import FastAPI, Request, HTTPException, BackgroundTasks, Path
from models.session import SessionCreate, SessionModel
from datetime import datetime, timezone
from database import sessions_collection, redis_client, virtual_state_collection, definitions_collection, request_logs_collection
from models.definition import DefinitionCreate, DefinitionModel
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="Stateful Mock Engine")

# --- НАСТРОЙКА CORS ---
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], # В реальном проде тут будет URL фронтенда, но для ВКР ставим "*"
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


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


@app.get("/sessions", response_model=list[SessionModel])
async def get_sessions():
    """Получить список всех сессий для Dashboard"""
    cursor = sessions_collection.find()
    # Сортируем от новых к старым
    cursor.sort("created_at", -1)
    sessions = await cursor.to_list(length=100)
    return sessions


@app.delete("/sessions/{session_id}")
async def finish_session(session_id: str):
    # 1. Меняем статус в MongoDB
    await sessions_collection.update_one(
        {"_id": session_id}, {"$set": {"status": "finished"}}
    )
    # 2. Удаляем из Redis (тест больше не сможет обращаться к прокси)
    await redis_client.delete(f"session:{session_id}:active")
    return {"message": "Session finished"}


@app.get("/definitions", response_model=list[DefinitionModel])
async def get_definitions():
    cursor = definitions_collection.find()
    # to_list(length=100) возвращает список, это верно
    rules = await cursor.to_list(length=100) 
    
    # ПРОВЕРКА: если правил нет, верни пустой список, а не None
    return rules if rules is not None else []

@app.post("/definitions", response_model=DefinitionModel)
async def create_definition(def_in: DefinitionCreate):
    new_def = DefinitionModel(**def_in.model_dump())
    await definitions_collection.insert_one(new_def.model_dump(by_alias=True))
    return new_def

# ОБНОВЛЕНИЕ ПРАВИЛА
@app.put("/definitions/{def_id}", response_model=DefinitionModel)
async def update_definition(def_id: str, def_in: DefinitionCreate):
    # Находим по _id и обновляем
    updated_def = await definitions_collection.find_one_and_update(
        {"_id": def_id}, 
        {"$set": def_in.model_dump()},
        return_document=True
    )
    if not updated_def:
        raise HTTPException(status_code=404, detail="Rule not found")
    return updated_def

# УДАЛЕНИЕ ПРАВИЛА
@app.delete("/definitions/{def_id}")
async def delete_definition(def_id: str):
    result = await definitions_collection.delete_one({"_id": def_id})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Rule not found")
    return {"message": "Rule deleted successfully"}


async def log_request_to_db(session_id: str, method: str, path: str, rule_id: str, rule_name: str, status_code: int):
    """Фоновая задача для записи лога в MongoDB"""
    log_doc = {
        "session_id": session_id,
        "timestamp": datetime.now(timezone.utc),
        "request": {
            "method": method,
            "path": path
        },
        "engine_decision": {
            "matched_rule_id": rule_id,
            "matched_rule_name": rule_name
        },
        "response": {
            "status_code": status_code
        }
    }
    await request_logs_collection.insert_one(log_doc)


@app.get("/logs/{session_id}")
async def get_session_logs(session_id: str):
    """Получить логи конкретной сессии для Инспектора"""
    cursor = request_logs_collection.find({"session_id": session_id})
    cursor.sort("timestamp", -1)
    logs = await cursor.to_list(length=200)
    
    # MongoDB возвращает _id как ObjectId, нам нужно превратить его в строку для JSON
    for log in logs:
        log["_id"] = str(log["_id"])
    return logs


# Этот роут ловит любые пути и любые методы, которые не совпали с ручками выше (типа /sessions)
@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy_engine(request: Request, path: str, background_tasks: BackgroundTasks):
    
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
            rule_id_str = str(rule.get("_id"))
            rule_name = rule.get("name", "Unknown")
            background_tasks.add_task(log_request_to_db, session_id, request.method, path, rule_id_str, rule_name, matched_rule["status_code"])
            break # Нашли самое приоритетное совпадение — останавливаемся!

    # 3. Если правило не найдено (тот самый Fallback)
    if not matched_rule:
        # 1. Добавляем задачу в фон (используем строку "None" или пустоту для rule_id)
        background_tasks.add_task(log_request_to_db, session_id, request.method, path, "no_rule", 404)
          
        # Возвращаем JSONResponse вместо raise HTTPException!
        return JSONResponse(
            status_code=404, 
            content={"detail": f"No mock rule found for {request.method} /{path}"}
        )
    
    # STATEFUL ЛОГИКА (Работа с виртуальной БД)

    state_logic = matched_rule.get("state_logic")
    rule_id_str = str(matched_rule.get("_id", "")) # Получаем ID сработавшего правила
    
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
            
             # Кидаем лог в фон перед возвратом ответа!
            background_tasks.add_task(log_request_to_db, session_id, request.method, path, rule_id_str, matched_rule["status_code"])
            return JSONResponse(status_code=matched_rule["status_code"], content={"message": "Saved", "data": body})

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
            
             # Кидаем лог в фон!
            background_tasks.add_task(log_request_to_db, session_id, request.method, path, rule_id_str, matched_rule["status_code"])
            return JSONResponse(status_code=matched_rule["status_code"], content=result_data)

    # 4. Если state_logic нет, работаем по-старому (Stateless)
    # Кидаем лог в фон для Stateless правил!
    background_tasks.add_task(log_request_to_db, session_id, request.method, path, rule_id_str, matched_rule["status_code"])
    return JSONResponse(
        status_code=matched_rule["status_code"],
        content=matched_rule.get("response_payload", {})
    )