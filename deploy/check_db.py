#!/usr/bin/env python3
"""Read-only connection check. Never prints credentials or creates tables."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps/api"))
from agentloom import store as db

try:
    if db.backend() == "postgresql":
        row = db.query(
            "SELECT current_database() AS database, current_user AS username, "
            "current_schema() AS schema, current_setting('server_version') AS version",
            one=True,
        )
        print("PostgreSQL 连接成功：", row)
    else:
        db.query("SELECT 1", one=True)
        print("SQLite 连接成功：", db.DB)
except ValueError as exc:
    print(str(exc), file=sys.stderr)
    raise SystemExit(1) from None
except Exception:
    print("数据库连接失败，请检查 SSH 隧道、.env 和数据库权限；凭据未输出。", file=sys.stderr)
    raise SystemExit(1) from None
finally:
    db.close()
