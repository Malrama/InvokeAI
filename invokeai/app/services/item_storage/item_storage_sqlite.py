from typing import Generic, Optional, TypeVar, get_args

from pydantic import BaseModel, TypeAdapter

from invokeai.app.services.shared.sqlite.sqlite_database import SqliteDatabase

from .item_storage_base import ItemStorageABC

T = TypeVar("T", bound=BaseModel)


class SqliteItemStorage(ItemStorageABC, Generic[T]):
    def __init__(self, db: SqliteDatabase, table_name: str, id_field: str = "id"):
        super().__init__()

        self._lock = db.lock
        self._conn = db.conn
        self._table_name = table_name
        self._id_field = id_field  # TODO: validate that T has this field
        self._cursor = self._conn.cursor()
        self._validator: Optional[TypeAdapter[T]] = None

        self._create_table()

    def _create_table(self):
        try:
            self._lock.acquire()
            self._cursor.execute(
                f"""CREATE TABLE IF NOT EXISTS {self._table_name} (
                item TEXT,
                id TEXT GENERATED ALWAYS AS (json_extract(item, '$.{self._id_field}')) VIRTUAL NOT NULL);"""
            )
            self._cursor.execute(
                f"""CREATE UNIQUE INDEX IF NOT EXISTS {self._table_name}_id ON {self._table_name}(id);"""
            )
        finally:
            self._lock.release()

    def _parse_item(self, item: str) -> T:
        if self._validator is None:
            """
            We don't get access to `__orig_class__` in `__init__()`, and we need this before start(), so
            we can create it when it is first needed instead.
            __orig_class__ is technically an implementation detail of the typing module, not a supported API
            """
            self._validator = TypeAdapter(get_args(self.__orig_class__)[0])  # type: ignore [attr-defined]
        return self._validator.validate_json(item)

    def set(self, item: T):
        try:
            self._lock.acquire()
            self._cursor.execute(
                f"""INSERT OR REPLACE INTO {self._table_name} (item) VALUES (?);""",
                (item.model_dump_json(warnings=False, exclude_none=True),),
            )
            self._conn.commit()
        finally:
            self._lock.release()
        self._on_changed(item)

    def get(self, id: str) -> Optional[T]:
        try:
            self._lock.acquire()
            self._cursor.execute(f"""SELECT item FROM {self._table_name} WHERE id = ?;""", (str(id),))
            result = self._cursor.fetchone()
        finally:
            self._lock.release()

        if not result:
            return None

        return self._parse_item(result[0])

    def delete(self, id: str):
        try:
            self._lock.acquire()
            self._cursor.execute(f"""DELETE FROM {self._table_name} WHERE id = ?;""", (str(id),))
            self._conn.commit()
        finally:
            self._lock.release()
        self._on_deleted(id)
