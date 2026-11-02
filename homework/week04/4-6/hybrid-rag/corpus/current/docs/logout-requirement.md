# 当前退出登录需求（尚未实现）

## 需要改的地方
本需求仍作为待实现方案，不表示代码已完成退出功能。
- app/auth.py：实现 logout()，删除 _TOKEN_STORE 中的令牌记录。
- app/session.py：增加 invalidate(token) 辅助函数，按原需求优先采用此方案。
- tests/test_session.py：增加 test_logout_invalidates。

## 验收标准
退出登录后，再用原令牌调用 check()，必须返回 False。
执行 python -m pytest tests/test_session.py -q，全部通过，不改既有测试期望值。

## 来源与范围
来源：history/2026-10-06-logout-request.md 的需要改的地方与验收标准。
保留待实现需求，不将旧背景时长或未决的服务端缓存清理当作当前规则。
