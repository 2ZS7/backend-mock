# Stateful-VirtualEngine Backend

Серверная часть системы для имитации API с поддержкой состояний (Stateful) и контрактной валидации.

## Технологический стек
* **Python 3.12+**
* **FastAPI** — асинхронный фреймворк
* **MongoDB** — хранилище состояний и логов
* **Redis** — кэширование сессий и управление квотами (Rate Limiting)

## Начало работы
1. Убедитесь, что установлены `docker` и `docker-compose`.
2. Создайте виртуальное окружение: `python -m venv .venv`
3. Установите зависимости: `pip install -r requirements.txt`
4. Запустите базы данных: `docker-compose up -d mongodb redis`
5. Запустите сервер: `uvicorn main:app --reload`

## Документация API
После запуска сервера интерактивная документация доступна по адресу: `http://localhost:8000/docs`
