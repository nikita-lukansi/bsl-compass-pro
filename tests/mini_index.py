# -*- coding: utf-8 -*-
"""Фабрика мини-индекса gyrfalcon для герметичных тестов тулов фасады."""
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE modules (id INTEGER PRIMARY KEY, rel_path TEXT UNIQUE NOT NULL,
    category TEXT, object_name TEXT, module_type TEXT, form_name TEXT,
    is_form INTEGER DEFAULT 0, mtime REAL, size INTEGER);
CREATE TABLE methods (id INTEGER PRIMARY KEY,
    module_id INTEGER NOT NULL REFERENCES modules(id), name TEXT NOT NULL,
    type TEXT NOT NULL, is_export INTEGER DEFAULT 0, params TEXT,
    line INTEGER, end_line INTEGER, loc INTEGER);
CREATE TABLE calls (id INTEGER PRIMARY KEY,
    caller_id INTEGER NOT NULL REFERENCES methods(id),
    callee_name TEXT NOT NULL, line INTEGER, callee_key TEXT,
    resolution TEXT NOT NULL, confidence REAL NOT NULL);
-- Зеркало ddl.rs (compass-44g): суффиксный индекс по ПОСЛЕДНЕЙ точке —
-- callers/repomap ищут «Модуль.Имя» и «Объект.Модуль.Имя». Зеркало индексов
-- мини держится вручную; сверку ТАБЛИЦ мини ↔ ddl.rs делает метатест
-- test_ddl_meta (compass-kep), индексы метатестом не сверяются
CREATE INDEX IF NOT EXISTS idx_calls_callee_last ON calls(
    (substr(callee_name, length(rtrim(callee_name, replace(callee_name, '.', ''))) + 1)) COLLATE NOCASE);
-- A12/P2 (полная DDL-поверхность GyrReader): FTS-поиск методов, подписки
-- событий, права ролей, регламентные задания, web/http-сервисы, code-usages
CREATE VIRTUAL TABLE methods_fts USING fts5(
    name, params, tokenize='unicode61 remove_diacritics 2');
CREATE TABLE event_subscriptions (id INTEGER PRIMARY KEY,
    name TEXT NOT NULL, synonym TEXT, event TEXT, handler_module TEXT,
    handler_procedure TEXT, source_types TEXT, source_count INTEGER,
    file TEXT);
CREATE TABLE scheduled_jobs (id INTEGER PRIMARY KEY,
    name TEXT, synonym TEXT, method_name TEXT, handler_module TEXT,
    handler_procedure TEXT, use TEXT, predefined INTEGER DEFAULT 0,
    restart_count INTEGER DEFAULT 0, restart_interval INTEGER DEFAULT 0,
    file TEXT);
CREATE TABLE web_services (id INTEGER PRIMARY KEY, name TEXT, namespace TEXT,
    operations_json TEXT, file TEXT);
CREATE TABLE http_services (id INTEGER PRIMARY KEY, name TEXT, root_url TEXT,
    templates_json TEXT, file TEXT);
CREATE TABLE role_rights (id INTEGER PRIMARY KEY,
    role_name TEXT NOT NULL, object_name TEXT NOT NULL,
    right_name TEXT NOT NULL, file TEXT);
CREATE TABLE metadata_code_usages (id INTEGER PRIMARY KEY,
    module_id INTEGER NOT NULL REFERENCES modules(id),
    object_ref TEXT NOT NULL, object_ref_key TEXT, member_path TEXT,
    usage_kind TEXT, line INTEGER);
CREATE TABLE object_attributes (id INTEGER PRIMARY KEY,
    object_name TEXT NOT NULL, category TEXT NOT NULL, attr_name TEXT NOT NULL,
    attr_synonym TEXT, attr_type TEXT, attr_kind TEXT NOT NULL, ts_name TEXT,
    source_file TEXT NOT NULL, length INTEGER, precision INTEGER,
    scale INTEGER, date_fractions TEXT);
CREATE TABLE predefined_items (id INTEGER PRIMARY KEY,
    object_name TEXT NOT NULL, category TEXT NOT NULL, item_name TEXT NOT NULL,
    item_synonym TEXT, item_code TEXT, types_json TEXT,
    is_folder INTEGER DEFAULT 0, source_file TEXT NOT NULL);
CREATE TABLE subsystem_content (id INTEGER PRIMARY KEY,
    subsystem_name TEXT NOT NULL, subsystem_synonym TEXT,
    object_ref TEXT NOT NULL, file TEXT NOT NULL);
CREATE TABLE module_headers (module_id INTEGER PRIMARY KEY REFERENCES modules(id),
    header_comment TEXT NOT NULL);
CREATE TABLE regions (id INTEGER PRIMARY KEY,
    module_id INTEGER REFERENCES modules(id), name TEXT NOT NULL,
    line INTEGER NOT NULL, end_line INTEGER);
CREATE TABLE enum_values (id INTEGER PRIMARY KEY,
    name TEXT NOT NULL, synonym TEXT, values_json TEXT NOT NULL,
    source_file TEXT NOT NULL);
CREATE TABLE object_synonyms (id INTEGER PRIMARY KEY,
    object_name TEXT NOT NULL, category TEXT NOT NULL, synonym TEXT NOT NULL,
    file TEXT NOT NULL);
CREATE VIRTUAL TABLE objects_fts USING fts5(name, tokenize = 'trigram');
CREATE TABLE form_elements (id INTEGER PRIMARY KEY, object_name TEXT,
    category TEXT, form_name TEXT, kind TEXT, scope TEXT, element_name TEXT,
    element_type TEXT, event TEXT, handler TEXT, data_path TEXT,
    main_table TEXT, attribute_is_main INTEGER, extra_json TEXT, file TEXT);
CREATE TABLE register_movements (id INTEGER PRIMARY KEY,
    document_name TEXT, register_name TEXT, source TEXT, file TEXT);
CREATE TABLE metadata_references (id INTEGER PRIMARY KEY,
    source_object TEXT NOT NULL, source_category TEXT NOT NULL,
    ref_object TEXT NOT NULL, ref_kind TEXT NOT NULL, used_in TEXT NOT NULL,
    path TEXT NOT NULL, line INTEGER);
-- help-слой: DDL сверен с vendored gyrfalcon help.rs (source_id, path,
-- category, object_name, form_name, title, text, chars, sha256)
CREATE TABLE extensions (name TEXT NOT NULL, purpose TEXT, name_prefix TEXT,
    rel_root TEXT NOT NULL);
CREATE TABLE extension_objects (extension_name TEXT NOT NULL,
    category TEXT NOT NULL, object_name TEXT NOT NULL, belonging TEXT NOT NULL,
    source_file TEXT NOT NULL);
CREATE TABLE extension_overrides (id INTEGER PRIMARY KEY,
    object_name TEXT NOT NULL, source_path TEXT NOT NULL DEFAULT '',
    source_module_id INTEGER, target_method TEXT NOT NULL,
    target_method_line INTEGER, annotation TEXT NOT NULL,
    extension_name TEXT NOT NULL, extension_purpose TEXT, extension_method TEXT,
    extension_root TEXT NOT NULL, ext_module_path TEXT NOT NULL,
    ext_line INTEGER);
CREATE TABLE help_sources (source_id TEXT PRIMARY KEY, root TEXT NOT NULL,
    config_version TEXT NOT NULL);
CREATE TABLE index_meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE help_pages (id INTEGER PRIMARY KEY,
    source_id TEXT NOT NULL, path TEXT NOT NULL, category TEXT NOT NULL,
    object_name TEXT NOT NULL, form_name TEXT NOT NULL, title TEXT NOT NULL,
    text TEXT NOT NULL, chars INTEGER NOT NULL, sha256 TEXT NOT NULL,
    UNIQUE(source_id, path));
CREATE VIRTUAL TABLE help_fts USING fts5(
    title, object_name, text, content='help_pages', content_rowid='id',
    tokenize='unicode61 remove_diacritics 2');
"""

SEED = """
INSERT INTO modules VALUES
 (1, 'CommonModules/ТестМодуль/Ext/Module.bsl', 'CommonModules',
  'ОбъектТест', 'Module', NULL, 0, 0, 0),
 (2, 'Documents/ДокЗаказ/Ext/ManagerModule.bsl', 'Documents',
  'ДокЗаказ', 'ManagerModule', NULL, 0, 0, 0);
INSERT INTO methods VALUES
 (1, 1, 'ЭкспортнаяФункция', 'Функция', 1, '(А)', 1, 5, 5),
 (2, 2, 'ОбработкаПроведения', 'Процедура', 0, '()', 1, 9, 9);
INSERT INTO calls VALUES
 (1, 1, 'МетодЧужой', 2, NULL, 'resolved', 1.0),
 (2, 2, 'ТестМодуль.ЭкспортнаяФункция', 3, NULL, 'resolved', 1.0);
-- Волна 1 (compass-20c): цепочка вызовов А→Б→{В,А(цикл)}, В→создание
-- ДокЗаказа и неразрешённый вызов — call_paths/who_creates/check_claims
INSERT INTO modules VALUES
 (3, 'CommonModules/Цепочка/Ext/Module.bsl', 'CommonModules',
  'ЦепочкаТест', 'Module', NULL, 0, 0, 0);
INSERT INTO methods VALUES
 (3, 3, 'ЦепочкаА', 'Функция', 1, '()', 1, 3, 3),
 (4, 3, 'ЦепочкаБ', 'Функция', 1, '()', 1, 5, 5),
 (5, 3, 'ЦепочкаВ', 'Процедура', 0, '()', 1, 7, 7);
INSERT INTO calls VALUES
 (10, 3, 'ЦепочкаБ', 2, NULL, 'resolved', 1.0),
 (11, 4, 'ЦепочкаВ', 3, NULL, 'resolved', 1.0),
 (12, 4, 'ЦепочкаА', 4, NULL, 'resolved', 1.0),
 (13, 5, 'Документы.ДокЗаказ.СоздатьДокумент', 2, NULL, 'unknown', 0.3),
 (14, 5, 'ПровестиЛогику', 5, NULL, 'unknown', 0.2);
-- ОбъектТест: реквизит шапки, ТЧ «Товары» с двумя колонками, измерение
-- (именованная вставка: новые колонки ddl.rs scale/date_fractions — NULL)
INSERT INTO object_attributes (id, object_name, category, attr_name,
    attr_synonym, attr_type, attr_kind, ts_name, source_file, length,
    precision) VALUES
 (1, 'ОбъектТест', 'CommonModules', 'Комментарий', 'Комментарий',
  '["String"]', 'attribute', NULL, 'f.xml', 0, 0),
 (2, 'ОбъектТест', 'CommonModules', 'Номенклатура', 'Номенклатура',
  '["CatalogRef.Номенклатура"]', 'ts_attribute', 'Товары', 'f.xml', 0, 0),
 (3, 'ОбъектТест', 'CommonModules', 'Количество', 'Количество',
  '["Number"]', 'ts_attribute', 'Товары', 'f.xml', 0, 0),
 (4, 'ОбъектТест', 'CommonModules', 'Организация', 'Организация',
  '["CatalogRef.Организации"]', 'dimension', NULL, 'f.xml', 0, 0),
 -- A12/P2: шапочные реквизиты ДокЗаказа — composite-обзор и verify_field
 (5, 'ДокЗаказ', 'Documents', 'Контрагент', 'Контрагент',
  '["CatalogRef.Контрагенты"]', 'attribute', NULL, 'f.xml', 0, 0),
 (6, 'ДокЗаказ', 'Documents', 'СуммаДокумента', 'Сумма документа',
  '["Number"]', 'attribute', NULL, 'f.xml', 15, 2),
 -- compass-g2r: справочник для стандартных реквизитов — в индексе один
 -- нестандартный реквизит, Код/Наименование платформа даёт сама
 (7, 'КаталогТест', 'Catalogs', 'СлужебныйРеквизит', 'Служебный',
  '["String"]', 'attribute', NULL, 'f.xml', 0, 0);
-- Хозрасчетный: атрибутов НЕТ (кейс not_indexed), но известен по ссылкам;
-- ref_object обязателен — references_to/verify_field ищут по нему
-- (реальный DDL ddl.rs: ref_object TEXT NOT NULL)
INSERT INTO metadata_references VALUES
 (1, 'ДокЗаказ', 'Documents', 'Хозрасчетный', 'attribute_type',
  'Document.ДокЗаказ.TabularSection.Товары.Attribute.Счет.Type',
  'Documents/ДокЗаказ.xml', NULL);
INSERT INTO metadata_references VALUES
 (2, 'ДокЗаказ', 'Documents', 'Хозрасчетный', 'attribute_type',
  'Document.ДокЗаказ.Attribute.СчетУчета.Type', 'Documents/ДокЗаказ.xml',
  NULL);
-- Волна 1 (who_creates): ссылка НА ДокЗаказ из другого модуля
INSERT INTO metadata_references VALUES
 (3, 'РегламентСверки', 'CommonModules', 'ДокЗаказ', 'code_call',
  'ПроверкаПередЗаписью',
  'CommonModules/РегламентСверки/Ext/Module.bsl', 12);
INSERT INTO subsystem_content VALUES
 (1, 'Продажи', 'Продажи', 'Document.ДокЗаказ', 'Subsystems/Продажи.xml');
INSERT INTO form_elements (id, object_name, category, form_name, kind,
    element_name, file) VALUES
 (1, 'ДокЗаказ', 'Documents', 'ФормаДокумента', 'field', 'Комментарий',
  'f.xml');
INSERT INTO register_movements (id, document_name, register_name, source)
 VALUES
 (1, 'ДокЗаказ', 'ТоварыНаСкладах', 'declared');
-- Три элемента у ВидыСубконтоТест: тест обрезки ждёт total=3 при limit=2
INSERT INTO predefined_items VALUES
 (1, 'ВидыСубконтоТест', 'ChartsOfAccounts', 'Субконто1', 'Субконто 1',
  '00001', NULL, 0, 'f.xml'),
 (2, 'ВидыСубконтоТест', 'ChartsOfAccounts', 'Субконто2', 'Субконто 2',
  '00002', NULL, 0, 'f.xml'),
 (3, 'ВидыСубконтоТест', 'ChartsOfAccounts', 'Субконто3', 'Субконто 3',
  '00003', NULL, 0, 'f.xml'),
 (4, 'Счета', 'ChartsOfAccounts', 'Товары', 'Товары', '41', NULL, 0, 'f.xml');
-- help-корпус: страница ИТС-руководства (ka25doc) + страница справки выгрузки
-- (primary) — тесты фильтра source_id и упоминаний ОбъектТест
INSERT INTO help_sources VALUES
 ('ka25doc', '/workspace/Help/ka25doc', ''),
 ('primary', '/workspace/Configurations/KA', '2.5');
INSERT INTO help_pages VALUES
 (1, 'ka25doc', 'Introduction/About/Ext/Help/ru.html', 'Introduction',
  'About', '', 'О программе',
  'О программе
Комплексная автоматизация: бухгалтерия, торговля, склад, расчет зарплаты.
Оформление заказа клиента описано в разделе продажи.', 160, 'sha_about'),
 (2, 'ka25doc', 'Settlements/Offsetting/Ext/Help/ru.html', 'Settlements',
  'Offsetting', '', 'Корректировка долга и зачёт аванса',
  'Корректировка долга: взаимозачёт, списание, зачёт аванса по договору.',
  90, 'sha_offset'),
 (3, 'primary', 'Documents/ДокЗаказ/Ext/Help/ru.html', 'Documents',
  'ДокЗаказ', '', 'ДокЗаказ',
  'Документ ДокЗаказ: оформление заказа клиента со склада.', 70, 'sha_order');
-- Т3: модули/методы расширений: rel_path с "../ext/" = признак слоя
-- (extensions.rs); идентификаторы ≥100 — не пересекаются с базовыми
INSERT INTO modules VALUES
 (101, '../ext/Расш1/CommonModules/Контакты/Ext/Module.bsl', 'CommonModules',
  'Контакты', 'Module', NULL, 0, 0, 0),
 (102, '../ext/Расш2/CommonModules/Контакты/Ext/Module.bsl', 'CommonModules',
  'Контакты', 'Module', NULL, 0, 0, 0),
 (103, '../ext/Расш5/CommonModules/Контакты/Ext/Module.bsl', 'CommonModules',
  'Контакты', 'Module', NULL, 0, 0, 0);
INSERT INTO methods VALUES
 (101, 101, 'ЗаполнитьКонтактнуюИнформацию_Перед', 'Процедура', 1, '()', 10, 25, 16),
 (102, 102, 'ЗаполнитьКонтактнуюИнформацию_Перед', 'Процедура', 1, '()', 5, 12, 8),
 (103, 102, 'ЗаполнитьКонтактнуюИнформацию_Вместо', 'Функция', 1, '()', 20, 40, 21),
 (104, 103, 'ЗаполнитьКонтактнуюИнформацию_Вместо', 'Функция', 1, '()', 3, 30, 28);
-- манифесты: имя/назначение/префикс (ConfigurationExtensionPurpose) + rel_root
INSERT INTO extensions VALUES
 ('Расш1', 'Patch', 'Р1_', '../ext/Расш1'),
 ('Расш2', 'Customization', 'Р2_', '../ext/Расш2'),
 ('Расш5', 'Addition', 'Р5_', '../ext/Расш5');
-- заимствования: расширения дорабатывают типовой ДокЗаказ (счётчики)
INSERT INTO extension_objects VALUES
 ('Расш1', 'Documents', 'ДокЗаказ', 'borrowed',
  '../ext/Расш1/Documents/ДокЗаказ.xml'),
 ('Расш2', 'Documents', 'ДокЗаказ', 'borrowed',
  '../ext/Расш2/Documents/ДокЗаказ.xml'),
 ('Расш5', 'Documents', 'ДокЗаказ', 'borrowed',
  '../ext/Расш5/Documents/ДокЗаказ.xml');
-- перехваты «ЭкспортнаяФункция» (ОбъектТест): Перед(Расш1), Вместо(Расш2),
-- Вместо(Расш5); source_module_id = id метода-перехватчика в methods
INSERT INTO extension_overrides (id, object_name, source_path, source_module_id,
    target_method, target_method_line, annotation, extension_name,
    extension_purpose, extension_method, extension_root, ext_module_path,
    ext_line) VALUES
 (1, 'ОбъектТест', '../ext/Расш1/ОбъектТест/Ext/ObjectModule.bsl', 101,
  'ЭкспортнаяФункция', 8, '&Перед', 'Расш1', 'Patch',
  'ЭкспортнаяФункция_Перед', '../ext/Расш1',
  '../ext/Расш1/CommonModules/Контакты/Ext/Module.bsl', 9),
 (2, 'ОбъектТест', '../ext/Расш2/ОбъектТест/Ext/ObjectModule.bsl', 102,
  'ЭкспортнаяФункция', 8, '&Вместо', 'Расш2', 'Customization',
  'ЭкспортнаяФункция_Вместо', '../ext/Расш2',
  '../ext/Расш2/CommonModules/Контакты/Ext/Module.bsl', 4),
 (3, 'ОбъектТест', '../ext/Расш5/ОбъектТест/Ext/ObjectModule.bsl', 104,
  'ЭкспортнаяФункция', 8, '&Вместо', 'Расш5', 'Addition',
  'ЭкспортнаяФункция_Вместо', '../ext/Расш5',
  '../ext/Расш5/CommonModules/Контакты/Ext/Module.bsl', 2);
-- Волна 1 (read_module/list_objects): заголовок и области модуля,
-- синонимы объектов + objects_fts (rowid = id object_synonyms, как в build.rs)
INSERT INTO module_headers VALUES
 (1, 'Тестовый общий модуль: служебные функции контура'),
 (2, 'Модуль менеджера DokЗаказа');
INSERT INTO regions VALUES
 (1, 1, 'ПрограммныйИнтерфейс', 1, 5),
 (2, 1, 'СлужебныеПроцедуры', 6, 6),
 (3, 2, 'ОбработчикиСобытий', 1, 9);
-- F3: перечисления — полное и пустое (честная пометка)
INSERT INTO enum_values VALUES
 (1, 'ХозяйственныеОперации', 'Хозяйственные операции',
  '["ПоступлениеДопРасходов", "ПродажаОпераций", "КорректировкаРегистров"]',
  'Enums/ХозяйственныеОперации.xml'),
 (2, 'ПустоеПеречисление', '', '[]', 'Enums/ПустоеПеречисление.xml');
INSERT INTO object_synonyms VALUES
 (1, 'ОбъектТест', 'CommonModules', 'Тестовый объект', 'f.xml'),
 (2, 'ДокЗаказ', 'Documents', 'Заказ клиента', 'f.xml'),
 (3, 'ТоварыНаСкладах', 'AccumulationRegisters', 'Товары на складах', 'f.xml');
INSERT INTO objects_fts(rowid, name)
 SELECT id, object_name || ' ' || synonym FROM object_synonyms;
INSERT INTO help_fts(rowid, title, object_name, text)
 SELECT id, title, object_name, text FROM help_pages;
-- A12/P2: FTS методов (строкой id = method.id — как в запросе reader'а),
-- подписка событий по ДокЗаказу, code-usages «ДокЗаказ» из модуля ОбъектТест
INSERT INTO methods_fts(rowid, name, params)
 SELECT id, name, params FROM methods;
INSERT INTO event_subscriptions (id, name, event, handler_module,
    handler_procedure, source_types) VALUES
 (1, 'ПриЗаписиПроверкаДокЗаказа', 'ПриЗаписи',
  'CommonModules/ТестМодуль/Ext/Module.bsl', 'ПроверкаПередЗаписью',
  'Document.ДокЗаказ, Document.ДокОтгрузка'),
 (2, 'ОбщаяПодпискаПроведения', 'ОбработкаПроведения',
  'CommonModules/ТестМодуль/Ext/Module.bsl', 'ПроведениеКонтроль',
  'Document.*');
-- порядок колонок как в ddl.rs: object_ref, object_ref_key ПЕРЕД member_path
INSERT INTO metadata_code_usages (id, module_id, object_ref, object_ref_key,
    member_path, usage_kind, line) VALUES
 (1, 1, 'Document.ДокЗаказ', 'Document.ДокЗаказ',
  'Документы.ДокЗаказ.Создать', 'create', 3),
 (2, 1, 'Document.ДокЗаказ', 'Document.ДокЗаказ',
  'Документы.ДокЗаказ.НайтиПоНомеру', 'call', 7);
INSERT INTO role_rights (id, role_name, object_name, right_name) VALUES
 (1, 'БазовыеПрава', 'Document.ДокЗаказ', 'Чтение'),
 (2, 'БазовыеПрава', 'Document.ДокЗаказ', 'Добавление');
INSERT INTO scheduled_jobs (id, name, method_name, handler_module,
    handler_procedure, use) VALUES
 (1, 'ОбновлениеИндекса', 'Обновить', 'ТестМодуль', 'Обновить', 'true');
INSERT INTO web_services VALUES (1, 'ОбменДанными', 'urn:compass', '[]',
  'f.xml');
INSERT INTO http_services VALUES (1, 'compass', 'compass', '[]', 'f.xml');
-- compass-1hh/v77: одно имя в двух видах (план счетов + регистр
-- бухгалтерии) — дизамбигуация по колонке category (classify.rs:39);
-- у плана ТЧ-строк нет (секции ТЧ не индексированы → None), у регистра —
-- ресурсы; ids 900+ не пересекаются с базовыми
INSERT INTO object_attributes (id, object_name, category, attr_name,
    attr_synonym, attr_type, attr_kind, ts_name, source_file, length,
    precision) VALUES
 (900, 'БухгалтерскийГлавный', 'ChartsOfAccounts', 'Валютный', 'Валютный',
  '["Boolean"]', 'attribute', NULL, 'f.xml', 0, 0),
 (901, 'БухгалтерскийГлавный', 'ChartsOfAccounts', 'Сторно', 'Сторно',
  '["Boolean"]', 'attribute', NULL, 'f.xml', 0, 0),
 (902, 'БухгалтерскийГлавный', 'AccountingRegisters', 'Организация',
  'Организация', '["CatalogRef.Организации"]', 'dimension', NULL, 'f.xml',
  0, 0),
 (903, 'БухгалтерскийГлавный', 'AccountingRegisters', 'Сумма', 'Сумма',
  '["Number"]', 'resource', NULL, 'f.xml', 15, 2),
 (904, 'БухгалтерскийГлавный', 'AccountingRegisters', 'СуммаНУ', 'Сумма НУ',
  '["Number"]', 'resource', NULL, 'f.xml', 15, 2),
 -- ТЧ-строка ДокЗаказа (v77): объект с проиндексированной секцией ТЧ —
 -- отсутствие поля среди атрибутов честно доказуемо (False)
 (905, 'ДокЗаказ', 'Documents', 'НоменклатураТЧ', 'Номенклатура',
  '["CatalogRef.Номенклатура"]', 'ts_attribute', 'Товары', 'f.xml', 0, 0);
"""


def make_mini_db(tmp_path: Path) -> Path:
    """Собрать мини-индекс во tmp; вернуть путь (для monkeypatch фасады)."""
    db = tmp_path / "mini_index.db"
    conn = sqlite3.connect(db)
    conn.executescript(SCHEMA + SEED)
    conn.commit()
    conn.close()
    return db


def make_mini_db_no_extensions(tmp_path: Path) -> Path:
    """Мини-индекс без слоя расширений — честная пустота extension_map."""
    db = make_mini_db(tmp_path)
    conn = sqlite3.connect(db)
    for table in ("extensions", "extension_objects", "extension_overrides"):
        conn.execute(f"DELETE FROM {table}")
    conn.commit()
    conn.close()
    return db


# Содержимое модулей мини-индекса: строки соответствуют сидам methods
# (ЭкспортнаяФункция: строки 1–5; ОбработкаПроведения: строки 1–9)
MODULE_SOURCES = {
    "CommonModules/ТестМодуль/Ext/Module.bsl": (
        "#Область ПрограммныйИнтерфейс\n"
        "// Экспортная функция тестового модуля\n"
        "Функция ЭкспортнаяФункция(А) Экспорт\n"
        "\tВозврат А * 2;\n"
        "КонецФункции\n"
        "#КонецОбласти\n"
    ),
    "Documents/ДокЗаказ/Ext/ManagerModule.bsl": (
        "// Модуль менеджера DokЗаказа\n"
        "Процедура ОбработкаПроведения(Отказ)\n"
        "\tЗаказ = Документы.ДокЗаказ.Создать();\n"
        "\tЗаказ.НайтиПоНомеру(\"00-001\");\n"
        "\tПровестиЛогику();\n"
        "\tИндексСтроки = 6;\n"
        "\tИндексСтроки = 7;\n"
        "\tИндексСтроки = 8;\n"
        "КонецПроцедуры\n"
    ),
}


def make_source_tree(tmp_path: Path) -> Path:
    """Дерево выгрузки под мини-индекс (rel_path → реальные файлы):
    read_function(include_body=True) читает тела через COMPASS_SOURCE_ROOT."""
    root = tmp_path / "source_root"
    for rel, text in MODULE_SOURCES.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8-sig")
    return root
