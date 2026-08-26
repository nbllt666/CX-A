# -*- coding: utf-8 -*-
"""极简 CXFC 子包（Task G2）：仅保留 ``embedded`` 进程内注册。

与 CX-O 的 CXFC（插件协议，含 direct/relay 传输 + UDP 发现 + 心跳）不同，
极简版 CXFC 退化为进程内工具注册表，供一体两面 / 内置工具挂载使用。

对外导出：
- ``LiteCXFC``：极简 CXFC 主类；
- ``CxfDisabled``：CXFC 关闭时 register/call 抛出的自定义异常；
- ``CxfToolNotFound``：调用未注册工具时抛出的自定义异常；
- ``TRANSPORT_EMBEDDED``：唯一允许的传输方式常量。
"""

from lite.cxfc.lite_cxfc import LiteCXFC, TRANSPORT_EMBEDDED, CxfDisabled, CxfToolNotFound

__all__ = [
    "LiteCXFC",
    "CxfDisabled",
    "CxfToolNotFound",
    "TRANSPORT_EMBEDDED",
]