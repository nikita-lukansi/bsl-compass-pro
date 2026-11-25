"""Аудит SAFE-1/SAFE-4: гейт-хелперы HTTP-слоя (юнит, без src.main)."""
from pathlib import Path

import pytest

from src.http_guard import safe_static_path, validated_name


class TestSafeStaticPath:
    def _base(self, tmp_path: Path) -> Path:
        (tmp_path / "ok.js").write_text("x", encoding="utf-8")
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "ok.css").write_text("y", encoding="utf-8")
        return tmp_path

    def test_inside_ok(self, tmp_path):
        base = self._base(tmp_path)
        assert safe_static_path(base, "ok.js") == (base / "ok.js").resolve()
        assert safe_static_path(base, "sub/ok.css") is not None

    def test_traversal_blocked(self, tmp_path):
        base = self._base(tmp_path)
        secret = tmp_path.parent / "compass_secret.txt"
        secret.write_text("s", encoding="utf-8")
        assert safe_static_path(base, "../compass_secret.txt") is None
        assert safe_static_path(base, "sub/../../compass_secret.txt") is None

    def test_absolute_and_empty_rejected(self, tmp_path):
        # абсолютный операнд подменяет базу (семантика Path / — аудит SAFE-4)
        base = self._base(tmp_path)
        assert safe_static_path(base, "/etc/passwd") is None
        assert safe_static_path(base, "") is None          # каталог, не файл
        assert safe_static_path(base, "nope.js") is None   # не существует


@pytest.mark.parametrize("ok", ["UT11_5", "ka-2.5.174", "Compass-2026", "a"])
def test_names_ok(ok):
    assert validated_name(ok) == ok


@pytest.mark.parametrize("bad", ["", ".", "..", "a/../b", "/etc", "a\\b",
                                 ".hidden", "a b", "a;b"])
def test_names_rejected(bad):
    assert validated_name(bad) is None
