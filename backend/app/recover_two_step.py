"""在部署主机或应用容器内恢复唯一启用超级管理员的两步验证。"""

import argparse
import asyncio

from app.core.config import get_settings
from app.core.database import create_engine, create_sessionmaker
from app.core.errors import AppError
from app.services.two_step_recovery import recover_on_host


async def recover(user_id: int) -> None:
    engine = create_engine(get_settings().database_url)
    try:
        async with create_sessionmaker(engine)() as db:
            await recover_on_host(db, user_id)
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user-id", required=True, type=int, help="被锁住的唯一启用超级管理员的用户 ID")
    parser.add_argument("--confirm", action="store_true", required=True, help="确认重置该账号的两步验证")
    args = parser.parse_args()
    if args.user_id <= 0:
        parser.error("用户 ID 必须为正整数")
    try:
        asyncio.run(recover(args.user_id))
    except AppError as exc:
        parser.exit(1, f"{exc.detail}\n")
    print(f"已重置用户 #{args.user_id} 的两步验证，并记录本机恢复审计。请使用原登录方式重新登录。")


if __name__ == "__main__":
    main()
