"""Гейт-хелперы HTTP-слоя: статика, имена каталогов, Host/Origin
(аудит SAFE-1/SAFE-4; публикация F — compass-rjq).

Лёгкий модуль без сторонних зависимостей: импортируется юнит-тестами.
is_local_host_name — общий для панельного POST-гейта (panel_api) и
глобального Host-гейта приложения (main.HostGateMiddleware).
"""
import ipaddress
import re
from pathlib import Path

_DIR_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")


def safe_static_path(base: Path, relative: str) -> Path | None:
    """Файл внутри base без выхода наружу (аудит SAFE-1): resolve +
    relative_to; None — traversal или не файл. Для FileResponse-роутов."""
    try:
        resolved = (base / relative).resolve()
        resolved.relative_to(base.resolve())
    except (ValueError, OSError):
        return None
    return resolved if resolved.is_file() else None


def validated_name(value: str) -> str | None:
    """alias/dir из query без traversal (аудит SAFE-4): без разделителей
    путей, «..», ведущей точки и пустоты. None — отказ."""
    v = (value or "").strip()
    if (not v or v.startswith(".") or ".." in v
            or "/" in v or "\\" in v
            or not _DIR_NAME_RE.fullmatch(v)):
        return None
    return v


def is_local_host_name(value: str) -> bool:
    """Имя хоста локальное: localhost, 127.0.0.0/8, ::1 (аудит 4l5 P1-1).

    Параметры:
        value — Origin или Host: возможно со схемой («http://localhost:8123»),
                портом («127.0.0.1:8123») или IPv6-скобками («[::1]:8123»).

    Возвращаемое значение:
        True — имя localhost-семейства; False — чужое имя/IP, пустая строка.
    """
    name = (value or "").strip()
    if "://" in name:  # Origin: срезать схему и путь
        name = name.split("://", 1)[1].split("/", 1)[0]
    if name.startswith("["):  # IPv6: [::1] или [::1]:8123
        name = name[1:].split("]", 1)[0]
    elif name.count(":") == 1:  # host:port (голый IPv6 всегда в скобках)
        name = name.rsplit(":", 1)[0]
    name = name.lower()
    if name == "localhost":
        return True
    try:
        return ipaddress.ip_address(name).is_loopback
    except ValueError:
        return False
