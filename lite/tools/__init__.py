# -*- coding: utf-8 -*-
"""内置工具系统子包（Task G2）：承载电脑控制 / 记忆读写 / 系统信息。

替代 CX-O 中"经 CXFC 注册工具"的部分职责——核心能力直接内置，云端 / 本地 LLM
通过 ``BuiltinToolRegistry.call`` 进程内直接调用。

对外导出：
- ``BuiltinToolRegistry``：内置工具注册表主类；
- ``SOURCE_BUILTIN``：内置工具来源标识常量。
"""

from lite.tools.builtin_registry import SOURCE_BUILTIN, BuiltinToolRegistry

__all__ = ["BuiltinToolRegistry", "SOURCE_BUILTIN"]