# -*- coding: utf-8 -*-
"""极简 CXFC 子包（Task G2 基线 + Task H1 relay 扩展）。

与 CX-O 的 CXFC（插件协议）对齐：保留 ``embedded`` 进程内注册，新增
``relay`` 前端转接传输（投递-等待-回报链路 + 防重放）；``direct`` 传输与
UDP 发现 / 心跳仍被砍掉（极简版无网络服务依赖）。

对外导出：
- ``LiteCXFC``：极简 CXFC 主类（embedded 注册 + relay 转接）；
- ``CxfDisabled``：CXFC 关闭时 register/call 抛出的自定义异常；
- ``CxfToolNotFound``：调用未注册工具时抛出的自定义异常；
- ``TRANSPORT_EMBEDDED`` / ``TRANSPORT_RELAY``：传输方式常量；
- ``RELAY_UNREACHABLE`` / ``RELAY_TIMEOUT`` / ``REPLAY_DETECTED``：relay 稳定
  错误码（以返回值 error_code 表达，对齐 CX-O 返回外壳）；
- ``RELAY_CALL_TYPE``：relay 投递 dict 的 type 字段值；
- ``DEFAULT_RELAY_TIMEOUT_S`` / ``DEFAULT_REPLAY_WINDOW_S``：窗口默认值。
"""

from lite.cxfc.lite_cxfc import (  # noqa: F401
    DEFAULT_RELAY_TIMEOUT_S,
    DEFAULT_REPLAY_WINDOW_S,
    RELAY_CALL_TYPE,
    RELAY_TIMEOUT,
    RELAY_UNREACHABLE,
    REPLAY_DETECTED,
    TRANSPORT_EMBEDDED,
    TRANSPORT_RELAY,
    CxfDisabled,
    CxfToolNotFound,
    LiteCXFC,
)

__all__ = [
    "LiteCXFC",
    "CxfDisabled",
    "CxfToolNotFound",
    "TRANSPORT_EMBEDDED",
    "TRANSPORT_RELAY",
    "RELAY_UNREACHABLE",
    "RELAY_TIMEOUT",
    "REPLAY_DETECTED",
    "RELAY_CALL_TYPE",
    "DEFAULT_RELAY_TIMEOUT_S",
    "DEFAULT_REPLAY_WINDOW_S",
]
