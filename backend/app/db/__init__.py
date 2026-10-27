# 数据库子包（P1-DB1）
#
# 本包集中存放数据库相关能力：
#   - engine.py  : 异步引擎、会话工厂、FastAPI 依赖 get_db、探活 ping
#   - models.py  : SQLAlchemy ORM 模型（users / conversations / messages）
#   - init_db.py : 执行规范 SQL 文件完成建表的命令行入口
#
# 规范建表 SQL 以 backend/sql/001_p1_db1_schema.sql 为唯一源头，
# ORM 模型仅用于应用层读写，字段与该 SQL 一一对应。
