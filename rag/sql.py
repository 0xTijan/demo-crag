from dotenv import load_dotenv
load_dotenv()
import json
import os
import re


_FORBIDDEN_SQL = re.compile(r"\b(?:DROP|DELETE|UPDATE|INSERT)\b", re.IGNORECASE)

DEFAULT_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+psycopg2://username:postgres@localhost:5432/postgres",
)


def execute_sql_query(query: str) -> str:
    """Execute a PostgreSQL query and return its result or error as JSON."""
    if not isinstance(query, str) or not query.strip():
        return json.dumps(
            {"status": "error", "error": "Query must be a non-empty string."}
        )

    if _FORBIDDEN_SQL.search(query):
        return json.dumps(
            {"status": "error", "error": "Query contains a forbidden SQL operation."}
        )

    connection = None
    try:
        import psycopg2
        from psycopg2.extras import RealDictCursor

        database_url = DEFAULT_URL.replace(
            "postgresql+psycopg2://", "postgresql://", 1
        )
        connection = psycopg2.connect(database_url)

        with connection:
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute("SET TRANSACTION READ ONLY")
                cursor.execute(query)
                rows = (
                    [dict(row) for row in cursor.fetchall()]
                    if cursor.description
                    else []
                )

        return json.dumps(
            {
                "status": "success",
                "row_count": len(rows),
                "message": "Query returned 0 rows." if not rows else "Query succeeded.",
                "rows": rows,
            },
            default=str,
        )
    except Exception as exc:
        return json.dumps({"status": "error", "error": str(exc)})
    finally:
        if connection is not None:
            connection.close()