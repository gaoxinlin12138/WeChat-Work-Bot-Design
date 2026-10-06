"""交互式初始化本地 PostgreSQL；管理员密码只在本机终端输入。"""

from getpass import getpass
from pathlib import Path
from secrets import token_urlsafe
from urllib.parse import quote

import psycopg
from psycopg import sql

HOST = "127.0.0.1"
PORT = 5432
ADMIN_USER = "postgres"
APP_USER = "wecom_bot"
APP_DATABASE = "wecom_knowledge_bot"


def main() -> None:
    admin_password = getpass("请输入安装时设置的 postgres 管理员密码：")
    app_password = token_urlsafe(36)

    with psycopg.connect(
        host=HOST,
        port=PORT,
        dbname="postgres",
        user=ADMIN_USER,
        password=admin_password,
        autocommit=True,
    ) as connection:
        role_exists = connection.execute(
            "SELECT 1 FROM pg_roles WHERE rolname = %s", (APP_USER,)
        ).fetchone()
        if role_exists:
            connection.execute(
                sql.SQL("ALTER ROLE {} WITH LOGIN PASSWORD {}").format(
                    sql.Identifier(APP_USER),
                    sql.Literal(app_password),
                )
            )
        else:
            connection.execute(
                sql.SQL("CREATE ROLE {} WITH LOGIN PASSWORD {}").format(
                    sql.Identifier(APP_USER),
                    sql.Literal(app_password),
                )
            )

        database_exists = connection.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (APP_DATABASE,)
        ).fetchone()
        if not database_exists:
            connection.execute(
                sql.SQL("CREATE DATABASE {} OWNER {}").format(
                    sql.Identifier(APP_DATABASE),
                    sql.Identifier(APP_USER),
                )
            )

    with psycopg.connect(
        host=HOST,
        port=PORT,
        dbname=APP_DATABASE,
        user=ADMIN_USER,
        password=admin_password,
        autocommit=True,
    ) as connection:
        connection.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
        # pgvector must be enabled before the application role initializes HNSW tables.
        # If the server package is missing, fail here with a clear setup error.
        connection.execute("CREATE EXTENSION IF NOT EXISTS vector")
        connection.execute(
            sql.SQL("ALTER DATABASE {} OWNER TO {}").format(
                sql.Identifier(APP_DATABASE),
                sql.Identifier(APP_USER),
            )
        )
        connection.execute(
            sql.SQL("GRANT ALL ON SCHEMA public TO {}").format(sql.Identifier(APP_USER))
        )

    encoded_password = quote(app_password, safe="")
    database_url = (
        f"postgresql+psycopg://{APP_USER}:{encoded_password}@{HOST}:{PORT}/{APP_DATABASE}"
    )
    project_root = Path(__file__).resolve().parents[1]
    (project_root / ".database.env").write_text(f"DATABASE_URL={database_url}\n", encoding="utf-8")
    print("初始化成功：项目数据库和专用账号已创建，本机连接配置已保存。")


if __name__ == "__main__":
    main()
