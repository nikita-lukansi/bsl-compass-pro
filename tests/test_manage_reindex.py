# -*- coding: utf-8 -*-
"""compass-756: manage reindex <CID> — позиционный cid и путь через
build_index_now (тот же протокол метрик, что панельный /reindex:
set_indexed + кэш счётчиков — см. исходник build_index_now)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src import manage  # noqa: E402


def test_reindex_parses_positional_cid():
    args = manage.build_parser().parse_args(["reindex", "Compass_x_0123abcd"])
    assert args.cid == "Compass_x_0123abcd"
    assert args.incremental is False


def test_reindex_config_flag_still_works():
    args = manage.build_parser().parse_args(
        ["reindex", "--config", "Compass_x_0123abcd"])
    assert args.cid is None
    assert args.config == "Compass_x_0123abcd"


def test_reindex_full_uses_build_index_now(tmp_path, monkeypatch):
    """Полный reindex идёт через build_index_now (метрики реестра), не
    напрямую в gyrfalcon; cid/paths передаются из build_paths (пути —
    песочница tmp_path, Б1: без абсолютных путей хоста)."""
    calls = {}

    def fake_build(cid, paths):
        calls["cid"] = cid
        calls["paths"] = paths
        return {"built": str(paths["index_db"])}

    monkeypatch.setattr(manage, "build_index_now", fake_build)
    monkeypatch.setattr(
        "src.registry.build_paths",
        lambda cid: {"cid": cid, "source_root": str(tmp_path / "src"),
                     "index_db": tmp_path / "index.db",
                     "display_name": "X"})
    args = manage.build_parser().parse_args(["reindex", "Compass_x_0123abcd"])
    result = manage.cmd_reindex(args)
    assert result["cid"] == "Compass_x_0123abcd"
    assert calls["cid"] == "Compass_x_0123abcd"
    assert "built" in result
