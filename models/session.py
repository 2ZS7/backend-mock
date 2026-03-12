from pydantic import BaseModel, Field
from typing import Optional
from datetime import datetime, timezone
from enum import Enum
import uuid

# 1. Перечисление для статусов (помогает избежать опечаток)
class SessionStatus(str, Enum):
    ACTIVE = "active"
    FINISHED = "finished"
    ABANDONED = "abandoned"
    ERROR = "error"

# 2. Вложенная модель для настроек сессии
class SessionConfig(BaseModel):
    rate_limit_rps: int = Field(default=100, description="Максимальное количество запросов в секунду")
    fallback_mode: str = Field(default="404", description="Режим при отсутствии правила: '404' или 'proxy'")

# 3. Вложенная модель для метрик
class SessionMetrics(BaseModel):
    total_requests: int = Field(default=0)
    failed_requests: int = Field(default=0)

# 4. Модель ЗАПРОСА (то, что тестировщик присылает в POST /sessions)
# Здесь нет ID, статуса или дат — их генерирует сервер!
class SessionCreate(BaseModel):
    name: str = Field(..., description="Понятное название тестовой сессии, например: 'UI-тесты корзины'")
    config: Optional[SessionConfig] = Field(default_factory=SessionConfig)

# 5. Главная модель (то, что мы храним в MongoDB и отдаем клиенту)
class SessionModel(BaseModel):
    # MongoDB использует поле _id, поэтому мы делаем alias
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), alias="_id")
    name: str
    status: SessionStatus = Field(default=SessionStatus.ACTIVE)
    
    # Автоматически ставим текущее время в формате UTC
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: Optional[datetime] = None
    
    config: SessionConfig
    metrics: SessionMetrics = Field(default_factory=SessionMetrics)

    class Config:
        # Эта настройка позволяет обращаться к полю как к 'id' в Python, 
        # но при сохранении в БД оно превратится в '_id'
        populate_by_name = True