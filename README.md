# Code MCP Skills

面向编码工作的技能集合：代码质量审查与改进，以及工程工具依赖的统一管理。

## toolbox-skill

[toolbox-skill](toolbox-skill/SKILL.md) 把所有便携式命令行工具集中安装到一个共享目录，先用先查、缺失再下载，多个工程复用同一份可执行文件，避免每个工程各自往临时目录下载一份缓存。附带 `toolbox` 命令行工具、内置 recipe 与项目清单同步。

## code-skill

[code-skill](code-skill/SKILL.md) 是技能入口，按需加载模块，提供代码审查、可读性与可维护性分析、重构建议和遗留代码渐进改进计划。

## 相关模块

| 模块 | 简介 |
| --- | --- |
| [clean-code-reviewer](code-skill/modules/clean-code-reviewer/module.md) | 基于 Robert C. Martin 的 Clean Code 原则审查代码，提供具体问题说明、重构示例和分阶段改进建议；附带参考资料、示例与可选预分析脚本。 |
