from pydantic import BaseModel, Field
from typing import Any, Dict, Optional
import uuid

# НОВАЯ МОДЕЛЬ: Инструкция для работы с состоянием
class StateLogic(BaseModel):
    action: str = Field(..., description="insert, find, none")
    collection_name: str = Field(..., description="Имя виртуальной коллекции, например 'users'")

class DefinitionCreate(BaseModel):
    name: str = Field(..., description="Понятное имя правила, например 'Мок для юзеров'")
    method: str = Field(default="GET", description="GET, POST, PUT, DELETE или ANY")
    path_pattern: str = Field(..., description="Регулярное выражение, например: ^api/v1/users.*$")
    priority: int = Field(default=0, description="Чем больше число, тем выше приоритет")
        
    # Что будет отдавать наш мок
    status_code: int = 200
    response_payload: Optional[Dict[str, Any]] = None # Теперь это опционально!
    state_logic: Optional[StateLogic] = None


class DefinitionModel(DefinitionCreate):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), alias="_id")

    class Config:
        populate_by_name = True