# -*- coding: utf-8 -*-
"""Task D2 电脑控制永久授权与安全机制单元测试。

覆盖：
- 初始默认全关：authorized=False / confirm_dangerous=True / audit_enabled=True；
- 开启 / 撤销 / 幂等；持久化（重载实例状态恢复）；
- needs_confirmation 命中黑名单；confirm 注入 True/False 行为；未注入默认 False；
- audit 写 jsonl（行数 / 字段齐全 / 中文参数摘要）；audit_enabled=False 不写；
- 与 D1 集成：authorizer 未授权时 computer_control.call_tool 抛 NotAuthorizedError。
"""

import json

import pytest

from lite.computer_control import (
    TOOL_COMMAND,
    TOOL_SCREEN,
    ControlAuthorizer,
    ComputerControl,
    NotAuthorizedError,
)


class FakeScreenBackend:
    """内存 mock 屏幕后端：返回固定字节，避免触碰真实屏幕。"""

    def __init__(self, image: bytes = b"\x89PNG\r\n\x1a\nfake-image"):
        self.image = image

    def screenshot(self, region=None) -> bytes:
        return self.image


class FakeKeyboardBackend:
    """内存 mock 键盘后端：无真实副作用。"""

    def click(self, x, y) -> None:
        pass

    def type_text(self, text) -> None:
        pass


def _audit_path(tmp_path) -> str:
    """返回本次测试用的审计日志路径。"""
    return str(tmp_path / "audit.jsonl")


# ------------------------------------------------------------------ #
# 1. 初始默认全关                                                    #
# ------------------------------------------------------------------ #


def test_init_defaults(tmp_path):
    """初始默认：授权关、高危确认开、操作审计开。"""
    auth = ControlAuthorizer(data_dir=str(tmp_path))
    assert auth.authorized is False
    assert auth.is_authorized() is False
    assert auth.confirm_dangerous is True
    assert auth.audit_enabled is True


# ------------------------------------------------------------------ #
# 2. 开启 / 撤销 / 幂等 / 持久化                                     #
# ------------------------------------------------------------------ #


def test_authorize_revoke_idempotent(tmp_path):
    """开启返回 True，重复开启返回 False（幂等）；撤销返回 True 并立即收回。"""
    auth = ControlAuthorizer(data_dir=str(tmp_path))
    assert auth.authorize() is True
    assert auth.is_authorized() is True
    # 幂等：已开启再次开启返回 False
    assert auth.authorize() is False
    assert auth.is_authorized() is True
    # 主动撤销：立即收回
    assert auth.revoke() is True
    assert auth.is_authorized() is False


def test_state_persists_across_reload(tmp_path):
    """授权后持久化；重载实例状态恢复（授权 / 高危确认 / 审计开关）。

    持久化在 authorize / revoke 时触发，故先将三要素调整到位再开启授权。
    """
    auth = ControlAuthorizer(data_dir=str(tmp_path))
    auth.confirm_dangerous = False
    auth.audit_enabled = False
    auth.authorize()

    reloaded = ControlAuthorizer(data_dir=str(tmp_path))
    assert reloaded.is_authorized() is True
    assert reloaded.confirm_dangerous is False
    assert reloaded.audit_enabled is False


def test_state_file_written_on_revoke(tmp_path):
    """撤销亦持久化：重载后保持未授权。"""
    auth = ControlAuthorizer(data_dir=str(tmp_path))
    auth.authorize()
    auth.revoke()
    reloaded = ControlAuthorizer(data_dir=str(tmp_path))
    assert reloaded.is_authorized() is False


# ------------------------------------------------------------------ #
# 3. needs_confirmation / confirm                                     #
# ------------------------------------------------------------------ #


def test_needs_confirmation_hits_blacklist(tmp_path):
    """黑名单命令（del / shutdown）需要确认；无害命令不需要。"""
    auth = ControlAuthorizer(data_dir=str(tmp_path))
    assert auth.needs_confirmation("del dirty.tmp") is True
    assert auth.needs_confirmation("shutdown /s /t 0") is True
    assert auth.needs_confirmation("dir /s") is False
    assert auth.needs_confirmation("whoami") is False


def test_needs_confirmation_hits_destructive(tmp_path):
    """黑名单之外的幂等破坏操作（如 DROP DATABASE）同样需确认。"""
    auth = ControlAuthorizer(data_dir=str(tmp_path))
    assert auth.needs_confirmation("mysql -e 'DROP DATABASE prod'") is True


def test_needs_confirmation_disabled_when_confirm_off(tmp_path):
    """confirm_dangerous=False 时即使命中黑名单也不要求确认。"""
    auth = ControlAuthorizer(data_dir=str(tmp_path))
    auth.confirm_dangerous = False
    assert auth.needs_confirmation("del x") is False


def test_confirm_injected_true_and_false(tmp_path):
    """注入 confirm_fn 时返回其调用结果（True 放行 / False 否决）。"""
    allow = ControlAuthorizer(data_dir=str(tmp_path), confirm_fn=lambda c: True)
    assert allow.confirm("del x") is True
    deny = ControlAuthorizer(data_dir=str(tmp_path), confirm_fn=lambda c: False)
    assert deny.confirm("del x") is False


def test_confirm_not_injected_default_false(tmp_path):
    """未注入 confirm_fn：需确认的命令默认否决（False）；无需确认则放行（True）。"""
    auth = ControlAuthorizer(data_dir=str(tmp_path))
    assert auth.confirm("rm -rf /") is False  # 需确认但未注入 -> 安全否决
    assert auth.confirm("whoami") is True  # 无需确认 -> 放行


# ------------------------------------------------------------------ #
# 4. audit 审计                                                      #
# ------------------------------------------------------------------ #


def test_audit_writes_jsonl_lines_and_fields(tmp_path):
    """audit 追加写 jsonl：行数正确、字段齐全、中文参数摘要保留、错误码提取。"""
    auth = ControlAuthorizer(data_dir=str(tmp_path))
    auth.audit("call_tool", TOOL_SCREEN, "region=全屏截图", True, "成功")
    auth.audit(
        "call_tool",
        TOOL_COMMAND,
        "command=删除目录数据",
        True,
        "失败：黑名单拦截 error_code=BLOCKED",
    )

    path = _audit_path(tmp_path)
    with open(path, "r", encoding="utf-8") as fh:
        lines = [ln for ln in fh.read().splitlines() if ln.strip()]

    assert len(lines) == 2
    required = {"timestamp", "tool", "args", "authorized", "result", "error_code"}
    for line in lines:
        record = json.loads(line)
        assert required.issubset(record)

    first = json.loads(lines[0])
    assert first["tool"] == TOOL_SCREEN
    assert first["args"] == "region=全屏截图"  # 中文参数摘要原样保留
    assert first["authorized"] is True
    assert first["error_code"] is None

    second = json.loads(lines[1])
    assert second["error_code"] == "BLOCKED"  # 从结果摘要提取错误码


def test_audit_disabled_writes_nothing(tmp_path):
    """audit_enabled=False 时不写审计日志文件。"""
    auth = ControlAuthorizer(data_dir=str(tmp_path))
    auth.audit_enabled = False
    auth.audit("call_tool", TOOL_SCREEN, "region=x", True, "成功")
    import os

    assert not os.path.exists(_audit_path(tmp_path))


# ------------------------------------------------------------------ #
# 5. 与 D1 集成                                                       #
# ------------------------------------------------------------------ #


def test_integration_unauthorized_raises_not_authorized(tmp_path):
    """authorizer 未授权时，D1 ComputerControl 拒绝对所有工具调用。"""
    auth = ControlAuthorizer(data_dir=str(tmp_path))
    ctrl = ComputerControl(
        authorized=auth.is_authorized(),
        screen_backend=FakeScreenBackend(),
        keyboard_backend=FakeKeyboardBackend(),
    )
    with pytest.raises(NotAuthorizedError) as exc:
        ctrl.call_tool(TOOL_SCREEN, {})
    assert exc.value.error_code == "NOT_AUTHORIZED"


def test_integration_after_authorize_allows(tmp_path):
    """authorizer 开启后，用最新 is_authorized() 构造的 D1 实例放行调用。"""
    auth = ControlAuthorizer(data_dir=str(tmp_path))
    assert auth.authorize() is True

    ctrl = ComputerControl(
        authorized=auth.is_authorized(),
        screen_backend=FakeScreenBackend(),
        keyboard_backend=FakeKeyboardBackend(),
    )
    res = ctrl.call_tool(TOOL_SCREEN, {})
    assert res.success is True


# ------------------------------------------------------------------ #
# 6. 包导出                                                           #
# ------------------------------------------------------------------ #


def test_package_exports_control_authorizer():
    """ControlAuthorizer 从包级导出。"""
    from lite import computer_control

    assert computer_control.ControlAuthorizer is ControlAuthorizer


# ------------------------------------------------------------------ #
# 7. 包裹器确认闸与授权审计（20260828_模块0_API鉴权与安全链路修复·批次A） #
# ------------------------------------------------------------------ #


def test_needs_confirmation_hits_wrappers(tmp_path):
    """N2：包裹器形态（cmd /c、powershell -c 别名、带引号绝对路径）强制进确认闸。

    修复前：黑名单为裸前缀匹配，以下形态整体绕过黑名单与确认双闸。
    """
    auth = ControlAuthorizer(data_dir=str(tmp_path))
    assert auth.needs_confirmation("cmd /c del C:\\x") is True
    assert auth.needs_confirmation("powershell -c ri C:\\x") is True
    assert auth.needs_confirmation('"C:\\Windows\\System32\\cmd.exe" /c del C:\\x') is True
    assert auth.needs_confirmation("pwsh -Command Get-Date") is True


def test_needs_confirmation_plain_commands_pass(tmp_path):
    """N2：普通无害命令不被包裹器名单误伤。"""
    auth = ControlAuthorizer(data_dir=str(tmp_path))
    assert auth.needs_confirmation("dir /s") is False
    assert auth.needs_confirmation("whoami") is False
    assert auth.needs_confirmation("notepad") is False


def test_authorize_revoke_write_audit(tmp_path):
    """N4：authorize / revoke 落盘后写审计记录（action + 来源 source=api）。"""
    auth = ControlAuthorizer(data_dir=str(tmp_path))
    assert auth.authorize() is True
    assert auth.revoke() is True

    with open(_audit_path(tmp_path), "r", encoding="utf-8") as fh:
        records = [json.loads(ln) for ln in fh.read().splitlines() if ln.strip()]

    by_action = {r["action"]: r for r in records}
    assert "authorize" in by_action
    assert "revoke" in by_action
    assert by_action["authorize"]["authorized"] is True
    assert "api" in by_action["authorize"]["args"]
    assert by_action["revoke"]["authorized"] is False
    assert "api" in by_action["revoke"]["args"]


# ------------------------------------------------------------------ #
# 批次1（第三轮体检）：审计容错 / 原子写 / 组合命令确认链               #
# ------------------------------------------------------------------ #


def test_authorize_audit_failure_does_not_rollback_state(tmp_path, monkeypatch):
    """M-2：audit 写失败（OSError）时 authorize 仍生效且不向上抛异常。"""
    auth = ControlAuthorizer(data_dir=str(tmp_path))

    def _boom(*args, **kwargs):
        raise OSError("磁盘已满")

    monkeypatch.setattr(auth, "audit", _boom)
    # 修复前：audit 抛 OSError 上传，authorized=True 却已生效（状态不一致）
    assert auth.authorize() is True
    assert auth.is_authorized() is True

    monkeypatch.setattr(auth, "audit", _boom)
    assert auth.revoke() is True
    assert auth.is_authorized() is False


def test_save_state_atomic_no_tmp_left(tmp_path):
    """L-1：_save_state 原子写完成后不残留 .tmp 中间文件。"""
    auth = ControlAuthorizer(data_dir=str(tmp_path))
    auth.authorize()
    state_path = tmp_path / "security_state.json"
    assert state_path.exists()
    assert not (tmp_path / "security_state.json.tmp").exists()
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["authorized"] is True


def test_needs_confirmation_combined_command_segments(tmp_path):
    """H-1：组合命令（& / && / | / ; / 换行 / 括号分组）逐段判定防绕过。

    修复前 ``echo hi & rd /s /q C:\\x`` 三道闸全部穿透（首 token 为 echo、
    黑名单前缀匹配不中、无 rmtree/remove/delete/drop 子串）。
    """
    auth = ControlAuthorizer(data_dir=str(tmp_path))
    # 黑名单段藏在组合命令后段 -> 整体命中（rd 本身在黑名单，直接 BLOCKED 级）
    assert auth.needs_confirmation("echo hi & rd /s /q C:\\x") is True
    # 括号分组改变首 token 的形态
    assert auth.needs_confirmation("(rd /s /q C:\\x)") is True
    # && 串接
    assert auth.needs_confirmation("cd temp&&del x") is True
    # 管道与分号
    assert auth.needs_confirmation("echo x | format q:") is True
    assert auth.needs_confirmation("echo a; shutdown /s") is True
    # 换行串接
    assert auth.needs_confirmation("echo a\nrm -rf /") is True
    # 包裹器段藏在组合命令后段 -> 进确认闸
    assert auth.needs_confirmation("echo hi & cmd /c echo ok") is True
    # 无危险段且无包裹器段的组合命令不误报
    assert auth.needs_confirmation("echo hi & echo bye") is False
    assert auth.needs_confirmation("dir /s & whoami") is False


# ------------------------------------------------------------------ #
# 批次A（第四轮体检）：控制流包裹词 / 词元级破坏判定                    #
# ------------------------------------------------------------------ #


def test_needs_confirmation_hits_control_flow_wrappers(tmp_path):
    """批次A：``if exist x del x`` / ``for ... do del ...`` 三层护栏全穿透形态补齐。

    修复前：首 token=if/for 不在包裹器名单、_is_destructive 查 delete 不查 del、
    黑名单段首前缀不命中——三层护栏全空，直接放行执行。
    """
    auth = ControlAuthorizer(data_dir=str(tmp_path))
    assert auth.needs_confirmation("if exist x del x") is True
    assert auth.needs_confirmation("for %i in (1) do del c:\\x") is True
    # 仅控制流包裹、无破坏词的命令也进确认闸（保守方向）
    assert auth.needs_confirmation("if exist file echo ok") is True


def test_is_destructive_covers_destructive_tokens(tmp_path):
    """批次A：_is_destructive 补词元级判定（del/rd/rm 等），^ 转义先剥离。"""
    assert ControlAuthorizer._is_destructive("if exist x del x") is True
    assert ControlAuthorizer._is_destructive("d^el x") is True
    assert ControlAuthorizer._is_destructive("for %i in (1) do rm x") is True
    # delete 子串判定保留兜底；无害词不含破坏词元不误报
    assert ControlAuthorizer._is_destructive("git delete-branch x") is True
    assert ControlAuthorizer._is_destructive("whoami") is False
    assert ControlAuthorizer._is_destructive("dir /s") is False