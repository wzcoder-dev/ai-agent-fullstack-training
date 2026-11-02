# 会话测试说明

在本项目根目录运行：

```sh
python -m pytest tests/test_session.py -q
```

测试入口：`tests/test_session.py`。

覆盖会话用户与到期时间、到期前有效、恰好到期无效、到期后无效，以及空用户拒绝。
默认登录会话有效期为 30 分钟。

上述命令是运行方法，运行是否成功以实际执行结果为准。
