---
name: report-helper
description: 把用户提供的资料整理成 Markdown 报告，并输出文件。
---

读取用户资料，整理标题、结论与来源。
使用 workspace_write 将报告保存为 report.md。
需要统计字符数时，可通过 workspace_command 运行：
python /skills/<skill_id>/scripts/count.py /workspace/report.md
脚本需要 Docker 运行环境；没有环境时只生成报告文件。
