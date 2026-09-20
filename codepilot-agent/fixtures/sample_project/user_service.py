"""用户查询与统计模块 —— CodePilot Agent 的端到端样例。

这个文件是**故意埋雷**的：它包含了四个维度的典型缺陷，用于验证审查→定位→修复→回归
整条链路。每处缺陷都用 ``# [SEED:n]`` 标记，与 ``tests/test_workflow_e2e.py`` 里的
预期一一对应。真实项目里不会有这些注释。
"""
import hashlib
import os
import sqlite3

# [SEED:1] 硬编码的凭据 —— Security / P0。锚点唯一，应当被精确定位并修复。
DB_PASSWORD = "admin123456"
API_TOKEN = "sk-live-9f8e7d6c5b4a39281706"


def get_user_by_name(conn, username):
    """按用户名查询用户。"""
    cursor = conn.cursor()
    # [SEED:2] SQL 注入 —— Security / P0。锚点跨两行，用于验证多行定位与 patch。
    query = "SELECT * FROM users WHERE name = '" + username + "'"
    cursor.execute(query)
    return cursor.fetchone()


def safe_get_user_by_name(conn, username):
    """参数化查询的正确写法，作为对照组，不应被报告为问题。"""
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM users WHERE name = ?", (username,))
    return cursor.fetchone()


def render_profile(user):
    """渲染用户主页。"""
    # [SEED:3] 空指针 —— Logic / P1。当 user 为 None 时崩溃。
    name = user["name"]
    return "<h1>{}</h1>".format(name)


def compute_pairwise(items):
    """两两相乘求和。"""
    total = 0
    # [SEED:4] 嵌套循环 —— Performance / P2。O(n^2)，且重复计算。
    for i in range(len(items)):
        for j in range(len(items)):
            total += items[i] * items[j]
    return total


def hash_password_bad(password):
    """密码哈希。"""
    # [SEED:5] 弱哈希 —— Security / P1。MD5 已不适合用于密码。
    return hashlib.md5(password.encode()).hexdigest()


def ReadConfig(path):
    """读配置。"""
    # [SEED:6] 命名规范 —— Style / P3。函数名不是 snake_case。
    with open(path) as f:
        return f.read()


def divide(a, b):
    """除法。"""
    # [SEED:7] 除零与异常吞噬 —— Logic / P1。
    try:
        return a / b
    except Exception:
        return None


def process_batch(rows):
    """批量处理。"""
    results = []
    for row in rows:
        # [SEED:8] N+1 查询 —— Performance / P2。循环内建连接。
        conn = sqlite3.connect("app.db")
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM orders WHERE user_id = ?", (row["id"],))
        results.append(cursor.fetchall())
    return results
