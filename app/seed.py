"""
初始化用户目录数据（非事件溯源部分）。
运行: python -m app.seed
"""
from sqlalchemy.orm import Session
from .db import SessionLocal, init_db, UserDirectory, now_utc
from .domain.permissions import UserRole


def seed_users(db: Session):
    users = [
        ("u-zhangsan", "张三", "zhangsan@example.com", UserRole.MEMBER.value, "team-a", False),
        ("u-lisi", "李四", "lisi@example.com", UserRole.MEMBER.value, "team-a", False),
        ("u-wangwu", "王五", "wangwu@example.com", UserRole.TEAM_ADMIN.value, "team-a", True),
        ("u-zhaoliu", "赵六", "zhaoliu@example.com", UserRole.MEMBER.value, "team-b", False),
        ("u-sunqi", "孙七", "sunqi@example.com", UserRole.TEAM_ADMIN.value, "team-b", True),
        ("u-recep", "前台小李", "reception@example.com", UserRole.RECEPTIONIST.value, None, False),
        ("u-admin", "系统管理员", "admin@example.com", UserRole.SYSTEM_ADMIN.value, None, False),
    ]
    created = 0
    for uid, name, email, role, team_id, ta in users:
        existing = db.query(UserDirectory).filter(UserDirectory.user_id == uid).first()
        if existing:
            existing.name = name
            existing.email = email
            existing.role = role
            existing.team_id = team_id
            existing.team_admin = ta
        else:
            db.add(UserDirectory(
                user_id=uid, name=name, email=email, role=role,
                team_id=team_id, team_admin=ta, created_at=now_utc(),
            ))
            created += 1
    db.commit()
    return created


def main():
    init_db()
    db = SessionLocal()
    try:
        c = seed_users(db)
        print(f"初始化完成，新增用户 {c} 条")
        for u in db.query(UserDirectory).all():
            print(f"  [{u.role}] {u.user_id} {u.name} team={u.team_id}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
