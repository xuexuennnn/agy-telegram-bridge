# Contributing

欢迎提交小而可审查的改动。请勿提交真实 token、账号导出、聊天记录、运行状态、第三方二进制或生产路径。

提交前运行：

```sh
python -m unittest discover -s tests -q
python -m py_compile bot.py rescue_core.py tests/test_*.py
python -m pip check
git diff --check
```

安全边界的修改应附回归测试，并说明 Bubblewrap、进程清理、回调确认或凭证事务为何仍然安全。
