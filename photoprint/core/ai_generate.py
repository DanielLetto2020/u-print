"""Клиент OpenRouter для генерации картинок.

Модуль ядра — без GTK. Делает три вещи:

1. :func:`list_image_models` — забирает каталог моделей и оставляет только
   те, что умеют возвращать ``image`` в выходных модальностях.
2. :func:`generate_image` — отправляет prompt (опционально вместе с одной или
   несколькими картинками-референсами) и достаёт PNG-байты из ответа.
3. :func:`save_image_bytes` — сохраняет PNG/JPEG в заданную папку, имя из
   timestamp + короткого суффикса prompt-а.

Сетевые вызовы — синхронный :mod:`urllib`, чтобы не тянуть лишний пакет.
UI должен звать всё это в worker-треде и доставать результат через
``GLib.idle_add``.
"""

from __future__ import annotations

import base64
import json
import logging
import re
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

OPENROUTER_BASE = "https://openrouter.ai/api/v1"
HTTP_REFERER = "https://github.com/DanielLetto2020/u-print"
APP_TITLE = "PhotoPrint"
DEFAULT_TIMEOUT = 120.0  # секунд — генерация может идти долго


class OpenRouterError(RuntimeError):
    """Любая ошибка от OpenRouter (HTTP, парсинг, отсутствие image-полей)."""


@dataclass(frozen=True)
class AIModel:
    """Описание одной image-capable модели из каталога OpenRouter."""

    id: str
    name: str
    description: str = ""
    # «pricing.prompt» и «pricing.image» в долларах за токен / картинку.
    # Не парсим в число — храним как есть, UI просто покажет.
    pricing_prompt: str = ""
    pricing_image: str = ""


@dataclass(frozen=True)
class GeneratedImage:
    """Одна сгенерированная картинка плюс сопутствующий текстовый ответ модели."""

    image_bytes: bytes
    mime_type: str
    text_reply: str = ""


def _request(
    url: str,
    *,
    api_key: str,
    method: str = "GET",
    body: dict | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> dict:
    """Минималистичный HTTP-вызов к OpenRouter с разбором JSON.

    Поднимает :class:`OpenRouterError` на любой неудаче — HTTP-код, не-JSON,
    структурно битый ответ. Текст ошибки максимально информативный, чтобы
    в UI было что показать пользователю.
    """
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
        "HTTP-Referer": HTTP_REFERER,
        "X-Title": APP_TITLE,
    }
    if data is not None:
        headers["Content-Type"] = "application/json"
    # Принудительный HTTPS — даже если кто-то подсунет http://, не уходим в открытый канал.
    if not url.lower().startswith("https://"):
        raise OpenRouterError("Refusing non-HTTPS URL")
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        # Тело даёт человеко-понятное сообщение OpenRouter — вытаскиваем.
        msg = _extract_error_message(exc) or exc.reason or str(exc)
        raise OpenRouterError(f"HTTP {exc.code}: {msg}") from exc
    except urllib.error.URLError as exc:
        raise OpenRouterError(f"Network error: {exc.reason}") from exc
    except (TimeoutError, ssl.SSLError, OSError) as exc:
        # urlopen может бросить чистый socket.timeout / SSL-ошибку мимо URLError.
        raise OpenRouterError(f"Network error: {exc}") from exc
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OpenRouterError(f"Invalid JSON response: {exc}") from exc


def _extract_error_message(exc: urllib.error.HTTPError) -> str:
    """Достать читаемый ``error.message`` из тела ответа, толерантно к форме.

    OpenRouter обычно отвечает ``{"error": {"message": "..."}}``, но может
    прислать и строку, и массив, и даже не-JSON. Любая такая форма не должна
    ронять выходящее исключение — лучше показать сырое тело.
    """
    try:
        payload = exc.read().decode("utf-8", errors="replace")
    except OSError:
        return ""
    if not payload:
        return ""
    try:
        parsed = json.loads(payload)
    except (ValueError, TypeError):
        return payload[:500]
    if isinstance(parsed, dict):
        err = parsed.get("error")
        if isinstance(err, dict):
            return str(err.get("message") or "") or payload[:500]
        if isinstance(err, str):
            return err
    return payload[:500]


def list_image_models(api_key: str) -> list[AIModel]:
    """Вернуть только модели, у которых в ``output_modalities`` есть ``image``.

    На странице ``/v1/models`` каждая модель содержит ``architecture`` с
    полями ``input_modalities``/``output_modalities``. Текстовые модели
    отфильтрованы, картинки нас интересуют только на выходе.

    На пустой ключ кидаем :class:`OpenRouterError` — без ключа полный
    каталог открывается, но дальше всё равно нечем будет генерить.
    """
    if not api_key.strip():
        raise OpenRouterError("API key is empty")
    data = _request(f"{OPENROUTER_BASE}/models", api_key=api_key)
    models: list[AIModel] = []
    for item in data.get("data", []):
        arch = item.get("architecture") or {}
        outputs = arch.get("output_modalities") or []
        if "image" not in outputs:
            continue
        pricing = item.get("pricing") or {}
        models.append(
            AIModel(
                id=str(item.get("id", "")),
                name=str(item.get("name", item.get("id", ""))),
                description=str(item.get("description", ""))[:240],
                pricing_prompt=str(pricing.get("prompt", "")),
                pricing_image=str(pricing.get("image", "")),
            )
        )
    models.sort(key=lambda m: m.name.lower())
    return models


def _data_url(image_bytes: bytes, mime_type: str = "image/png") -> str:
    """Закодировать байты картинки в ``data:`` URL для multimodal-инпута."""
    return f"data:{mime_type};base64,{base64.b64encode(image_bytes).decode('ascii')}"


def generate_image(
    *,
    api_key: str,
    model: str,
    prompt: str,
    reference_images: list[Path] | None = None,
    history: list[dict] | None = None,
) -> GeneratedImage:
    """Сгенерировать (или отредактировать) картинку.

    Args:
        api_key: ключ OpenRouter.
        model: id image-capable модели (например ``google/gemini-2.5-flash-image``).
        prompt: текст пользователя.
        reference_images: пути к локальным картинкам, которые надо подмешать
            в сообщение в роли референса. OpenRouter ожидает их в
            ``content`` как ``image_url`` с data: URL.
        history: предыдущие сообщения сессии в формате OpenAI-chat. Если
            заданы — добавляются ПЕРЕД новым user-сообщением, чтобы модель
            видела всю цепочку правок.

    Returns:
        :class:`GeneratedImage` с PNG-байтами. Текстовый ответ модели — если
        был — лежит в ``text_reply``.

    Raises:
        OpenRouterError: при сетевой ошибке или если в ответе нет картинки.
    """
    if not api_key.strip():
        raise OpenRouterError("API key is empty")
    if not model.strip():
        raise OpenRouterError("Model is empty")
    if not prompt.strip() and not reference_images:
        raise OpenRouterError("Prompt is empty")

    content: list[dict] = []
    if prompt.strip():
        content.append({"type": "text", "text": prompt.strip()})
    for ref in reference_images or []:
        try:
            raw = ref.read_bytes()
        except OSError as exc:
            raise OpenRouterError(f"Cannot read {ref}: {exc}") from exc
        mime = _guess_mime(ref)
        content.append(
            {"type": "image_url", "image_url": {"url": _data_url(raw, mime)}}
        )

    messages: list[dict] = []
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": content})

    body = {
        "model": model,
        "messages": messages,
        # Ключевой момент: без этого модель уйдёт в текстовый режим даже
        # если умеет рисовать. Spec OpenRouter: image+text вместе.
        "modalities": ["image", "text"],
    }
    data = _request(
        f"{OPENROUTER_BASE}/chat/completions",
        api_key=api_key,
        method="POST",
        body=body,
    )

    choices = data.get("choices") or []
    if not choices:
        raise OpenRouterError("Empty response (no choices)")
    msg = choices[0].get("message") or {}
    images = msg.get("images") or []
    if not images:
        # Иногда модель отказывается рисовать и шлёт только текст —
        # выдаём это пользователю, чтобы он понял, в чём дело.
        text = (msg.get("content") or "").strip()
        raise OpenRouterError(text or "Model returned no image")

    img0 = images[0]
    url = (img0.get("image_url") or {}).get("url") or ""
    if not url.startswith("data:"):
        raise OpenRouterError("Image is not inline data URL")
    try:
        header, payload = url.split(",", 1)
        mime = header.removeprefix("data:").split(";")[0] or "image/png"
        image_bytes = base64.b64decode(payload)
    except (ValueError, base64.binascii.Error) as exc:
        raise OpenRouterError(f"Bad data URL: {exc}") from exc

    text_reply = ""
    raw_content = msg.get("content")
    if isinstance(raw_content, str):
        text_reply = raw_content.strip()
    elif isinstance(raw_content, list):
        text_reply = " ".join(
            part.get("text", "") for part in raw_content if part.get("type") == "text"
        ).strip()

    return GeneratedImage(image_bytes=image_bytes, mime_type=mime, text_reply=text_reply)


def _guess_mime(path: Path) -> str:
    """Грубый mime по расширению. OpenRouter принимает png/jpeg/webp."""
    ext = path.suffix.lower()
    return {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
        ".gif": "image/gif",
    }.get(ext, "image/png")


def _safe_filename_part(prompt: str, limit: int = 40) -> str:
    """Превратить prompt в кусок имени файла: латиница/цифры/дефис, ≤40 симв."""
    cleaned = re.sub(r"[^\w\-]+", "-", prompt.strip(), flags=re.UNICODE)
    cleaned = cleaned.strip("-")
    return cleaned[:limit].rstrip("-")


def save_image_bytes(
    image: GeneratedImage, directory: Path, prompt_hint: str = ""
) -> Path:
    """Сохранить картинку в ``directory``, вернуть путь к новому файлу.

    Имя файла: ``YYYYMMDD-HHMMSS_<slug>.png``. Расширение выбирается по
    ``image.mime_type``. Если файл с таким именем уже есть, добавляем -1, -2…
    """
    directory.mkdir(parents=True, exist_ok=True)
    ext = {
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/webp": ".webp",
    }.get(image.mime_type, ".png")

    stem = datetime.now().strftime("%Y%m%d-%H%M%S")
    slug = _safe_filename_part(prompt_hint)
    base = f"{stem}_{slug}" if slug else stem
    candidate = directory / f"{base}{ext}"
    n = 1
    while candidate.exists():
        candidate = directory / f"{base}-{n}{ext}"
        n += 1
    candidate.write_bytes(image.image_bytes)
    logger.info("Saved AI image: %s (%d bytes)", candidate, len(image.image_bytes))
    return candidate
