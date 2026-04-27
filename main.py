# ============================================================================
# ПРОГРАММА: Конвертер StarUML диаграмм в SQL код для PostgreSQL
# Назначение: Парсит текстовое описание диаграммы в формате StarUML
#            и генерирует SQL-скрипт для создания базы данных
# Автор: Разработано для дипломного проекта
# Версия: 2.0
# ============================================================================

# ----------------------------------------------------------------------------
# ИМПОРТ НЕОБХОДИМЫХ МОДУЛЕЙ
# ----------------------------------------------------------------------------
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
import uvicorn
import re
import zlib
import os
from typing import Dict, List, Tuple, Optional
from pydantic import BaseModel

# ============================================================================
# ИНИЦИАЛИЗАЦИЯ FASTAPI ПРИЛОЖЕНИЯ
# ============================================================================
app = FastAPI(title="StarUML to SQL Converter")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ============================================================================
# МОДЕЛИ ДАННЫХ (Pydantic)
# ============================================================================
class StarUMLRequest(BaseModel):
    staruml_code: str

# ============================================================================
# КЛАССЫ ДЛЯ ХРАНЕНИЯ ПРОПАРСЕННОЙ ИНФОРМАЦИИ
# ============================================================================

class Entity:
    def __init__(self, name: str, display_name: str = ""):
        self.name = name
        self.display_name = display_name
        self.attributes = []
        self.pk = []
        self.uk = []
        self.fk = []
    
    def add_attribute(self, name: str, data_type: str, is_pk: bool = False, 
                      is_fk: bool = False, is_uk: bool = False):
        self.attributes.append((name, data_type, is_pk, is_fk, is_uk))
        if is_pk:
            self.pk.append(name)
        if is_uk:
            self.uk.append(name)
        if is_fk:
            self.fk.append(name)

class Relationship:
    def __init__(self, from_entity: str, to_entity: str, relation_type: str, label: str = ""):
        self.from_entity = from_entity
        self.to_entity = to_entity
        self.relation_type = relation_type
        self.label = label

# ============================================================================
# ПАРСЕР STARUML
# ============================================================================

class StarUMLParser:
    def __init__(self, staruml_code: str):
        self.code = staruml_code
        self.entities: Dict[str, Entity] = {}
        self.relationships: List[Relationship] = []
        self.many_to_many = []
    
    def parse(self):
        lines = self.code.strip().split('\n')
        current_entity = None
        
        for line in lines:
            line = line.strip()
            
            if line.startswith("'") or line.startswith("@startuml") or line.startswith("@enduml") or line.startswith("!theme"):
                continue
            
            # ПОИСК СУЩНОСТИ
            entity_match = re.match(r'entity\s+(?:"([^"]+)"\s+as\s+)?(\w+)\s*{', line)
            
            if entity_match:
                display_name = entity_match.group(1) if entity_match.group(1) else entity_match.group(2)
                entity_name = entity_match.group(2)
                current_entity = Entity(entity_name, display_name)
                self.entities[entity_name] = current_entity
                continue
            
            # ПОИСК АТРИБУТОВ
            if current_entity and not line.startswith('}'):
                if line == '--':
                    continue
                
                # Поддержка русских символов и * для PK
                attr_match = re.match(r'([*+]?)\s*([\w\u0400-\u04FF\s]+?)\s*:\s*(\w+)(?:\s*<<(.*?)>>)?', line, re.UNICODE)
                
                if attr_match:
                    modifier = attr_match.group(1)
                    attr_name = attr_match.group(2).strip()
                    attr_type = attr_match.group(3)
                    constraints = attr_match.group(4) if attr_match.group(4) else ""
                    
                    is_pk = 'PK' in constraints or modifier == '*'
                    is_fk = 'FK' in constraints
                    is_uk = 'UK' in constraints
                    
                    # Очищаем имя атрибута
                    attr_name = attr_name.lower().replace(' ', '_')
                    
                    current_entity.add_attribute(attr_name, attr_type, is_pk, is_fk, is_uk)
            
            # ПОИСК СВЯЗЕЙ
            rel_match = re.match(r'(\w+)\s*([\|}o][o\|]{0,2}--[o\|]{0,2}[\|o{]?)\s*(\w+)', line)
            
            if rel_match:
                from_entity = rel_match.group(1)
                rel_type = rel_match.group(2)
                to_entity = rel_match.group(3)
                
                if from_entity in self.entities and to_entity in self.entities:
                    rel = Relationship(from_entity, to_entity, rel_type, "")
                    self.relationships.append(rel)
                    
                    if '}o--o{' in rel_type:
                        self.many_to_many.append((from_entity, to_entity))
        
        return self.entities, self.relationships, self.many_to_many

# ============================================================================
# ГЕНЕРАТОР SQL КОДА
# ============================================================================

class SQLGenerator:
    def __init__(self, entities: Dict[str, Entity], relationships: List[Relationship], 
                 many_to_many: List[Tuple]):
        self.entities = entities
        self.relationships = relationships
        self.many_to_many = many_to_many
    
    def _map_type(self, staruml_type: str) -> str:
        mapping = {
            'int': 'INTEGER',
            'integer': 'INTEGER',
            'string': 'VARCHAR(255)',
            'varchar': 'VARCHAR(255)',
            'text': 'TEXT',
            'datetime': 'TIMESTAMP',
            'timestamp': 'TIMESTAMP',
            'date': 'DATE',
            'boolean': 'BOOLEAN',
            'bool': 'BOOLEAN',
            'enum': 'VARCHAR(50)',
            'float': 'FLOAT',
            'double': 'DOUBLE PRECISION',
            'decimal': 'DECIMAL(10,2)',
            'numeric': 'NUMERIC(15,2)'
        }
        return mapping.get(staruml_type.lower(), 'VARCHAR(255)')
    
    def _quote_ident(self, name: str) -> str:
        reserved_keywords = {'user', 'group', 'table', 'column', 'index', 
                            'foreign', 'primary', 'key', 'order'}
        
        if name.lower() in reserved_keywords:
            return f'"{name}"'
        return name
    
    def _get_pk_columns(self, entity_name: str) -> List[str]:
        entity = self.entities.get(entity_name)
        if not entity:
            return []
        return [pk.lower() for pk in entity.pk]
    
    def _get_pk_column(self, entity_name: str) -> Optional[str]:
        pk_cols = self._get_pk_columns(entity_name)
        if len(pk_cols) == 1:
            return pk_cols[0]
        return None
    
    def _determine_parent_child(self, rel: Relationship) -> Tuple[Optional[str], Optional[str]]:
        rel_type = rel.relation_type
        
        if rel_type.startswith('||') and ('o{' in rel_type or 'o|' in rel_type):
            return rel.from_entity, rel.to_entity
        elif ('o{' in rel_type or 'o|' in rel_type) and rel_type.endswith('||'):
            return rel.to_entity, rel.from_entity
        elif rel_type == '||--||':
            return rel.from_entity, rel.to_entity
        
        return None, None
    
    def generate(self) -> str:
        sql = []
        
        sql.append("-- SQL код для PostgreSQL")
        sql.append("-- Сгенерировано из StarUML диаграммы")
        sql.append("-- ВНИМАНИЕ: Проверьте имена таблиц и колонок перед выполнением!\n")
        
        # Создаем словарь для хранения внешних ключей
        foreign_keys = []
        
        # СОЗДАНИЕ ТАБЛИЦ
        for entity_name, entity in self.entities.items():
            table_name = entity_name.lower()
            
            sql.append(f"-- Таблица: {entity.display_name if entity.display_name else entity_name}")
            sql.append(f"CREATE TABLE IF NOT EXISTS {self._quote_ident(table_name)} (")
            
            attrs_sql = []
            
            for attr_name, attr_type, is_pk, is_fk, is_uk in entity.attributes:
                sql_type = self._map_type(attr_type)
                col_name = attr_name.lower()
                
                # NOT NULL для PK
                null_constraint = "NOT NULL" if is_pk else ""
                
                # PRIMARY KEY для одиночного ключа
                pk_constraint = " PRIMARY KEY" if is_pk and len(entity.pk) == 1 else ""
                
                # UNIQUE ограничение
                unique_constraint = " UNIQUE" if is_uk else ""
                
                # AUTO_INCREMENT для определенных полей
                auto_increment = ""
                if is_pk and len(entity.pk) == 1 and col_name in ['id', 'код_товара', 'номер_заказа', 'номер_поставки', 'номер_отгрузки']:
                    auto_increment = " GENERATED BY DEFAULT AS IDENTITY"
                
                attr_sql = f"    {self._quote_ident(col_name)} {sql_type}{auto_increment} {null_constraint}{pk_constraint}{unique_constraint}".strip()
                attrs_sql.append(attr_sql)
                
                # Сохраняем информацию о внешнем ключе для последующего добавления
                if is_fk and len(entity.pk) != 1:  # Не первичный ключ
                    foreign_keys.append((table_name, col_name, None, None))
            
            # Составной первичный ключ
            if len(entity.pk) > 1:
                pk_attrs = ", ".join([self._quote_ident(pk.lower()) for pk in entity.pk])
                attrs_sql.append(f"    PRIMARY KEY ({pk_attrs})")
            
            if not attrs_sql:
                attrs_sql.append(f"    {self._quote_ident('id')} INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY")
            
            sql.append(",\n".join(attrs_sql))
            sql.append(f");\n")
        
        # ДОБАВЛЕНИЕ ВНЕШНИХ КЛЮЧЕЙ
        sql.append("-- Внешние ключи")
        fk_added = set()
        
        for rel in self.relationships:
            parent, child = self._determine_parent_child(rel)
            
            if parent and child and parent in self.entities and child in self.entities:
                parent_entity = self.entities[parent]
                child_entity = self.entities[child]
                
                # Определяем колонку внешнего ключа
                fk_columns = []
                
                # Ищем атрибуты с FK в дочерней таблице
                for attr_name, _, _, is_fk, _ in child_entity.attributes:
                    if is_fk:
                        # Проверяем, ссылается ли этот FK на родителя
                        if parent.lower() in attr_name.lower() or 'код' in attr_name.lower() or 'id' in attr_name.lower():
                            fk_columns.append(attr_name.lower())
                
                # Если не нашли, создаем стандартное имя
                if not fk_columns:
                    parent_pk = self._get_pk_column(parent)
                    if parent_pk:
                        fk_columns.append(f"{parent.lower()}_{parent_pk}")
                    else:
                        fk_columns.append(f"{parent.lower()}_id")
                
                # Добавляем внешние ключи
                parent_pk_columns = self._get_pk_columns(parent)
                
                if parent_pk_columns:
                    for i, fk_col in enumerate(fk_columns):
                        if i < len(parent_pk_columns):
                            fk_name = f"fk_{child.lower()}_{parent.lower()}"
                            if fk_name not in fk_added:
                                sql.append(f"\n-- Связь: {parent} -> {child}")
                                sql.append(f"ALTER TABLE {self._quote_ident(child.lower())}")
                                sql.append(f"    ADD CONSTRAINT {fk_name}")
                                sql.append(f"    FOREIGN KEY ({self._quote_ident(fk_col)})")
                                sql.append(f"    REFERENCES {self._quote_ident(parent.lower())}({self._quote_ident(parent_pk_columns[i])})")
                                sql.append(f"    ON DELETE RESTRICT ON UPDATE CASCADE;")
                                fk_added.add(fk_name)
        
        # Добавляем внешние ключи для связей many-to-many
        for from_ent, to_ent in self.many_to_many:
            table_name = f"{from_ent.lower()}_{to_ent.lower()}"
            fk_name1 = f"fk_{table_name}_{from_ent.lower()}"
            fk_name2 = f"fk_{table_name}_{to_ent.lower()}"
            
            if fk_name1 not in fk_added:
                sql.append(f"\n-- Внешний ключ для связи {from_ent} -> {table_name}")
                sql.append(f"ALTER TABLE {self._quote_ident(table_name)}")
                sql.append(f"    ADD CONSTRAINT {fk_name1}")
                sql.append(f"    FOREIGN KEY ({self._quote_ident(f'{from_ent.lower()}_id')})")
                sql.append(f"    REFERENCES {self._quote_ident(from_ent.lower())}({self._quote_ident('id')})")
                sql.append(f"    ON DELETE CASCADE ON UPDATE CASCADE;")
                fk_added.add(fk_name1)
            
            if fk_name2 not in fk_added:
                sql.append(f"\n-- Внешний ключ для связи {to_ent} -> {table_name}")
                sql.append(f"ALTER TABLE {self._quote_ident(table_name)}")
                sql.append(f"    ADD CONSTRAINT {fk_name2}")
                sql.append(f"    FOREIGN KEY ({self._quote_ident(f'{to_ent.lower()}_id')})")
                sql.append(f"    REFERENCES {self._quote_ident(to_ent.lower())}({self._quote_ident('id')})")
                sql.append(f"    ON DELETE CASCADE ON UPDATE CASCADE;")
                fk_added.add(fk_name2)
        
        return "\n".join(sql)


# ============================================================================
# ФУНКЦИЯ КОДИРОВАНИЯ ДЛЯ STARUML
# ============================================================================

def encode_staruml(text: str) -> str:
    def encode6bit(b):
        if b < 10:
            return chr(48 + b)
        b -= 10
        if b < 26:
            return chr(65 + b)
        b -= 26
        if b < 26:
            return chr(97 + b)
        b -= 26
        if b == 0:
            return '-'
        if b == 1:
            return '_'
        return '?'
    
    def append3bytes(b1, b2, b3):
        c1 = b1 >> 2
        c2 = ((b1 & 0x3) << 4) | (b2 >> 4)
        c3 = ((b2 & 0xF) << 2) | (b3 >> 6)
        c4 = b3 & 0x3F
        return (encode6bit(c1 & 0x3F) + encode6bit(c2 & 0x3F) +
                encode6bit(c3 & 0x3F) + encode6bit(c4 & 0x3F))
    
    compressed = zlib.compress(text.encode("utf-8"))[2:-4]
    res = ""
    i = 0
    while i < len(compressed):
        b1 = compressed[i]
        b2 = compressed[i + 1] if i + 1 < len(compressed) else 0
        b3 = compressed[i + 2] if i + 2 < len(compressed) else 0
        res += append3bytes(b1, b2, b3)
        i += 3
    return res


# ============================================================================
# HTML ШАБЛОН
# ============================================================================

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>StarUML to SQL Converter</title>
    <style>
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }
        
        body {
            font-family: 'SF Mono', Monaco, 'Cascadia Code', 'Consolas', monospace;
            background: #1a1a1a;
            color: #e0e0e0;
            min-height: 100vh;
            padding: 20px;
        }
        
        .container {
            max-width: 1800px;
            margin: 0 auto;
        }
        
        h1 {
            text-align: center;
            margin-bottom: 25px;
            font-weight: 400;
            font-size: 2em;
            color: #88c0d0;
            letter-spacing: 1px;
            border-bottom: 1px solid #3b4252;
            padding-bottom: 15px;
        }
        
        .examples-panel {
            background: #2e3440;
            border: 1px solid #3b4252;
            border-radius: 8px;
            padding: 15px;
            margin-bottom: 20px;
        }
        
        .examples-title {
            color: #88c0d0;
            margin-bottom: 12px;
            font-size: 13px;
            text-transform: uppercase;
            letter-spacing: 1px;
        }
        
        .examples-grid {
            display: flex;
            gap: 12px;
            flex-wrap: wrap;
        }
        
        .example-btn {
            background: #3b4252;
            border: 1px solid #434c5e;
            color: #e5e9f0;
            padding: 8px 16px;
            border-radius: 6px;
            cursor: pointer;
            font-size: 13px;
            font-family: inherit;
            transition: all 0.2s;
        }
        
        .example-btn:hover {
            background: #434c5e;
            border-color: #88c0d0;
            color: #88c0d0;
        }
        
        .main-panel {
            display: grid;
            grid-template-columns: 1fr 1.5fr 1fr;
            gap: 20px;
            margin-bottom: 20px;
        }
        
        .panel {
            background: #2e3440;
            border: 1px solid #3b4252;
            border-radius: 8px;
            display: flex;
            flex-direction: column;
            height: 650px;
        }
        
        .panel-header {
            padding: 12px 16px;
            border-bottom: 1px solid #3b4252;
            background: #3b4252;
            border-radius: 8px 8px 0 0;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        
        .panel-header h3 {
            font-weight: 400;
            font-size: 13px;
            color: #e5e9f0;
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }
        
        .panel-actions {
            display: flex;
            gap: 8px;
        }
        
        .panel-actions button {
            background: #434c5e;
            border: none;
            color: #e5e9f0;
            padding: 4px 10px;
            border-radius: 4px;
            cursor: pointer;
            font-size: 12px;
            font-family: inherit;
        }
        
        .panel-actions button:hover {
            background: #4c566a;
            color: #88c0d0;
        }
        
        .panel-content {
            flex: 1;
            overflow: hidden;
            background: #1a1a1a;
        }
        
        textarea {
            width: 100%;
            height: 100%;
            padding: 16px;
            border: none;
            background: #1a1a1a;
            color: #e5e9f0;
            font-family: 'SF Mono', Monaco, 'Consolas', monospace;
            font-size: 13px;
            line-height: 1.6;
            resize: none;
            outline: none;
        }
        
        .sql-output {
            height: 100%;
            overflow: auto;
            background: #1a1a1a;
            color: #a3be8c;
            padding: 16px;
            font-family: 'SF Mono', Monaco, 'Consolas', monospace;
            font-size: 13px;
            line-height: 1.6;
            white-space: pre-wrap;
        }
        
        .diagram-container {
            height: 100%;
            overflow: auto;
            background: #ffffff;
            display: flex;
            justify-content: center;
            align-items: flex-start;
            padding: 20px;
        }
        
        .diagram-container img {
            max-width: 100%;
            height: auto;
            border-radius: 4px;
            box-shadow: 0 4px 12px rgba(0,0,0,0.3);
        }
        
        .status-bar {
            background: #434c5e;
            color: #e5e9f0;
            padding: 8px 16px;
            border-radius: 6px;
            font-size: 13px;
            text-align: right;
        }
        
        .error-message {
            color: #bf616a;
            padding: 16px;
            background: #3b4252;
            border-left: 3px solid #bf616a;
            margin: 16px;
            font-family: monospace;
            white-space: pre-wrap;
        }
        
        .loading {
            color: #88c0d0;
            padding: 20px;
            text-align: center;
        }
        
        .info-note {
            background: #3b4252;
            border-left: 3px solid #88c0d0;
            padding: 10px 15px;
            margin-top: 20px;
            font-size: 12px;
            color: #e5e9f0;
        }
        
        .info-note code {
            background: #2e3440;
            padding: 2px 5px;
            border-radius: 3px;
            color: #88c0d0;
        }
    </style>
</head>
<body>
    <div class="container">
        <h1>STARUML → SQL CONVERTER (PostgreSQL)</h1>
        
        <div class="examples-panel">
            <div class="examples-title">📐 ПРИМЕРЫ ДИАГРАММ</div>
            <div class="examples-grid">
                <button class="example-btn" id="example1">Номенклатура и заказы</button>
                <button class="example-btn" id="example2">Роли пользователей</button>
                <button class="example-btn" id="example3">Видеоконференции</button>
            </div>
        </div>
        
        <div class="main-panel">
            <div class="panel">
                <div class="panel-header">
                    <h3>📝 STARUML КОД</h3>
                    <div class="panel-actions">
                        <button id="clearBtn">Очистить</button>
                    </div>
                </div>
                <div class="panel-content">
                    <textarea id="starumlInput" placeholder="Введите StarUML код..."></textarea>
                </div>
            </div>
            
            <div class="panel">
                <div class="panel-header">
                    <h3>🖼️ ДИАГРАММА</h3>
                    <div class="panel-actions">
                        <button id="renderBtn">Обновить</button>
                    </div>
                </div>
                <div class="panel-content">
                    <div id="diagramContainer" class="diagram-container">
                        <div class="loading">Введите код и нажмите "Обновить"</div>
                    </div>
                </div>
            </div>
            
            <div class="panel">
                <div class="panel-header">
                    <h3>🗄️ SQL КОД</h3>
                    <div class="panel-actions">
                        <button id="copyBtn">Копировать</button>
                        <button id="downloadBtn">Скачать</button>
                    </div>
                </div>
                <div class="panel-content">
                    <div id="sqlOutput" class="sql-output">-- SQL код появится после конвертации</div>
                </div>
            </div>
        </div>
        
        <div class="status-bar" id="statusBar">
            Готов к работе
        </div>
        
        <div class="info-note">
            💡 <strong>Примечание:</strong> Для корректного распознавания внешних ключей используйте тег <code>&lt;&lt;FK&gt;&gt;</code> или <code>&lt;&lt;PK, FK&gt;&gt;</code>. 
            Первичные ключи обозначайте символом <code>*</code> в начале строки атрибута или тегом <code>&lt;&lt;PK&gt;&gt;</code>.
            Пример: <code>* Код товара : int &lt;&lt;PK&gt;&gt;</code>
        </div>
    </div>

    <script>
        const EXAMPLES = {
            example1: `@startuml
' =============================
' Диаграмма - Номенклатура и заказы
' =============================

entity "Номенклатура" as nomen {
  * Код товара : int <<PK>>
  --
  Наименование : string
  Единица измерения : string
  Поставщик : string
}

entity "Заказ клиента" as "order" {
  * Номер заказа : int <<PK>>
  --
  Дата заказа : datetime
  Срок поставки : datetime
  Клиент : string
  Склад : string
}

entity "Поступление товаров" as incoming {
  * Номер поставки : int <<PK>>
  --
  Дата поставки : datetime
  Поставщик : string
  Партия : string
  Срок годности : datetime
  Код товара : int <<FK>>
}

entity "Реализация товаров" as outgoing {
  * Номер отгрузки : int <<PK>>
  --
  Дата отгрузки : datetime
  Контрагент : string
  Код товара : int <<FK>>
}

entity "Остатки на складах" as remains {
  * Склад : string
  * Код товара : int <<FK>>
  * Номер партии : string
  * Срок годности : datetime
  --
  Количество : numeric
}

nomen ||--o{ incoming
nomen ||--o{ outgoing
incoming ||--o{ remains
outgoing ||--o{ remains
"order" ||--|| outgoing

@enduml`,
            
            example2: `@startuml
' =============================
' Диаграмма - Роли пользователя
' =============================

entity "Пользователь" as User {
  * id : int <<PK>>
  --
  имя : string
  email : string
  дата_регистрации : datetime
  тип_пользователя : string
}

entity "Постоянный" as Regular {
  * id : int <<PK, FK>>
  --
  последний_визит : datetime
  аватар : string
}

entity "Модератор" as Moderator {
  * id : int <<PK, FK>>
  --
  уровень_прав : int
  дата_назначения : datetime
}

entity "Гость" as Guest {
  * id : int <<PK, FK>>
  --
  срок_действия_ссылки : datetime
  организация : string
}

User ||--o| Regular
User ||--o| Moderator
User ||--o| Guest

@enduml`,
            
            example3: `@startuml
' =============================
' Диаграмма - Видеоконференции
' =============================

entity users {
  * id : int <<PK>>
  --
  username : varchar
  email : varchar <<UK>>
  password_hash : varchar
  created_at : timestamp
}

entity rooms {
  * id : int <<PK>>
  --
  name : varchar
  creator_id : int <<FK>>
  created_at : timestamp
  is_active : boolean
}

entity participants {
  * user_id : int <<PK, FK>>
  * room_id : int <<PK, FK>>
  --
  joined_at : timestamp
}

entity devices {
  * id : int <<PK>>
  --
  user_id : int <<FK>>
  device_type : enum
  device_name : varchar
}

entity messages {
  * id : int <<PK>>
  --
  content : text
  sent_at : timestamp
  sender_id : int <<FK>>
  room_id : int <<FK>>
}

users ||--o{ devices
users ||--o{ messages
rooms ||--o{ messages
users }o--o{ participants
rooms }o--o{ participants

@enduml`
        };

        const starumlInput = document.getElementById('starumlInput');
        const sqlOutput = document.getElementById('sqlOutput');
        const diagramContainer = document.getElementById('diagramContainer');
        const statusBar = document.getElementById('statusBar');

        async function renderDiagram() {
            const code = starumlInput.value;
            if (!code.trim()) {
                diagramContainer.innerHTML = '<div class="loading">Введите StarUML код</div>';
                return;
            }

            diagramContainer.innerHTML = '<div class="loading">Загрузка диаграммы...</div>';
            sqlOutput.textContent = '-- Конвертация...';
            
            try {
                const [renderResponse, convertResponse] = await Promise.all([
                    fetch('/render', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ staruml_code: code })
                    }),
                    fetch('/convert', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ staruml_code: code })
                    })
                ]);
                
                const renderData = await renderResponse.json();
                const convertData = await convertResponse.json();
                
                if (renderResponse.ok) {
                    const img = new Image();
                    img.onload = () => {
                        diagramContainer.innerHTML = '';
                        diagramContainer.appendChild(img);
                    };
                    img.onerror = () => {
                        diagramContainer.innerHTML = '<div class="error-message">Ошибка загрузки диаграммы</div>';
                    };
                    img.src = renderData.image_url;
                    img.alt = 'StarUML Diagram';
                    img.style.maxWidth = '100%';
                } else {
                    diagramContainer.innerHTML = `<div class="error-message">${renderData.detail}</div>`;
                }
                
                if (convertResponse.ok) {
                    sqlOutput.textContent = convertData.sql;
                    updateStatus('Конвертация завершена');
                } else {
                    sqlOutput.textContent = `-- Ошибка: ${convertData.detail}`;
                    updateStatus('Ошибка конвертации', true);
                }
                
            } catch (error) {
                diagramContainer.innerHTML = `<div class="error-message">Ошибка: ${error.message}</div>`;
                sqlOutput.textContent = `-- Ошибка: ${error.message}`;
                updateStatus('Ошибка', true);
            }
        }

        function copySQL() {
            const sql = sqlOutput.textContent;
            if (sql && !sql.includes('Ошибка') && !sql.includes('Введите')) {
                navigator.clipboard.writeText(sql).then(() => {
                    updateStatus('SQL скопирован');
                }).catch(() => {
                    updateStatus('Ошибка копирования', true);
                });
            }
        }

        function downloadSQL() {
            const sql = sqlOutput.textContent;
            if (sql && !sql.includes('Ошибка') && !sql.includes('Введите')) {
                const blob = new Blob([sql], { type: 'text/plain' });
                const url = URL.createObjectURL(blob);
                const a = document.createElement('a');
                a.href = url;
                a.download = 'schema.sql';
                document.body.appendChild(a);
                a.click();
                document.body.removeChild(a);
                URL.revokeObjectURL(url);
                updateStatus('SQL скачан');
            }
        }

        function loadExample(exampleKey) {
            starumlInput.value = EXAMPLES[exampleKey];
            renderDiagram();
            updateStatus('Пример загружен');
        }

        function clearAll() {
            starumlInput.value = '';
            diagramContainer.innerHTML = '<div class="loading">Введите StarUML код</div>';
            sqlOutput.textContent = '-- SQL код появится после конвертации';
            updateStatus('Готов к работе');
        }

        function updateStatus(message, isError = false) {
            statusBar.textContent = message;
            statusBar.style.background = isError ? '#bf616a' : '#434c5e';
        }

        document.getElementById('example1').addEventListener('click', () => loadExample('example1'));
        document.getElementById('example2').addEventListener('click', () => loadExample('example2'));
        document.getElementById('example3').addEventListener('click', () => loadExample('example3'));
        document.getElementById('renderBtn').addEventListener('click', renderDiagram);
        document.getElementById('copyBtn').addEventListener('click', copySQL);
        document.getElementById('downloadBtn').addEventListener('click', downloadSQL);
        document.getElementById('clearBtn').addEventListener('click', clearAll);

        loadExample('example1');
    </script>
</body>
</html>"""


# ============================================================================
# API ЭНДПОЙНТЫ FASTAPI
# ============================================================================

@app.get("/", response_class=HTMLResponse)
async def root():
    return HTML_TEMPLATE

@app.post("/convert")
async def convert(request: StarUMLRequest):
    try:
        parser = StarUMLParser(request.staruml_code)
        entities, relationships, many_to_many = parser.parse()
        
        if not entities:
            raise HTTPException(status_code=400, detail="Не удалось распознать сущности")
        
        generator = SQLGenerator(entities, relationships, many_to_many)
        sql = generator.generate()
        
        return {"sql": sql}
    
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/render")
async def render(request: StarUMLRequest):
    try:
        encoded = encode_staruml(request.staruml_code)
        image_url = f"https://www.plantuml.com/plantuml/png/{encoded}"
        return {"image_url": image_url}
    
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

# ============================================================================
# ТОЧКА ВХОДА В ПРИЛОЖЕНИЕ (АДАПТИРОВАНО ДЛЯ RENDER)
# ============================================================================
if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(
        app, 
        host="0.0.0.0",  # Важно: для Render нужно 0.0.0.0
        port=port,
        log_level="info"
    )