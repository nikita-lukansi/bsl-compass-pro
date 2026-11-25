# -*- coding: utf-8 -*-
"""Клиент эмбеддингов OpenAI-совместимого API (clean-room, без заимствований).

Назначение: векторизация текстов и запросов через любой OpenAI-совместимый
эндпоинт (/v1/embeddings): KodikRouter, OpenAI, vLLM, Ollama-совместимые шлюзы.

Особенности:
- только stdlib (urllib) — слой тестируется без сторонних зависимостей;
- батчинг с лимитом размера, ретраи с экспоненциальной паузой;
- честные исключения EmbeddingError с причиной (баланс/сеть/5xx).
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

RETRY_DELAYS = (1.0, 2.0, 4.0, 8.0)  # секунды между попытками


class EmbeddingError(RuntimeError):
    """Провайдер эмбеддингов недоступен или отклонил запрос."""


class EmbeddingsClient:
    """Тонкий клиент /embeddings OpenAI-совместимого API.

    Параметры конфигурации берутся из аргументов конструктора; env-значения
    ниже — точка интеграции с настройками ядра:
        EMBEDDING_BASE_URL, EMBEDDING_API_KEY, EMBEDDING_MODEL,
        EMBEDDING_TIMEOUT (сек, дефолт 60), EMBEDDING_BATCH (дефолт 32).
    """

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        timeout: float | None = None,
        batch_size: int | None = None,
    ):
        self.base_url = (base_url or os.getenv("EMBEDDING_BASE_URL", "")).rstrip("/")
        self.api_key = api_key or os.getenv("EMBEDDING_API_KEY", "")
        self.model = model or os.getenv("EMBEDDING_MODEL", "")
        self.timeout = float(timeout or os.getenv("EMBEDDING_TIMEOUT", "60"))
        self.batch_size = int(batch_size or os.getenv("EMBEDDING_BATCH", "32"))
        if not self.base_url or not self.model:
            raise EmbeddingError(
                "EMBEDDING_BASE_URL и EMBEDDING_MODEL обязательны для семантического слоя"
            )

    def _post(self, texts: list[str], timeout: float | None = None,
              retry_delays: tuple | None = None) -> list[list[float]]:
        """Один HTTP-запрос /embeddings с ретраями. Возвращает векторы по порядку.

        timeout — переопределение self.timeout (F8/xsv: запросный путь
        codesearch короче индексационного); retry_delays — свои задержки
        (пустой кортеж = один запрос без ретраев).
        """
        timeout = timeout or self.timeout
        if retry_delays is None:
            retry_delays = RETRY_DELAYS
        payload = json.dumps({"model": self.model, "input": texts}).encode("utf-8")
        req = urllib.request.Request(
            self.base_url + "/embeddings",
            data=payload,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
        )
        last_exc: Exception | None = None
        for attempt, delay in enumerate((0.0,) + tuple(retry_delays), start=1):
            if delay:
                time.sleep(delay)
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                vectors = [item["embedding"] for item in data["data"]]
                if len(vectors) != len(texts):
                    raise EmbeddingError(
                        f"провайдер вернул {len(vectors)} векторов на {len(texts)} текстов"
                    )
                return vectors
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace")[:200]
                last_exc = EmbeddingError(f"HTTP {exc.code}: {body}")
                # 4xx (кроме 429) не ретраятся — запрос не станет валидным
                if exc.code not in (429, 500, 502, 503, 504):
                    raise last_exc
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                last_exc = EmbeddingError(f"{type(exc).__name__}: {exc}")
        raise EmbeddingError(f"эмбеддинги не получены после {attempt} попыток: {last_exc}")

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Векторизация корпуса батчами (для индексации)."""
        out: list[list[float]] = []
        for i in range(0, len(texts), self.batch_size):
            out.extend(self._post(texts[i : i + self.batch_size]))
        return out

    def embed_query(self, text: str, timeout: float | None = None) -> list[float]:
        """Векторизация одиночного запроса.

        timeout — для ЗАПРОСНОГО пути (codesearch): короче индексационного
        и без длинных ретраев (один запрос) — клиент MCP не должен висеть
        на недоступном провайдере (F8/compass-xsv).
        """
        return self._post([text], timeout=timeout, retry_delays=())[0]


def split_camel_case(text: str) -> str:
    """Разбиение слитного CamelCase для эмбеддинга: 'ОбработкаПроведения'
    -> 'Обработка Проведения', 'APIКлиент' -> 'API Клиент' (границы:
    нижний→верхний и латиница↔кириллица). Применяется ТОЛЬКО к тексту
    эмбеддинга, буквальный поиск — по исходной строке."""
    import re

    text = re.sub(r"(?<=[а-яёa-z])(?=[А-ЯЁA-Z])", " ", text)
    text = re.sub(r"(?<=[a-zA-Z])(?=[А-ЯЁ])", " ", text)
    text = re.sub(r"(?<=[а-яёА-ЯЁ])(?=[A-Za-z])", " ", text)
    return text
