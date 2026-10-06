from urllib.parse import urlsplit

import psycopg

from app.config import get_settings


def main() -> None:
    database_url = get_settings().database_url
    if not database_url:
        raise RuntimeError("DATABASE_URL 尚未配置")
    connection_url = database_url.replace("postgresql+psycopg://", "postgresql://", 1)
    with psycopg.connect(connection_url) as connection:
        database, user = connection.execute("SELECT current_database(), current_user").fetchone()
        extensions = dict(
            connection.execute(
                """
                SELECT extname, extversion FROM pg_extension
                WHERE extname IN ('pg_trgm', 'vector')
                """
            ).fetchall()
        )
    host = urlsplit(connection_url).hostname
    print(f"连接成功：database={database}, user={user}, host={host}")
    print(f"pg_trgm 扩展：{extensions.get('pg_trgm', '未启用')}")
    print(f"vector 扩展：{extensions.get('vector', '未启用')}")


if __name__ == "__main__":
    main()
