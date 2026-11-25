# -*- coding: utf-8 -*-
"""P1-2 (аудит 4l5): бэкапы .env.bak-<ts> роута /api/settings/hubs и
локальные .env-оверлеи покрыты .gitignore — путь утечки секретов закрыт."""
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

_REPO = Path(__file__).parent.parent

# Проверка правила git — нужен сам git: docker-прогон из git archive
# (условия CI-артефакта) его не содержит; на CI и в dev-чекбоксе git есть.
pytestmark = pytest.mark.skipif(shutil.which("git") is None,
                                reason="git недоступен в этой среде")


def _ignored(rel: str) -> bool:
    """git check-ignore: True, если путь покрыт правилами игнора."""
    proc = subprocess.run(["git", "check-ignore", "--", rel],
                          cwd=_REPO, capture_output=True, text=True)
    return proc.returncode == 0


def test_env_backups_ignored():
    assert _ignored(".env.bak-1763000000")
    assert _ignored(".env.bak-20261123T120000")


def test_env_local_overlays_ignored():
    assert _ignored(".env.production.local")
    assert _ignored(".env")               # прежнее правило не сломано
