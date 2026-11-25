# Contributing / Как внести вклад

Спасибо за интерес к проекту! Ниже — краткая инструкция, как поднять
dev-окружение, проверить изменения и оформить Pull Request.

## Dev-окружение

Нужен **Windows 10/11** (полный сценарий с `winws.exe` и UAC) и **Python 3.10+**.
На Linux/macOS можно запускать офлайн-тесты и линтеры, но GUI/драйвер не соберётся.

```bat
git clone https://github.com/artur0vi4/desync-pilot.git
cd desync-pilot
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt -r requirements-dev.txt
```

Запуск из исходников (приложение само запросит повышение прав):

```bat
python -m app.main
python -m app.main --minimized   # сразу в трей
```

## Тесты и линтеры

Перед каждым PR:

```bat
pytest tests -q          :: офлайн-тесты (без сети и без Qt)
ruff check .             :: линтер (правила настроены в pyproject.toml)
mypy --platform win32 app\tester.py app\config.py app\main.py
```

Тесты не трогают сеть и реестр: сетевые пробы в `app/tester.py` подменяются
через `httpx.MockTransport` / `mock.patch`, конфиг пишется во временный каталог.

## Как устроен код (коротко)

- `app/main.py` — вход: single-instance mutex, повышение прав UAC.
- `app/controller.py` — контроллер: все долгие операции в фоновых потоках,
  отмена через `run_id` + `threading.Event`. **Не меняйте этот механизм** —
  он защищает от race conditions между UI и потоками.
- `app/tester.py` — прогон стратегий и HTTPS-пробы целей (общий `httpx.Client`,
  retry с экспоненциальной задержкой и джиттером).
- `app/config.py` — `config.ini` рядом с exe; новые числовые поля добавляйте
  и в `_INT_BOUNDS` (границы + дефолт для кривых значений).
- `app/paths.py` — пути в dev- и frozen-режиме (PyInstaller).
- `app/ui/` — PyQt6: окно, трей, настройки. Строки интерфейса — только через
  `i18n.t(...)`.

## Правила Pull Request

1. Одна тема — один PR. Сначала ответвление от `main`, не коммит напрямую.
2. К поведенческим изменениям — тест (`tests/`). Баг-фикс: тест, воспроизводящий
   баг до фикса.
3. Стиль, принятый в проекте: `from __future__ import annotations` в начале
   модуля, `dataclasses` для структур, `logging` через `app.logger.get()`,
   type hints на всех новых/изменённых функциях.
4. Не коммитьте: `config.ini`, `state.json`, логи, бинарники из `bin/`,
   временные артефакты сборки, секреты и токены.
5. Сообщение коммита — короткая императивная строка (как в истории:
   `Add a GitHub publish helper...`, `1.4.0: accept-or-continue dialog...`).
6. Обсуждения — по-русски или по-английски, на усмотрение автора.
7. Сборку exe в PR делать не нужно: релизный бандл собирает CI
   (`.github/workflows/build.yml`) по тегу `v*`.

Если изменение меняет формат `config.ini` или кэша `state.json` — сохраняйте
обратную совместимость: конфиги существующих пользователей не должны ломаться.
