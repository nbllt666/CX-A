# -*- coding: utf-8 -*-
"""Task A10 安装程序单元测试（pytest，tmp_path 临时根目录）。

覆盖：
- ensure_dirs 幂等且目录齐全
- init_workplace 后 config.json 存在、memories.db 建表成功
- verify_components 在缺组件时返回警告不抛错
- first_run 全流程驱动：注入 input → cloud.provider=deepseek、
  api_key 写入且加密往返、本地小 LLM 提示输出
"""

import json
import os
import sqlite3

import pytest

from installer import bootstrap
from installer.first_run import FirstRunDriver
from lite.config.config_manager import ConfigManager


# ------------------------------------------------------------------ #
# ensure_dirs：幂等 + 目录齐全                                        #
# ------------------------------------------------------------------ #

def test_ensure_dirs_idempotent_and_complete(tmp_path):
    """ensure_dirs 幂等，且 data/ 系列与 logs/ 目录齐全，memories.db 占位存在。"""
    root = str(tmp_path)

    dirs = bootstrap.ensure_dirs(root)
    bootstrap.ensure_dirs(root)  # 二次调用应幂等，不报错

    for rel in ["data", os.path.join("data", "lancedb"),
                os.path.join("data", "local_llm"), os.path.join("data", "voices"),
                "logs"]:
        assert os.path.isdir(os.path.join(root, rel)), f"缺少目录 {rel}"
    assert os.path.exists(os.path.join(root, "data", "memories.db"))
    assert len(dirs) >= 5


# ------------------------------------------------------------------ #
# init_workplace：config.json + memories.db 建表                      #
# ------------------------------------------------------------------ #

def test_init_workplace_config_and_db(tmp_path):
    """init_workplace 后 config.json 生成、memories.db 完成建表。"""
    root = str(tmp_path)
    bootstrap.ensure_dirs(root)

    cfg = bootstrap.init_workplace(root)

    # config.json 存在且含默认云端提供商
    assert os.path.exists(os.path.join(root, "config.json"))
    assert cfg.get("cloud", "provider") == "deepseek"

    # memories.db 已建表
    db_path = os.path.join(root, "data", "memories.db")
    assert os.path.exists(db_path)
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='memories'"
        ).fetchone()
    finally:
        conn.close()
    assert row is not None


# ------------------------------------------------------------------ #
# verify_components：缺组件时返回警告不抛错                            #
# ------------------------------------------------------------------ #

def test_verify_components_returns_warnings_not_raise(tmp_path):
    """空白根目录缺少必需组件时，返回警告列表而不抛错。"""
    root = str(tmp_path)  # 全新空目录：目录缺失 + 组件待装

    problems = bootstrap.verify_components(root)

    assert isinstance(problems, list)
    assert len(problems) > 0
    # 提示文案包含"待装态"或"缺少"等中文警告
    joined = "\n".join(problems)
    assert ("待装态" in joined) or ("缺少" in joined)


def test_verify_components_after_ensure_reduces_warnings(tmp_path):
    """ensure_dirs 后再校验，数据目录相关警告应消除（组件待装仅为开发态提示）。"""
    root = str(tmp_path)
    bootstrap.ensure_dirs(root)

    problems = bootstrap.verify_components(root)

    # 不再出现"缺少必需数据目录"类问题
    assert not any("缺少必需数据目录" in p for p in problems)


# ------------------------------------------------------------------ #
# first_run：全流程驱动                                              #
# ------------------------------------------------------------------ #

def test_first_run_full_flow(tmp_path):
    """注入默认输入，验证 provider=deepseek、api_key 加密往返、本地小 LLM 提示。"""
    root = str(tmp_path)
    bootstrap.ensure_dirs(root)

    # 注入序列：提供商留空→默认 deepseek；API Key；本地小 LLM 无输入
    responses = iter(["", "sk-super-secret", ""])
    output_lines = []
    driver = FirstRunDriver(
        root,
        input_fn=lambda _prompt: next(responses),
        output_fn=output_lines.append,
    )

    result = driver.run()

    # 步骤1：默认 deepseek
    assert result["provider"] == "deepseek"
    assert driver.cm.get("cloud", "provider") == "deepseek"

    # 步骤2：api_key 写入并加密往返
    assert result["api_key"] == "sk-super-secret"
    raw = json.loads(open(os.path.join(root, "config.json"), encoding="utf-8").read())
    assert raw["cloud"]["api_key"].startswith("cxa_enc:")
    assert "sk-super-secret" not in raw["cloud"]["api_key"]
    # 重新加载还原明文
    cfg2 = ConfigManager(
        config_path=os.path.join(root, "config.json"),
        data_dir=os.path.join(root, "data"),
    )
    assert cfg2.get("cloud", "api_key") == "sk-super-secret"

    # 步骤4：本地小 LLM 引导提示输出 + source=modelscope
    joined = "\n".join(output_lines)
    assert "本地小 LLM" in joined
    assert "1.7B" in joined
    assert "data/local_llm/" in joined
    assert driver.cm.get("local_llm", "source") == "modelscope"

    # 步骤3：CX-OPEN 音色提示
    assert "CX-OPEN" in joined


def test_first_run_api_key_blank_skips(tmp_path):
    """API Key 留空时跳过，不写入，且提示可在设置页补填。"""
    root = str(tmp_path)
    bootstrap.ensure_dirs(root)

    responses = iter(["tongyi", "", ""])
    output_lines = []
    driver = FirstRunDriver(
        root, input_fn=lambda _p: next(responses), output_fn=output_lines.append
    )

    result = driver.run()

    assert result["provider"] == "tongyi"
    assert result["api_key"] == ""
    joined = "\n".join(output_lines)
    assert "可在设置页补填" in joined
    # 未填写则不写入 key
    raw = json.loads(open(os.path.join(root, "config.json"), encoding="utf-8").read())
    assert raw["cloud"]["api_key"] == ""