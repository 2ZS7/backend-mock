import re
import traceback 
from fastapi import FastAPI, Request, HTTPException, BackgroundTasks, Path
from models.session import SessionCreate, SessionModel
from datetime import datetime, timezone
from database import sessions_collection, redis_client, virtual_state_collection, definitions_collection, request_logs_collection
from models.definition import DefinitionCreate, DefinitionModel
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="Stateful Mock Engine")

async def create_indexes():
    # Мы ищем логи по session_id, поэтому индекс критически важен!
    await request_logs_collection.create_index("session_id")
    # Ищем правила по методу и приоритету
    await definitions_collection.create_index([("method", 1), ("priority", -1)])
    await virtual_state_collection.create_index("session_id")
    
@app.on_event("startup")
async def startup_event():
    await create_indexes()

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
    """Получить список всех сессий для Dashboard с ленивой синхронизацией статусов"""
    cursor = sessions_collection.find()
    cursor.sort("created_at", -1)
    sessions = await cursor.to_list(length=100)
    
    # ==========================================================
    # ЛЕНИВАЯ СИНХРОНИЗАЦИЯ СТАТУСОВ (Redis -> MongoDB)
    # ==========================================================
    for session in sessions:
        if session["status"] == "active":
            # Проверяем, существует ли еще ключ активности в Redis
            redis_key = f"session:{session['_id']}:active"
            is_active = await redis_client.get(redis_key)
            
            if is_active is None:
                # Если в Redis ключа нет — значит, время жизни (TTL) сессии истекло!
                # Переводим статус сессии в MongoDB в положение "finished"
                await sessions_collection.update_one(
                    {"_id": session["_id"]}, 
                    {"$set": {"status": "finished"}}
                )
                # Обновляем статус в объекте ответа, который улетит на фронтенд
                session["status"] = "finished"
                
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

@app.get("/logs/{session_id}")
async def get_session_logs(session_id: str):
    """Получить логи конкретной сессии для Инспектора"""
    cursor = request_logs_collection.find({"session_id": session_id})
    cursor.sort("timestamp", -1)
    
    # УВЕЛИЧИЛИ ЛИМИТ ДО 1000, чтобы вмещать результаты нагрузочных тестов
    logs = await cursor.to_list(length=1000) 
    
    for log in logs:
        log["_id"] = str(log["_id"])
    return logs

async def log_request_to_db(
    session_id: str, 
    method: str, 
    path: str, 
    rule_id: str | None, 
    rule_name: str | None, 
    status_code: int,
    request_body = None,   # Убрали спорные тайп-хинты, чтобы избежать ошибок импорта
    response_body = None   # Убрали спорные тайп-хинты
):
    """Фоновая задача с защитой от скрытых ошибок"""
    try:
        # 1. Формируем и записываем лог
        log_doc = {
            "session_id": session_id,
            "timestamp": datetime.now(timezone.utc),
            "request": {
                "method": method,
                "path": path,
                "body": request_body
            },
            "engine_decision": {
                "matched_rule_id": None if rule_id in ("no_rule", None) else rule_id,
                "matched_rule_name": rule_name if rule_name else "no_rule"
            },
            "response": {
                "status_code": status_code,
                "body": response_body
            }
        }
        await request_logs_collection.insert_one(log_doc)

        # 2. Обновляем метрики сессии в MongoDB
        update_query = {"$inc": {"metrics.total_requests": 1}}
        if status_code >= 400:
            update_query["$inc"]["metrics.failed_requests"] = 1
            
        await sessions_collection.update_one({"_id": session_id}, update_query)
        print(f"DEBUG: Лог успешно записан, метрики обновлены для сессии {session_id}")

    except Exception as e:
        # Если внутри фонового потока произойдет ЛЮБАЯ ошибка — мы увидим её в терминале!
        print(f"!!! ОШИБКА В ФОНОВОЙ ЗАДАЧЕ ЛОГИРОВАНИЯ: {e}")
        traceback.print_exc() # Печатаем полный стек ошибки в консоль


# Этот роут ловит любые пути и любые методы, которые не совпали с ручками выше
@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy_engine(request: Request, path: str, background_tasks: BackgroundTasks):
    # 1. Извлечение и проверка сессии
    session_id = request.headers.get("x-session-id")
    if not session_id:
        raise HTTPException(status_code=401, detail="X-Session-ID header is missing")

    redis_key = f"session:{session_id}:active"
    is_active = await redis_client.get(redis_key)
    if is_active is None:
        raise HTTPException(status_code=401, detail="Session is invalid or expired")

    # ==========================================================
    # ВСТАВИЛИ: СЛОЙ НАДЕЖНОСТИ - RATE LIMITING (Redis)
    # ==========================================================
    current_second = int(datetime.now(timezone.utc).timestamp())
    rate_limit_key = f"rate_limit:{session_id}:{current_second}"
    
    # Атомарно увеличиваем счетчик запросов для текущей секунды
    request_count = await redis_client.incr(rate_limit_key)
    if request_count == 1:
        await redis_client.expire(rate_limit_key, 2) # TTL 2 секунды для очистки
        
    max_limit = 5 # Устанавливаем жесткий лимит в 5 запросов в секунду для теста
    
    if request_count > max_limit:
        # Асинхронно логируем превышение лимита в request_logs со статусом 429
        background_tasks.add_task(log_request_to_db, session_id, request.method, path, None, "rate_limit_exceeded", 429, None, {"detail": "Too Many Requests"})
        return JSONResponse(
            status_code=429,
            content={"detail": "Too Many Requests. Rate limit exceeded (Max 5 requests per second)."}
        )

    # 2. Читаем тело запроса строго один раз
    body_json = None
    if request.method in ("POST", "PUT", "PATCH"):
        try:
            body_json = await request.json()
        except Exception:
            pass

    # 3. Поиск правил в MongoDB (Priority Matching)
    cursor = definitions_collection.find({"method": {"$in": [request.method, "ANY"]}})
    cursor.sort("priority", -1)
    rules = await cursor.to_list(length=100)

    matched_rule = None
    for rule in rules:
        if re.match(rule["path_pattern"], path):
            matched_rule = rule
            break

    # 4. Обработка случая, когда правило не найдено (404)
    if not matched_rule:
        err_content = {"detail": f"No mock rule found for {request.method} /{path}"}
        background_tasks.add_task(log_request_to_db, session_id, request.method, path, None, "no_rule", 404, body_json, err_content)
        return JSONResponse(status_code=404, content=err_content)
    
    rule_id_str = str(matched_rule.get("_id", ""))
    rule_name = matched_rule.get("name", "Unknown")
    state_logic = matched_rule.get("state_logic")
    
    response_status = matched_rule["status_code"]
    response_content = matched_rule.get("response_payload", {})

    # 5. Выполнение логики сохранения состояния (Stateful)
    if state_logic:
        action = state_logic.get("action")
        collection_name = state_logic.get("collection_name")
        
        if action == "insert":
            virtual_doc = {
                "session_id": session_id,
                "entity_type": collection_name,
                "payload": body_json
            }
            await virtual_state_collection.insert_one(virtual_doc)
            response_content = {"message": "Saved successfully", "data": body_json}
            response_status = 201

        elif action == "find":
            cursor_state = virtual_state_collection.find({
                "session_id": session_id,
                "entity_type": collection_name
            })
            saved_docs = await cursor_state.to_list(length=100)
            response_content = [doc["payload"] for doc in saved_docs]
            response_status = 200

    # 6. ЕДИНАЯ ЗАПИСЬ ЛОГА В ФОНЕ (Гарантирует 1 лог на транзакцию)
    background_tasks.add_task(
        log_request_to_db, 
        session_id, 
        request.method, 
        path, 
        rule_id_str, 
        rule_name, 
        response_status,
        body_json,
        response_content
    )

    return JSONResponse(status_code=response_status, content=response_content)