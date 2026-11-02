# 登录会话边界

## 结论
登录会话有效条件是 now < expires_at。恰好到期时应返回 False，不能使用 now <= expires_at。
这项结论在现有 session-rules.md 与 app/session.py 中仍成立。
来源：history/2026-09-12-expiry-bug.md 的结论；本次核对现行规则后整理。
