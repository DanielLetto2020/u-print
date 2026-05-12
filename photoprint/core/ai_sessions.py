"""Локальная история AI-сессий.

Сессия — это цепочка пар «запрос → одна или несколько картинок», которые
пользователь продолжает уточнять. Храним всё в одном JSON-файле
``ai_sessions.json`` рядом с прочими настройками: батч-формат, без БД,
потому что данных немного, а в одном файле проще делать atomic-replace.

Каждое сообщение хранит только пути к сохранённым на диск картинкам — сами
байты лежат в `ai_output_dir`, мы их в JSON не дублируем.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from photoprint.core.settings import config_dir

logger = logging.getLogger(__name__)

SESSIONS_FILE = "ai_sessions.json"


@dataclass
class AIMessage:
    """Одно «прохождение цикла»: prompt + сгенерированные пути."""

    prompt: str
    image_paths: list[str] = field(default_factory=list)
    # ISO-timestamp, чтобы UI мог показать «когда сгенерили».
    created_at: str = ""
    # Текстовый ответ модели (необязательный, но иногда модель комментирует).
    text_reply: str = ""
    # Картинки-референсы, которые юзер подмешал к этому prompt-у
    # (например, чтобы попросить «измени вот это»). Не путать с image_paths
    # — те это РЕЗУЛЬТАТ, эти — ВХОД.
    reference_paths: list[str] = field(default_factory=list)


@dataclass
class AISession:
    """Целая беседа: id, имя, выбранная модель, последовательность сообщений."""

    id: str
    name: str
    model: str
    messages: list[AIMessage] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""
    # Имя задано автоматически («Session 1», «Сессия 1») — UI имеет право
    # заменить его на короткий первый prompt после первой генерации.
    # Если юзер переименовал руками — флаг снимается и UI больше не трогает.
    auto_named: bool = True

    @classmethod
    def new(cls, name: str, model: str) -> AISession:
        now = datetime.now().isoformat(timespec="seconds")
        return cls(
            id=uuid.uuid4().hex,
            name=name,
            model=model,
            messages=[],
            created_at=now,
            updated_at=now,
            auto_named=True,
        )


def sessions_path() -> Path:
    return config_dir() / SESSIONS_FILE


def load_all() -> list[AISession]:
    """Прочитать все сессии. Пустой/битый файл → пустой список (не ошибка)."""
    path = sessions_path()
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Failed to read AI sessions: %s", exc)
        return []
    out: list[AISession] = []
    for item in raw:
        try:
            msgs = [
                AIMessage(
                    prompt=m.get("prompt", ""),
                    image_paths=list(m.get("image_paths", [])),
                    created_at=m.get("created_at", ""),
                    text_reply=m.get("text_reply", ""),
                    reference_paths=list(m.get("reference_paths", [])),
                )
                for m in item.get("messages", [])
            ]
            out.append(
                AISession(
                    id=item["id"],
                    name=item.get("name", "Session"),
                    model=item.get("model", ""),
                    messages=msgs,
                    created_at=item.get("created_at", ""),
                    updated_at=item.get("updated_at", ""),
                    auto_named=bool(item.get("auto_named", False)),
                )
            )
        except KeyError as exc:
            logger.warning("Skip malformed session: missing %s", exc)
    # Свежие — сверху.
    out.sort(key=lambda s: s.updated_at or s.created_at, reverse=True)
    return out


def save_all(sessions: list[AISession]) -> None:
    """Полная перезапись всех сессий через atomic-replace."""
    data = [asdict(s) for s in sessions]
    path = sessions_path()
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    tmp.replace(path)


def touch(session: AISession) -> None:
    """Сдвинуть ``updated_at`` сессии на текущее время."""
    session.updated_at = datetime.now().isoformat(timespec="seconds")
