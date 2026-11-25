# -*- coding: utf-8 -*-
"""Реестр офлайн-задач: статусы, watcher-тик вручную, восстановление (arch §9)."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.runtime import offline, tasks as tt  # noqa: E402


CALLS = [{"method": "data_query",
          "params": {"query": "ВЫБРАТЬ 1", "limit": 10}}]


def resp_file(root: Path, task_id: str, results=None, fatal=None) -> None:
    data = {"version": 1, "task_id": task_id, "created_at": "2026-11-11T00:00:00+00:00",
            "base": {"name": "КА", "version": "0.2.0"},
            "results": results if results is not None else
                       [{"method": "data_query", "ok": True, "answer": {}}]}
    if fatal:
        data["Фатально"] = fatal
    offline.write_atomic(root / "results" / f"resp_{task_id}.json",
                         json.dumps(data, ensure_ascii=False))


@pytest.fixture
def reg(tmp_path, monkeypatch):
    monkeypatch.setattr(offline, "OFFLINE_ROOT", tmp_path)
    r = tt.TaskRegistry(tmp_path)          # watcher НЕ запущен — тики руками
    yield r
    r._stop.set()


def test_submit_writes_atomic_req(reg, tmp_path):
    t = reg.submit("chat-1", CALLS, "тест")
    assert t.status == "queued"
    raw = json.loads((tmp_path / "queue" / f"req_{t.id}.json")
                     .read_text(encoding="utf-8"))
    assert raw["version"] == 1 and raw["chat"] == "chat-1"
    assert raw["calls"] == CALLS and len(t.id) == 9 and t.id.startswith("t")


def test_submit_validates_calls(reg):
    with pytest.raises(ValueError):
        reg.submit("chat-1", [])
    with pytest.raises(ValueError):
        reg.submit("chat-1", [{"method": ""}])
    with pytest.raises(ValueError):
        reg.submit("chat-1", [{"method": "ping", "params": "не dict"}])
    with pytest.raises(ValueError):
        reg.submit("chat-1", [{"method": "ping", "params": {}}] * 101)


def test_taken_marks_in_1c_and_resp_makes_ready(reg, tmp_path):
    t = reg.submit("chat-1", CALLS)
    (tmp_path / "queue" / f"req_{t.id}.json").rename(
        tmp_path / "queue" / f"req_{t.id}.taken")
    reg._tick()
    assert reg._tasks[t.id].status == "in_1c"
    resp_file(tmp_path, t.id)
    reg._tick()
    task = reg._tasks[t.id]
    assert task.status == "ready" and task.results is not None
    # resp перенесён в архив
    assert not (tmp_path / "results" / f"resp_{t.id}.json").exists()
    assert (tmp_path / "archive" / f"resp_{t.id}.loaded").exists()


def test_resp_with_fatally_is_error(reg, tmp_path):
    t = reg.submit("chat-1", CALLS)
    resp_file(tmp_path, t.id, results=[], fatal="упало")
    reg._tick()
    assert reg._tasks[t.id].status == "error"


def test_expired_then_late_resp_resurrects(reg, tmp_path):
    t = reg.submit("chat-1", CALLS)
    task = reg._tasks[t.id]
    task.created_at = "2000-01-01T00:00:00+00:00"   # состарить
    reg._tick()
    assert task.status == "expired"
    resp_file(tmp_path, t.id)
    reg._tick()
    assert task.status == "ready"                    # воскрешение


def test_wait_isolation_and_idempotency(reg, tmp_path):
    t = reg.submit("chat-1", CALLS)
    resp_file(tmp_path, t.id)
    reg._tick()
    out1 = reg.wait("chat-2", t.id, timeout=0.1)
    assert "error" in out1 and "my_tasks" in out1     # чужой чат
    out2 = reg.wait("chat-1", t.id, timeout=1)
    assert out2["status"] in ("ready", "delivered") and out2["results"]
    out3 = reg.wait("chat-1", t.id, timeout=1)        # идемпотентно
    assert out3["status"] in ("ready", "delivered") and out3["results"]


def test_wait_timeout(reg):
    t = reg.submit("chat-1", CALLS)
    out = reg.wait("chat-1", t.id, timeout=0.3)
    assert out["status"] == "timeout"


def test_restore_from_files(tmp_path, monkeypatch):
    monkeypatch.setattr(offline, "OFFLINE_ROOT", tmp_path)
    t1 = tt.TaskRegistry(tmp_path).submit("chat-1", CALLS, "ждёт")
    t2 = tt.TaskRegistry(tmp_path).submit("chat-2", CALLS, "взят")
    (tmp_path / "queue" / f"req_{t2.id}.json").rename(
        tmp_path / "queue" / f"req_{t2.id}.taken")
    fresh = tt.TaskRegistry(tmp_path)                 # «рестарт»
    assert fresh._tasks[t1.id].status == "queued"
    assert fresh._tasks[t2.id].status == "in_1c"


def test_orphan_resp_is_recognized(reg, tmp_path):
    resp_file(tmp_path, "tunknown01")
    reg._tick()
    orphans = [x for x in reg._tasks.values() if x.unrecognized]
    assert len(orphans) == 1
    assert (tmp_path / "archive" / "resp_tunknown01.loaded").exists()


def test_cancel_only_queued(reg, tmp_path):
    t = reg.submit("chat-1", CALLS)
    assert reg.cancel("chat-1", t.id)["status"] == "cancelled"
    assert not (tmp_path / "queue" / f"req_{t.id}.json").exists()
    t2 = reg.submit("chat-1", CALLS)
    resp_file(tmp_path, t2.id)
    reg._tick()
    assert "error" in reg.cancel("chat-1", t2.id)     # готовую нельзя


def test_cancel_owner_bypasses_chat(reg):
    """Панель — владелец: owner=True отменяет задачу чужого чата."""
    t = reg.submit("chat-1", CALLS)
    assert reg.cancel("panel", t.id, owner=True)["status"] == "cancelled"


def test_resubmit_creates_new_task_and_cancels_old(reg, tmp_path):
    t = reg.submit("chat-1", CALLS, "повтор")
    out = reg.resubmit("panel", t.id, owner=True)
    assert out["resubmitted"] != t.id
    assert reg._tasks[t.id].status == "cancelled"
    new_task = reg._tasks[out["resubmitted"]]
    assert new_task.status == "queued"
    assert new_task.calls == CALLS and new_task.description == "повтор"
    assert new_task.chat == "chat-1"                  # чат задачи сохранён
    # в очереди — файл нового task_id, старого нет
    assert (tmp_path / "queue" / f"req_{out['resubmitted']}.json").exists()
    assert not (tmp_path / "queue" / f"req_{t.id}.json").exists()


def test_safe_queue_name():
    assert tt.safe_queue_name("req_t12345678.json")
    assert tt.safe_queue_name("req_t12345678.taken")
    assert not tt.safe_queue_name("../req_x.json")
    assert not tt.safe_queue_name("req_/etc/passwd")
    assert not tt.safe_queue_name("evil.sh")


def test_offline_summary_counts_and_last(tmp_path, monkeypatch):
    monkeypatch.setattr(offline, "OFFLINE_ROOT", tmp_path)
    reg = tt.TaskRegistry(tmp_path)
    monkeypatch.setattr(tt, "_registry", reg)
    t1 = reg.submit("chat-1", CALLS, "первая")
    t2 = reg.submit("chat-1", CALLS, "вторая")
    reg.cancel("chat-1", t2.id)
    # «последняя» — максимум created_at среди не-unrecognized: фиксируем
    # времена явно, чтобы тест не зависел от границы секунды
    reg._tasks[t1.id].created_at = "2026-11-11T00:00:02+00:00"
    reg._tasks[t2.id].created_at = "2026-11-11T00:00:01+00:00"
    s = tt.offline_summary()
    assert s["mode"] == "offline"
    assert s["counts"] == {"queued": 1, "cancelled": 1}
    assert s["last"]["id"] == t1.id


def test_offline_summary_no_registry(monkeypatch):
    monkeypatch.setattr(tt, "_registry", None)
    s = tt.offline_summary()
    assert s == {"mode": "offline", "counts": {}, "last": None}


# ------------------------------------------------- Т2-I: owner-деанон, ARCHIVE_RAW

def _resp_with_secret(root: Path, task_id: str) -> None:
    data = {"version": 1, "task_id": task_id, "created_at": "x", "base": {},
            "results": [{"method": "get_object", "ok": True,
                         "answer": {"Наименование": "ООО Секрет"}}]}
    offline.write_atomic(root / "results" / f"resp_{task_id}.json",
                         json.dumps(data, ensure_ascii=False))


def test_owner_sees_deanonymized_agent_sees_tokens(tmp_path, monkeypatch):
    monkeypatch.setattr(offline, "OFFLINE_ROOT", tmp_path)
    reg = tt.TaskRegistry(tmp_path)
    t = reg.submit("chat-1", CALLS)
    _resp_with_secret(tmp_path, t.id)
    reg._tick()
    agent_view = reg.task_view("chat-1", t.id, owner=False)
    owner_view = reg.task_view("chat-1", t.id, owner=True)
    # ensure_ascii=False: json.dumps по умолчанию экранирует кириллицу
    assert "ООО Секрет" not in json.dumps(agent_view, ensure_ascii=False)   # агент — токены
    assert "ООО Секрет" in json.dumps(owner_view, ensure_ascii=False)       # владелец — значения


def test_owner_view_reads_archive_raw(tmp_path, monkeypatch):
    """Память пуста (TTL), но архив жив: владелец получает сырьё как есть
    (results_source: archive_raw), агент — ничего."""
    monkeypatch.setattr(offline, "OFFLINE_ROOT", tmp_path)
    reg = tt.TaskRegistry(tmp_path)
    t = reg.submit("chat-1", CALLS)
    _resp_with_secret(tmp_path, t.id)
    reg._tick()
    reg._tasks[t.id].results = None               # имитируем истечение TTL памяти
    owner_view = reg.task_view("chat-1", t.id, owner=True)
    assert owner_view.get("results_source") == "archive_raw"
    assert "ООО Секрет" in json.dumps(owner_view, ensure_ascii=False)
    agent_view = reg.task_view("chat-1", t.id, owner=False)
    assert "ООО Секрет" not in json.dumps(agent_view, ensure_ascii=False)


def test_archive_raw_off_drops_file_and_wait_from_archive(tmp_path, monkeypatch):
    """ARCHIVE_RAW=off: сырьё в archive/ не хранится; wait без памяти —
    честный «результат утерян», а не переанонимизация."""
    monkeypatch.setenv("ARCHIVE_RAW", "off")
    monkeypatch.setattr(offline, "OFFLINE_ROOT", tmp_path)
    reg = tt.TaskRegistry(tmp_path)
    t = reg.submit("chat-1", CALLS)
    _resp_with_secret(tmp_path, t.id)
    reg._tick()
    assert reg._tasks[t.id].status == "ready"
    assert not (tmp_path / "archive" / f"resp_{t.id}.loaded").exists()
    reg._tasks[t.id].results = None               # имитируем истечение TTL памяти
    out = reg.wait("chat-1", t.id, timeout=1)
    assert "утерян" in json.dumps(out, ensure_ascii=False)


# ------------------------------------------------- compass-5pw: результаты переживают TTL

def _expire_task(reg, task) -> None:
    """Состарить задачу и прогнать тик — штатный путь в expired."""
    task.created_at = "2000-01-01T00:00:00+00:00"
    reg._tick()
    assert task.status == "expired"


def test_wait_expired_without_results_stays_expired(reg):
    """Без ответа — прежнее поведение: честный expired, без late."""
    t = reg.submit("chat-1", CALLS)
    _expire_task(reg, reg._tasks[t.id])
    out = reg.wait("chat-1", t.id, timeout=1)
    assert out["status"] == "expired"
    assert out["results"] is None
    assert "late" not in out
    assert "_note" in out            # подсказка: поздний ответ доставит повторный wait


def test_late_resp_delivers_with_late_flag(reg, tmp_path):
    """Транзитный expired (владелец медлил), потом ответ в results/:
    воскрешение → wait выдаёт результаты с пометкой late: true."""
    t = reg.submit("chat-1", CALLS)
    _expire_task(reg, reg._tasks[t.id])
    resp_file(tmp_path, t.id)
    reg._tick()
    out = reg.wait("chat-1", t.id, timeout=1)
    assert out["status"] == "delivered" and out["results"]
    assert out["late"] is True


def _archive_resp_after_live_exchange(reg, tmp_path, task_id) -> None:
    """Архив как после живого обмена: resp подхвачен и уехал в archive/."""
    resp_file(tmp_path, task_id)
    reg._tick()
    assert (tmp_path / "archive" / f"resp_{task_id}.loaded").exists()


def test_expired_revives_from_archive_on_tick(reg, tmp_path):
    """Рестарт-сценарий (compass-5pw): ответ уже в archive/, задача
    ушла в expired при пустом results/ — тик воскрешает из архива."""
    t = reg.submit("chat-1", CALLS)
    _archive_resp_after_live_exchange(reg, tmp_path, t.id)
    task = reg._tasks[t.id]
    task.status = "expired"          # имитация: просрочилась вне тиков
    task.transitions["expired"] = "2026-11-11T00:00:00+00:00"
    task.results = None
    reg._tick()
    assert task.status == "ready"
    assert task.results is not None
    out = reg.wait("chat-1", t.id, timeout=1)
    assert out["status"] == "delivered" and out["results"]
    assert out["late"] is True


def test_wait_revives_expired_from_archive_inline(reg, tmp_path):
    """offline_wait не отвечает expired, когда ответ уже в archive/:
    воскрешение на месте, без ожидания тика watcher'а."""
    t = reg.submit("chat-1", CALLS)
    _archive_resp_after_live_exchange(reg, tmp_path, t.id)
    task = reg._tasks[t.id]
    task.status = "expired"
    task.transitions["expired"] = "2026-11-11T00:00:00+00:00"
    task.results = None
    out = reg.wait("chat-1", t.id, timeout=1)
    assert out["status"] == "delivered" and out["results"]
    assert out["late"] is True


def test_terminal_req_files_move_to_archive(reg, tmp_path):
    """Форма финализирует заявку на месте (.json.done после записи resp,
    .json.broken — битый пакет, arch §11): watcher убирает терминальные
    файлы из queue/ в archive/."""
    t = reg.submit("chat-1", CALLS)
    (tmp_path / "queue" / f"req_{t.id}.json").rename(
        tmp_path / "queue" / f"req_{t.id}.json.done")
    (tmp_path / "queue" / "req_tbroken01.json.broken").touch()
    reg._tick()
    assert not (tmp_path / "queue" / f"req_{t.id}.json.done").exists()
    assert (tmp_path / "archive" / f"req_{t.id}.json.done").exists()
    assert (tmp_path / "archive" / "req_tbroken01.json.broken").exists()
    # живой req не тронут
    t2 = reg.submit("chat-1", CALLS)
    reg._tick()
    assert (tmp_path / "queue" / f"req_{t2.id}.json").exists()


# ------------------------------------------ compass-lz7: сериализация ответа (P0/P1)

def _resp_leak_scenario(root: Path, task_id: str) -> None:
    """Сценарий приёмки (feedback-2026-11-17, 6 каналов утечки): прямая
    выборка Наименование + разыменование Ном.Наименование + колонка Ссылка
    (представление с номером и датой) + Код справочника; рядом — ISO-дата,
    GUID и число (whitelist)."""
    results = [{
        "method": "data_query", "ok": True,
        "answer": {
            "ЕщеЕсть": False,
            "Строки": [{
                "Наименование":
                    "Приобретение товаров и услуг ЛД00-000998 от 16.09.2026",
                "Номенклатура.Наименование":
                    "Chapman Рэд КССЛ, МРЦ 250,00 T&T",
                "Ссылка":
                    "Приобретение товаров и услуг ЛД00-000998 "
                    "от 16.09.2026 12:34:00",
                "Код": "00-00035542",
                "Период": "2026-09-16T00:00:00",
                "СсылкаГуид": "e1c4a2b3-0000-1111-2222-333344445555",
                "Количество": 12,
            }],
        },
    }]
    offline.write_atomic(
        root / "results" / f"resp_{task_id}.json",
        json.dumps({"version": 1, "task_id": task_id, "results": results},
                   ensure_ascii=False))


def test_acceptance_all_string_cells_masked(reg, tmp_path):
    """Приёмка P0 (compass-lz7): ВСЕ строковые значения замаскированы
    (представления, разыменования, Код); ISO-дата/GUID/число прошли;
    deanonymize обратим для всех новых токенов."""
    t = reg.submit("chat-1", CALLS)
    _resp_leak_scenario(tmp_path, t.id)
    reg._tick()
    out = reg.wait("chat-1", t.id, timeout=1)
    row = out["results"][0]["answer"]["Строки"][0]
    assert row["Наименование"].startswith("Значение_Т")
    assert row["Номенклатура.Наименование"].startswith("Номенклатура_Т")
    assert row["Ссылка"].startswith("Значение_Т")
    assert row["Код"].startswith("Значение_Т")
    assert row["Период"] == "2026-09-16T00:00:00"               # whitelist
    assert row["СсылкаГуид"] == "e1c4a2b3-0000-1111-2222-333344445555"
    assert row["Количество"] == 12
    assert out["results"][0]["answer"]["ЕщеЕсть"] is False
    # ни одной утечки в агентском виде (6 каналов)
    dumped = json.dumps(out, ensure_ascii=False)
    for leaked in ("ЛД00-000998", "Chapman", "00-00035542", "Приобретение"):
        assert leaked not in dumped
    # deanonymize обратим
    a = tt.get_anonymizer("chat-1")
    row0 = a.deanonymize_data(out["results"])[0]["answer"]["Строки"][0]
    assert row0["Наименование"] == \
           "Приобретение товаров и услуг ЛД00-000998 от 16.09.2026"
    assert row0["Номенклатура.Наименование"] == \
           "Chapman Рэд КССЛ, МРЦ 250,00 T&T"
    assert row0["Ссылка"] == ("Приобретение товаров и услуг ЛД00-000998 "
                              "от 16.09.2026 12:34:00")
    assert row0["Код"] == "00-00035542"
    assert row0["Период"] == "2026-09-16T00:00:00"              # не токен
    assert row0["Количество"] == 12


def test_classify_error_patterns():
    """Классификация по ядровым текстам (P1, compass-lz7): RLS/права,
    поле не обнаружено, синтаксис; прочее — OTHER."""
    assert tt._classify_error("У пользователя недостаточно прав на "
                              "исполнение операции над данными") == \
        "ACCESS_DENIED"
    assert tt._classify_error("нарушение прав доступа") == "ACCESS_DENIED"
    assert tt._classify_error("объект не найден (удалён или нет прав)") == \
        "ACCESS_DENIED"
    assert tt._classify_error("Поле не обнаружено: 'Наименование'") == \
        "FIELD_NOT_FOUND"
    assert tt._classify_error("Поле объекта не обнаружено (Пинг)") == \
        "FIELD_NOT_FOUND"
    assert tt._classify_error("Синтаксическая ошибка \"ВЫБРАТЬ ...\"") == \
        "SYNTAX_ERROR"
    assert tt._classify_error("объект Справочник.X по коду '1' не найден") \
        == "OTHER"
    assert tt._classify_error("") == "OTHER"


def test_error_results_classified_and_masked(reg, tmp_path):
    """P1 (compass-lz7): ok:false → {method, ok, code, masked_message};
    класс НЕ маскируется, текст — токеном как сейчас; deanonymize
    возвращает исходные тексты (owner-вид)."""
    cases = [
        ("У пользователя недостаточно прав на исполнение операции "
         "над данными", "ACCESS_DENIED"),
        ("Поле не обнаружено: 'Наименование'", "FIELD_NOT_FOUND"),
        ("Синтаксическая ошибка", "SYNTAX_ERROR"),
        ("неизвестный метод: ping2, известны: ping", "OTHER"),
    ]
    results = [{"method": "data_query", "ok": False,
                "answer": {"Ошибка": text}} for text, _ in cases]
    t = reg.submit("chat-1", CALLS)
    offline.write_atomic(
        tmp_path / "results" / f"resp_{t.id}.json",
        json.dumps({"version": 1, "task_id": t.id, "results": results},
                   ensure_ascii=False))
    reg._tick()
    out = reg.wait("chat-1", t.id, timeout=1)
    for item, (text, code) in zip(out["results"], cases):
        assert item["method"] == "data_query" and item["ok"] is False
        assert item["code"] == code
        assert item["masked_message"].startswith("Значение_Т")
        assert text not in json.dumps(item, ensure_ascii=False)
    # классы не маскируются
    assert "ACCESS_DENIED" in json.dumps(out, ensure_ascii=False)
    assert "FIELD_NOT_FOUND" in json.dumps(out, ensure_ascii=False)
    # deanonymize обратим для masked_message
    restored = tt.get_anonymizer("chat-1").deanonymize_data(out["results"])
    for item, (text, _) in zip(restored, cases):
        assert item["masked_message"] == text


def test_fatal_masked_and_classified(reg, tmp_path):
    """«Фатально» — тоже строковое значение ответа (P0): текст-токен +
    класс (P1) в виде задачи; сырого текста нет нигде в агентском виде."""
    t = reg.submit("chat-1", CALLS)
    resp_file(tmp_path, t.id, results=[],
              fatal="серверная обработка пакета: нет прав")
    reg._tick()
    task = reg._tasks[t.id]
    assert task.status == "error"
    assert task.fatal.startswith("Значение_Т")
    assert task.fatal_code == "ACCESS_DENIED"
    view = json.dumps(reg.task_view("chat-1", t.id), ensure_ascii=False)
    assert "нет прав" not in view
    assert view.count("ACCESS_DENIED") == 1


def test_owner_view_restores_masked_results(reg, tmp_path):
    """owner-вид после новой сериализации: владелец видит исходные значения
    (деанон словарём чата), агент — токены (Т2-I, теперь и для masked_message)."""
    t = reg.submit("chat-1", CALLS)
    _resp_leak_scenario(tmp_path, t.id)
    reg._tick()
    owner_view = json.dumps(reg.task_view("chat-1", t.id, owner=True),
                            ensure_ascii=False)
    agent_view = json.dumps(reg.task_view("chat-1", t.id), ensure_ascii=False)
    assert "ЛД00-000998" in owner_view and "Chapman" in owner_view
    assert "ЛД00-000998" not in agent_view and "Chapman" not in agent_view
