# -*- coding: utf-8 -*-
"""Клиент ИТС (its.1c.ru): CAS-логин через login.1c.ru, throttle, ре-логин.

Схема входа (CAS-портал 1С):
GET /user/auth → GET login.1c.ru/login?service=… (куки потока) → POST
username/password/execution/_eventId → ticket → ИТС-сессия. Все страницы
Windows-1251. Cookie-сессия живёт только в ОЗУ процесса и на диск не
пишется.
"""
from __future__ import annotations

import http.cookiejar
import random
import re
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://its.1c.ru"
PORTAL_LOGIN = "https://login.1c.ru/login"
UA = "bsl-compass-pro/0.2 personal docs sync"

# Признак «страница отдалась гостю»: форма входа через портал (страница
# /user/auth) или ссылка «Войти» в шапке (страницы книг/документов)
_GUEST_MARKER = re.compile(
    r'<form method="GET" action="https://login\.1c\.ru/login"|href="/user/auth')


class ItsError(Exception):
    """Ошибка обмена с ИТС (сеть, HTTP, неожиданная разметка)."""


class ItsAuthError(ItsError):
    """Вход не удался или сессия протухла без возможности ре-логина."""


class ItsClient:
    """HTTP-клиент ИТС с сессией в ОЗУ.

    Режимы: «login» (логин+пароль — CAS-вход и авто-ре-логин), «cookie»
    (готовая строка Cookie из браузера, ре-логина нет), «none» (только
    публичные страницы). Между запросами пауза delay + случайный джиттер;
    заданный pause_event замораживает клиента до снятия (пауза оператора).
    """

    def __init__(self, login: str = "", password: str = "", cookie: str = "",
                 delay: float = 2.5, pause_event=None, timeout: int = 45):
        self.login_name = str(login or "").strip()
        self.password = str(password or "")
        self.cookie_header = str(cookie or "").strip()
        self.delay = max(0.0, float(delay))
        self.pause = pause_event
        self.timeout = timeout
        self._jar = http.cookiejar.CookieJar()
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self._jar))
        self._logged_in = False

    # ------------------------------------------------------------ служебное

    def _throttle(self) -> None:
        if self.pause is not None:
            self.pause.wait()
        if self.delay:
            time.sleep(self.delay + random.uniform(0.0, 0.5))

    def _request(self, url: str, data: bytes | None):
        headers = {"User-Agent": UA}
        if self.cookie_header and not self.login_name:
            headers["Cookie"] = self.cookie_header
        req = urllib.request.Request(url, data=data, headers=headers)
        try:
            resp = self._opener.open(req, timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            raise ItsError(f"HTTP {exc.code}: {url[:120]}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ItsError(f"сеть недоступна: {url[:120]}: {exc}") from exc
        with resp:
            raw = resp.read()
            ctype = resp.headers.get("Content-Type", "")
            return resp.status, resp.geturl(), self._decode(raw, ctype)

    @staticmethod
    def _decode(raw: bytes, ctype: str) -> str:
        match = re.search(r"charset=([\w-]+)", ctype or "", re.I)
        encodings = [match.group(1).lower()] if match else []
        encodings += ["cp1251", "utf-8"]
        for enc in encodings:
            try:
                return raw.decode(enc)
            except (UnicodeDecodeError, LookupError):
                continue
        return raw.decode("cp1251", errors="replace")

    # ------------------------------------------------------------ публичное

    def login(self) -> None:
        """CAS-вход через портал 1С. При неудаче — ItsAuthError с причиной."""
        if not self.login_name or not self.password:
            raise ItsAuthError("не заданы ITS_LOGIN/ITS_PASSWORD")
        _, _, page = self._request("https://its.1c.ru/user/auth", None)
        match = re.search(
            r'<form method="GET" action="(https://login\.1c\.ru/login)"'
            r'[^>]*>\s*<input[^>]*name="service"\s+value="([^"]+)"', page)
        if not match:
            raise ItsAuthError("на /user/auth нет формы входа через портал")
        service = match.group(2).replace("&amp;", "&")
        portal_url = PORTAL_LOGIN + "?" + urllib.parse.urlencode(
            {"service": service})
        _, _, form = self._request(portal_url, None)
        exec_match = re.search(r'name="execution"\s+value="([^"]+)"', form)
        if not exec_match:
            raise ItsAuthError(
                "на форме портала нет execution — разметка изменилась?")
        fields = urllib.parse.urlencode({
            "inviteCode": "", "inviteType": "",
            "username": self.login_name, "password": self.password,
            "rememberMe": "on", "execution": exec_match.group(1),
            "_eventId": "submit", "geolocation": "", "submit": "Войти",
        }).encode()
        _, final_url, result = self._request(PORTAL_LOGIN, fields)
        if "login.1c.ru" in final_url and 'name="execution"' in result:
            marker = re.search(r'class="error[^"]*"[^>]*>([^<]{5,160})', result)
            reason = marker.group(1).strip() if marker else \
                "портал вернул форму входа (неверные креды или блокировка)"
            raise ItsAuthError(f"логин не принят: {reason}")
        if "its.1c.ru" not in final_url:
            raise ItsAuthError(f"после POST не вернулись на ИТС: {final_url[:90]}")
        # финальная проверка: страница книги должна прийти без гостевой формы
        _, _, check = self._request(f"{BASE}/db/ka25doc", None)
        if _GUEST_MARKER.search(check):
            raise ItsAuthError("вход не принят: ИТС отдаёт страницы гостя")
        self._logged_in = True

    def ensure_session(self) -> None:
        """Гарантировать авторизованную сессию (login-режим)."""
        if self._logged_in:
            return
        self.login()

    def get(self, url: str) -> str:
        """GET страницы с throttle; при потере сессии — ре-логин и повтор.

        В cookie-режиме потеря сессии — ItsAuthError (ре-логина нет).
        """
        self._throttle()
        _, _, text = self._request(url, None)
        if not _GUEST_MARKER.search(text):
            return text
        if self.login_name and self.password:
            if self._logged_in:
                self._logged_in = False
                self.login()  # сессия протухла в середине обхода
            else:
                self.login()
            _, _, text = self._request(url, None)
            if _GUEST_MARKER.search(text):
                raise ItsAuthError(f"сессия не восстановилась: {url[:120]}")
            return text
        if self.cookie_header:
            raise ItsAuthError("кука протухла — обновите ITS_COOKIE")
        return text  # режим «none»: гость — штатное состояние
