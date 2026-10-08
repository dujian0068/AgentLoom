# 可信 Python Hooks 示例

在仓库根目录运行，无需模型 Key、数据库或网络：

```bash
PYTHON_DOTENV_DISABLED=1 PYTHONPATH=packages/runtime .venv/bin/python examples/hooks/demo.py
```

示例使用确定性的 `DemoGateway` 和内存检查点，演示完整 Agent Loop：

1. 模型生成 `limit=500` 的工具请求。
2. `tool.before` 将上限改为 3，平台重新校验参数。
3. 工具返回记录数和内部备注，原始结果先进入检查点。
4. `tool.after` 只向模型提供记录数。
5. `model.chat.after` 去掉回答前后的空白，完成检查使用独立的 purpose。

实际接入方式见[Hooks 运行时使用说明](../../docs/hooks-runtime-v0.1.md)。示例的内存检查点仅用于演示；部署时需要传入真实持久化回调。此目录里的代码属于宿主信任的扩展，不是团队上传代码的隔离执行器。
