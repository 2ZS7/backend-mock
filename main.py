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

async def log_request_to_db(
    session_id: str, 
    method: str, 
    path: str, 
    rule_id: str | None, 
    rule_name: str | None, 
    status_code: int,
    request_body: any = None,
    response_body: any = None
):
    """Фоновая задача для записи лога и обновления метрик сессии в MongoDB"""
    
    # 1. Записываем сам лог транзакции в request_logs (как было)
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

    # ==========================================================
    # ОБНОВЛЕНИЕ МЕТРИК СЕССИИ (Новый блок!)
    # ==========================================================
    # При каждом запросе увеличиваем total_requests на 1
    update_query = {"$inc": {"metrics.total_requests": 1}}
    
    # Если статус ответа >= 400 (ошибка), то увеличиваем и failed_requests на 1
    if status_code >= 400:
        update_query["$inc"]["metrics.failed_requests"] = 1
        
    # Асинхронно обновляем документ сессии в MongoDB
    await sessions_collection.update_one({"_id": session_id}, update_query)

    

# Этот роут ловит любые пути и любые методы, которые не совпали с ручками выше (типа /sessions)
@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy_engine(request: Request, path: str, background_tasks: BackgroundTasks):
    session_id = request.headers.get("x-session-id")
    if not session_id:
        raise HTTPException(status_code=401, detail="X-Session-ID header is missing")

    redis_key = f"session:{session_id}:active"
    is_active = await redis_client.get(redis_key)
    if is_active is None:
        raise HTTPException(status_code=401, detail="Session is invalid or expired")

    # ЧИТАЕМ ТЕЛО ЗАПРОСА СТРОГО ОДИН РАЗ (для методов, которые могут его иметь)
    body_json = None
    if request.method in ("POST", "PUT", "PATCH"):
        try:
            body_json = await request.json()
        except Exception:
            pass # Если тело пустое или это не JSON, оставляем None

    # 1. Поиск правил в MongoDB
    cursor = definitions_collection.find({"method": {"$in": [request.method, "ANY"]}})
    cursor.sort("priority", -1)
    rules = await cursor.to_list(length=100)

    matched_rule = None
    for rule in rules:
        if re.match(rule["path_pattern"], path):
            matched_rule = rule
            break

    # 2. Обработка случая, когда правило не найдено (404)
    if not matched_rule:
        err_content = {"detail": f"No mock rule found for {request.method} /{path}"}
        # Передаем тело запроса (body_json) и тело ответа (err_content) в лог
        background_tasks.add_task(log_request_to_db, session_id, request.method, path, None, "no_rule", 404, body_json, err_content)
        return JSONResponse(status_code=404, content=err_content)
    
    rule_id_str = str(matched_rule.get("_id", ""))
    rule_name = matched_rule.get("name", "Unknown")
    state_logic = matched_rule.get("state_logic")
    
    response_status = matched_rule["status_code"]
    response_content = matched_rule.get("response_payload", {})

    # 3. Выполнение логики сохранения состояния (Stateful)
    if state_logic:
        action = state_logic.get("action")
        collection_name = state_logic.get("collection_name")
        
        if action == "insert":
            # Используем уже прочитанный body_json!
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

    # 4. ЗАПИСЬ ЛОГА (Передаем body_json и response_content)
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