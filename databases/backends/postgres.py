import logging
import typing
from collections.abc import Sequence

import asyncpg
from sqlalchemy.dialects.postgresql import pypostgresql
from sqlalchemy.engine.interfaces import Dialect
from sqlalchemy.sql import ClauseElement
from sqlalchemy.sql.ddl import DDLElement
from sqlalchemy.sql.schema import Column
from sqlalchemy.types import TypeEngine

from databases.core import LOG_EXTRA, DatabaseURL
from databases.interfaces import (
    ConnectionBackend,
    DatabaseBackend,
    Record as RecordInterface,
    TransactionBackend,
)

logger = logging.getLogger("databases")


class PostgresBackend(DatabaseBackend):
    def __init__(
        self, database_url: typing.Union[DatabaseURL, str], **options: typing.Any
    ) -> None:
        self._database_url = DatabaseURL(database_url)
        self._options = options
        self._dialect = self._get_dialect()
        self._pool = None

    def _get_dialect(self) -> Dialect:
        dialect = pypostgresql.dialect(paramstyle="pyformat")

        dialect.implicit_returning = True
        dialect.supports_native_enum = True
        dialect.supports_smallserial = True  # 9.2+
        dialect._backslash_escapes = False
        dialect.supports_sane_multi_rowcount = True  # psycopg 2.0.9+
        dialect._has_native_hstore = True
        dialect.supports_native_decimal = True

        return dialect

    def _get_connection_kwargs(self) -> dict:
        url_options = self._database_url.options

        kwargs = {}  # type: typing.Dict[str, typing.Any]
        min_size = url_options.get("min_size")
        max_size = url_options.get("max_size")
        ssl = url_options.get("ssl")

        if min_size is not None:
            kwargs["min_size"] = int(min_size)
        if max_size is not None:
            kwargs["max_size"] = int(max_size)
        if ssl is not None:
            kwargs["ssl"] = {"true": True, "false": False}[ssl.lower()]

        kwargs.update(self._options)

        return kwargs

    async def connect(self) -> None:
        assert self._pool is None, "DatabaseBackend is already running"
        kwargs = dict(
            host=self._database_url.hostname,
            port=self._database_url.port,
            user=self._database_url.username,
            password=self._database_url.password,
            database=self._database_url.database,
        )
        kwargs.update(self._get_connection_kwargs())
        self._pool = await asyncpg.create_pool(**kwargs)

    async def disconnect(self) -> None:
        assert self._pool is not None, "DatabaseBackend is not running"
        await self._pool.close()
        self._pool = None

    def connection(self) -> "PostgresConnection":
        return PostgresConnection(self, self._dialect)


class Record(RecordInterface):
    __slots__ = (
        "_row",
        "_result_columns",
        "_dialect",
        "_column_map",
        "_column_map_int",
        "_column_map_full",
    )

    def __init__(
        self,
        row: asyncpg.Record,
        result_columns: tuple,
        dialect: Dialect,
        column_maps: typing.Tuple[
            typing.Mapping[typing.Any, typing.Tuple[int, TypeEngine]],
            typing.Mapping[int, typing.Tuple[int, TypeEngine]],
            typing.Mapping[str, typing.Tuple[int, TypeEngine]],
        ],
    ) -> None:
        self._row = row
        self._result_columns = result_columns
        self._dialect = dialect
        self._column_map, self._column_map_int, self._column_map_full = column_maps

    @property
    def _mapping(self) -> typing.Mapping:
        return self._row

    def keys(self) -> typing.KeysView:
        import warnings

        warnings.warn(
            "The `Row.keys()` method is deprecated to mimic SQLAlchemy behaviour, "
            "use `Row._mapping.keys()` instead.",
            DeprecationWarning,
        )
        return self._mapping.keys()

    def values(self) -> typing.ValuesView:
        import warnings

        warnings.warn(
            "The `Row.values()` method is deprecated to mimic SQLAlchemy behaviour, "
            "use `Row._mapping.values()` instead.",
            DeprecationWarning,
        )
        return self._mapping.values()

    def __getitem__(self, key: typing.Any) -> typing.Any:
        if len(self._column_map) == 0:  # raw query
            return self._row[key]
        elif isinstance(key, Column):
            idx, datatype = self._column_map_full[str(key)]
        elif isinstance(key, int):
            idx, datatype = self._column_map_int[key]
        else:
            idx, datatype = self._column_map[key]
        raw = self._row[idx]
        processor = datatype._cached_result_processor(self._dialect, None)

        if processor is not None:
            return processor(raw)
        return raw

    def __iter__(self) -> typing.Iterator:
        return iter(self._row.keys())

    def __len__(self) -> int:
        return len(self._row)

    def __getattr__(self, name: str) -> typing.Any:
        return self._mapping.get(name)


class PostgresConnection(ConnectionBackend):
    def __init__(self, database: PostgresBackend, dialect: Dialect):
        self._database = database
        self._dialect = dialect
        self._connection = None  # type: typing.Optional[asyncpg.connection.Connection]

    async def acquire(self) -> None:
        assert self._connection is None, "Connection is already acquired"
        assert self._database._pool is not None, "DatabaseBackend is not running"
        self._connection = await self._database._pool.acquire()

    async def release(self) -> None:
        assert self._connection is not None, "Connection is not acquired"
        assert self._database._pool is not None, "DatabaseBackend is not running"
        connection, self._connection = self._connection, None
        self._connection = await self._database._pool.release(connection)

    async def fetch_all(self, query: ClauseElement) -> typing.List[RecordInterface]:
        assert self._connection is not None, "Connection is not acquired"
        query_str, args, result_columns = self._compile(query)
        rows = await self._connection.fetch(query_str, *args)
        dialect = self._dialect
        column_maps = self._create_column_maps(result_columns)
        return [Record(row, result_columns, dialect, column_maps) for row in rows]

    async def fetch_one(self, query: ClauseElement) -> typing.Optional[RecordInterface]:
        assert self._connection is not None, "Connection is not acquired"
        query_str, args, result_columns = self._compile(query)
        row = await self._connection.fetchrow(query_str, *args)
        if row is None:
            return None
        return Record(
            row,
            result_columns,
            self._dialect,
            self._create_column_maps(result_columns),
        )

    async def fetch_val(
        self, query: ClauseElement, column: typing.Any = 0
    ) -> typing.Any:
        # we are not calling self._connection.fetchval here because
        # it does not convert all the types, e.g. JSON stays string
        # instead of an object
        # see also:
        # https://github.com/encode/databases/pull/131
        # https://github.com/encode/databases/pull/132
        # https://github.com/encode/databases/pull/246
        row = await self.fetch_one(query)
        if row is None:
            return None
        return row[column]

    async def execute(self, query: ClauseElement) -> typing.Any:
        assert self._connection is not None, "Connection is not acquired"
        query_str, args, result_columns = self._compile(query)
        return await self._connection.fetchval(query_str, *args)

    async def execute_many(self, queries: typing.List[ClauseElement]) -> None:
        assert self._connection is not None, "Connection is not acquired"
        # asyncpg uses prepared statements under the hood, so we just
        # loop through multiple executes here, which should all end up
        # using the same prepared statement.
        for single_query in queries:
            single_query, args, result_columns = self._compile(single_query)
            await self._connection.execute(single_query, *args)

    async def iterate(
        self, query: ClauseElement
    ) -> typing.AsyncGenerator[typing.Any, None]:
        assert self._connection is not None, "Connection is not acquired"
        query_str, args, result_columns = self._compile(query)
        column_maps = self._create_column_maps(result_columns)
        async for row in self._connection.cursor(query_str, *args):
            yield Record(row, result_columns, self._dialect, column_maps)

    def transaction(self) -> TransactionBackend:
        return PostgresTransaction(connection=self)

    def _compile(self, query: ClauseElement) -> typing.Tuple[str, list, tuple]:
        compiled = query.compile(
            dialect=self._dialect, compile_kwargs={"render_postcompile": True}
        )

        if not isinstance(query, DDLElement):
            compiled_params = sorted(compiled.params.items())

            mapping = {
                key: "$" + str(i) for i, (key, _) in enumerate(compiled_params, start=1)
            }
            compiled_query = compiled.string % mapping

            processors = compiled._bind_processors
            args = [
                processors[key](val) if key in processors else val
                for key, val in compiled_params
            ]

            result_map = compiled._result_columns
        else:
            compiled_query = compiled.string
            args = []
            result_map = None

        query_message = compiled_query.replace(" \n", " ").replace("\n", " ")
        logger.debug(
            "Query: %s Args: %s", query_message, repr(tuple(args)), extra=LOG_EXTRA
        )
        return compiled_query, args, result_map

    @staticmethod
    def _create_column_maps(
        result_columns: tuple,
    ) -> typing.Tuple[
        typing.Mapping[typing.Any, typing.Tuple[int, TypeEngine]],
        typing.Mapping[int, typing.Tuple[int, TypeEngine]],
        typing.Mapping[str, typing.Tuple[int, TypeEngine]],
    ]:
        """
        Generate column -> datatype mappings from the column definitions.

        These mappings are used throughout PostgresConnection methods
        to initialize Record-s. The underlying DB driver does not do type
        conversion for us so we have wrap the returned asyncpg.Record-s.

        :return: Three mappings from different ways to address a column to \
                 corresponding column indexes and datatypes: \
                 1. by column identifier; \
                 2. by column index; \
                 3. by column name in Column sqlalchemy objects.
        """
        column_map, column_map_int, column_map_full = {}, {}, {}
        for idx, (column_name, _, column, datatype) in enumerate(result_columns):
            column_map[column_name] = (idx, datatype)
            column_map_int[idx] = (idx, datatype)
            column_map_full[str(column[0])] = (idx, datatype)
        return column_map, column_map_int, column_map_full

    @property
    def raw_connection(self) -> asyncpg.connection.Connection:
        assert self._connection is not None, "Connection is not acquired"
        return self._connection


class PostgresTransaction(TransactionBackend):
    def __init__(self, connection: PostgresConnection):
        self._connection = connection
        self._transaction = (
            None
        )  # type: typing.Optional[asyncpg.transaction.Transaction]

    async def start(
        self, is_root: bool, extra_options: typing.Dict[typing.Any, typing.Any]
    ) -> None:
        assert self._connection._connection is not None, "Connection is not acquired"
        self._transaction = self._connection._connection.transaction(**extra_options)
        await self._transaction.start()

    async def commit(self) -> None:
        assert self._transaction is not None
        await self._transaction.commit()

    async def rollback(self) -> None:
        assert self._transaction is not None
        await self._transaction.rollback()
