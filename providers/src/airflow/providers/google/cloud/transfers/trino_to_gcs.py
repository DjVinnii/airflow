#
# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
from __future__ import annotations

from functools import cached_property
from typing import TYPE_CHECKING, Any

from airflow.providers.google.cloud.transfers.sql_to_gcs import BaseSQLToGCSOperator
from airflow.providers.trino.hooks.trino import TrinoHook

if TYPE_CHECKING:
    from trino.client import TrinoResult
    from trino.dbapi import Cursor as TrinoCursor

    from airflow.providers.openlineage.extractors import OperatorLineage


class _TrinoToGCSTrinoCursorAdapter:
    """
    An adapter that adds additional feature to the Trino cursor.

    The implementation of cursor in the trino library is not sufficient.
    The following changes have been made:

    * The poke mechanism for row. You can look at the next row without consuming it.
    * The description attribute is available before reading the first row. Thanks to the poke mechanism.
    * the iterator interface has been implemented.

    A detailed description of the class methods is available in
    `PEP-249 <https://www.python.org/dev/peps/pep-0249/>`__.
    """

    def __init__(self, cursor: TrinoCursor):
        self.cursor: TrinoCursor = cursor
        self.rows: list[Any] = []
        self.initialized: bool = False

    @property
    def description(self) -> list[tuple]:
        """
        This read-only attribute is a sequence of 7-item sequences.

        Each of these sequences contains information describing one result column:

        * ``name``
        * ``type_code``
        * ``display_size``
        * ``internal_size``
        * ``precision``
        * ``scale``
        * ``null_ok``

        The first two items (``name`` and ``type_code``) are mandatory, the other
        five are optional and are set to None if no meaningful values can be provided.
        """
        if not self.initialized:
            # Peek for first row to load description.
            self.peekone()
        return self.cursor.description

    @property
    def rowcount(self) -> int:
        """The read-only attribute specifies the number of rows."""
        return self.cursor.rowcount

    def close(self) -> None:
        """Close the cursor now."""
        self.cursor.close()

    def execute(self, *args, **kwargs) -> TrinoResult:
        """Prepare and execute a database operation (query or command)."""
        self.initialized = False
        self.rows = []
        return self.cursor.execute(*args, **kwargs)

    def executemany(self, *args, **kwargs):
        """
        Prepare and execute a database query.

        Prepare a database operation (query or command) and then execute it against
        all parameter sequences or mappings found in the sequence seq_of_parameters.
        """
        self.initialized = False
        self.rows = []
        return self.cursor.executemany(*args, **kwargs)

    def peekone(self) -> Any:
        """Return the next row without consuming it."""
        self.initialized = True
        element = self.cursor.fetchone()
        self.rows.insert(0, element)
        return element

    def fetchone(self) -> Any:
        """Fetch the next row of a query result set, returning a single sequence, or ``None``."""
        if self.rows:
            return self.rows.pop(0)
        return self.cursor.fetchone()

    def fetchmany(self, size=None) -> list:
        """
        Fetch the next set of rows of a query result, returning a sequence of sequences.

        An empty sequence is returned when no more rows are available.
        """
        if size is None:
            size = self.cursor.arraysize

        result = []
        for _ in range(size):
            row = self.fetchone()
            if row is None:
                break
            result.append(row)

        return result

    def __next__(self) -> Any:
        """
        Return the next row from the current SQL statement using the same semantics as ``.fetchone()``.

        A ``StopIteration`` exception is raised when the result set is exhausted.
        """
        result = self.fetchone()
        if result is None:
            raise StopIteration()
        return result

    def __iter__(self) -> _TrinoToGCSTrinoCursorAdapter:
        """Return self to make cursors compatible to the iteration protocol."""
        return self


class TrinoToGCSOperator(BaseSQLToGCSOperator):
    """
    Copy data from TrinoDB to Google Cloud Storage in JSON, CSV or Parquet format.

    :param trino_conn_id: Reference to a specific Trino hook.
    """

    ui_color = "#a0e08c"

    type_map = {
        "BOOLEAN": "BOOL",
        "TINYINT": "INT64",
        "SMALLINT": "INT64",
        "INTEGER": "INT64",
        "BIGINT": "INT64",
        "REAL": "FLOAT64",
        "DOUBLE": "FLOAT64",
        "DECIMAL": "NUMERIC",
        "VARCHAR": "STRING",
        "CHAR": "STRING",
        "VARBINARY": "BYTES",
        "JSON": "STRING",
        "DATE": "DATE",
        "TIME": "TIME",
        # BigQuery don't time with timezone native.
        "TIME WITH TIME ZONE": "STRING",
        "TIMESTAMP": "TIMESTAMP",
        # BigQuery supports a narrow range of time zones during import.
        # You should use TIMESTAMP function, if you want have TIMESTAMP type
        "TIMESTAMP WITH TIME ZONE": "STRING",
        "IPADDRESS": "STRING",
        "UUID": "STRING",
    }

    def __init__(self, *, trino_conn_id: str = "trino_default", **kwargs):
        super().__init__(**kwargs)
        self.trino_conn_id = trino_conn_id

    @cached_property
    def db_hook(self) -> TrinoHook:
        return TrinoHook(trino_conn_id=self.trino_conn_id)

    def query(self):
        """Query trino and returns a cursor to the results."""
        conn = self.db_hook.get_conn()
        cursor = conn.cursor()
        self.log.info("Executing: %s", self.sql)
        cursor.execute(self.sql)
        return _TrinoToGCSTrinoCursorAdapter(cursor)

    def field_to_bigquery(self, field) -> dict[str, str]:
        """Convert trino field type to BigQuery field type."""
        clear_field_type = field[1].upper()
        # remove type argument e.g. DECIMAL(2, 10) => DECIMAL
        clear_field_type, _, _ = clear_field_type.partition("(")
        new_field_type = self.type_map.get(clear_field_type, "STRING")

        return {"name": field[0], "type": new_field_type}

    def convert_type(self, value, schema_type, **kwargs):
        """
        Do nothing. Trino uses JSON on the transport layer, so types are simple.

        :param value: Trino column value
        :param schema_type: BigQuery data type
        """
        return value

    def get_openlineage_facets_on_start(self) -> OperatorLineage | None:
        from airflow.providers.common.compat.openlineage.facet import SQLJobFacet
        from airflow.providers.common.compat.openlineage.utils.sql import get_openlineage_facets_with_sql
        from airflow.providers.openlineage.extractors import OperatorLineage

        sql_parsing_result = get_openlineage_facets_with_sql(
            hook=self.db_hook,
            sql=self.sql,
            conn_id=self.trino_conn_id,
            database=None,
        )
        gcs_output_datasets = self._get_openlineage_output_datasets()
        if sql_parsing_result:
            sql_parsing_result.outputs = gcs_output_datasets
            return sql_parsing_result
        return OperatorLineage(outputs=gcs_output_datasets, job_facets={"sql": SQLJobFacet(self.sql)})
