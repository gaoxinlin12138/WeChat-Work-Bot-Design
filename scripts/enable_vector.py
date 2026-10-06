"""在本机数据库启用 pgvector；管理员密码只从终端读取。"""

from getpass import getpass

import psycopg


def main() -> None:
    admin_password = getpass("请输入 postgres 管理员密码（不会回显）：")
    with psycopg.connect(
        host="127.0.0.1",
        port=5432,
        dbname="wecom_knowledge_bot",
        user="postgres",
        password=admin_password,
        autocommit=True,
    ) as connection:
        connection.execute("CREATE EXTENSION IF NOT EXISTS vector")
        version = connection.execute(
            "SELECT extversion FROM pg_extension WHERE extname = 'vector'"
        ).fetchone()[0]
    print(f"vector 扩展已启用，版本：{version}")


if __name__ == "__main__":
    main()
