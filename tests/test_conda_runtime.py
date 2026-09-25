# -*- coding: utf-8 -*-
"""内置 conda 运行时安装单测（installer/conda_runtime）。

覆盖：
- install_conda：已就位幂等跳过 / NSIS 静默参数正确 / 源缺失告警 / 失败不抛
- create_voice_env：conda create 命令形状（tuna 通道 + --override-channels）/
  环境已存在跳过 / conda 未安装时跳过
全部用注入 runner（绝不真实执行安装器 / conda / 触网）。
"""

import os

from installer import conda_runtime


def _touch(path, payload=b"x"):
    """在 path 处写一个小文件（自动建父目录），用于伪造安装产物。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(payload)


# ------------------------------------------------------------------ #
# install_conda                                                       #
# ------------------------------------------------------------------ #

def test_install_conda_skips_when_already_installed(tmp_path):
    """conda.exe 已存在：直接跳过，不调用 runner。"""
    root = str(tmp_path)
    _touch(conda_runtime.conda_exe_path(root))
    calls = []

    def fake_runner(cmd):
        """记录调用（本用例应零调用）。"""
        calls.append(list(cmd))
        return 0, ""

    result = conda_runtime.install_conda(root, runner=fake_runner)

    assert result["installed"] is True
    assert result["skipped"] is True
    assert result["reason"] == "already-installed"
    assert calls == []


def test_install_conda_runs_silent_installer_and_verifies(tmp_path):
    """源存在且 runner 产出 conda.exe：装成功，NSIS 参数为 /S + /D=<target>。"""
    root = str(tmp_path)
    bundled = tmp_path / "bundled"
    bundled.mkdir()
    src = bundled / conda_runtime.CONDA_INSTALLER_NAME
    src.write_bytes(b"MZ")
    calls = []

    def fake_runner(cmd):
        """模拟 NSIS 安装器：记录命令并产出 conda.exe。"""
        calls.append(list(cmd))
        _touch(conda_runtime.conda_exe_path(root))
        return 0, "installed"

    result = conda_runtime.install_conda(root, runner=fake_runner, bundled_dir=str(bundled))

    assert result["installed"] is True
    assert result["skipped"] is False
    target = os.path.join(root, "runtime", "conda")
    assert calls == [[str(src), "/S", f"/D={target}"]]


def test_install_conda_source_missing_warns_and_returns(tmp_path, capsys, monkeypatch):
    """源缺失：告警并返回 source-missing，不抛错（失败不阻断主安装）。

    真实仓库的 installer/bundled/ 下已有安装器源，故此处 monkeypatch
    ``BUNDLED_DIR`` 指向不存在目录，构造"三候选取值全空"的源缺失场景。
    """
    root = str(tmp_path)
    monkeypatch.setattr(conda_runtime, "BUNDLED_DIR", str(tmp_path / "no-such-bundled"))

    result = conda_runtime.install_conda(root, runner=lambda cmd: (0, ""))

    assert result["installed"] is False
    assert result["skipped"] is True
    assert result["reason"] == "source-missing"
    out = capsys.readouterr().out
    assert "源缺失" in out


def test_install_conda_failure_not_raise(tmp_path, capsys):
    """runner 返回非 0：installed=False、reason=install-failed，且不抛错。"""
    root = str(tmp_path)
    bundled = tmp_path / "bundled"
    bundled.mkdir()
    (bundled / conda_runtime.CONDA_INSTALLER_NAME).write_bytes(b"MZ")

    result = conda_runtime.install_conda(
        root, runner=lambda cmd: (1, "boom"), bundled_dir=str(bundled)
    )

    assert result["installed"] is False
    assert result["reason"] == "install-failed"
    assert result["returncode"] == 1
    assert "boom" in result["output_tail"]
    assert "不影响主安装" in capsys.readouterr().out


def test_find_installer_source_priority(tmp_path):
    """源查找优先级：显式 bundled_dir > runtime/_bundled > installer/bundled。"""
    root = str(tmp_path)
    explicit = tmp_path / "explicit"
    explicit.mkdir()
    bundled_src = explicit / conda_runtime.CONDA_INSTALLER_NAME
    bundled_src.write_bytes(b"MZ")

    assert conda_runtime.find_installer_source(root, bundled_dir=str(explicit)) == str(bundled_src)

    # 显式目录无源时回退 runtime/_bundled
    _touch(os.path.join(root, "runtime", "_bundled", conda_runtime.CONDA_INSTALLER_NAME))
    assert conda_runtime.find_installer_source(root, bundled_dir=str(tmp_path / "empty")) == os.path.join(
        root, "runtime", "_bundled", conda_runtime.CONDA_INSTALLER_NAME
    )


# ------------------------------------------------------------------ #
# create_voice_env                                                    #
# ------------------------------------------------------------------ #

def test_create_voice_env_builds_conda_create_command(tmp_path):
    """conda 已装且 runner 产出环境 python：命令含 -y / python=3.12 / tuna 通道 + --override-channels。"""
    root = str(tmp_path)
    _touch(conda_runtime.conda_exe_path(root))
    calls = []

    def fake_runner(cmd):
        """模拟 conda create：记录命令并产出环境 python.exe。"""
        calls.append(list(cmd))
        _touch(conda_runtime.voice_env_python(root))
        return 0, "done"

    result = conda_runtime.create_voice_env(root, runner=fake_runner)

    assert result["created"] is True
    assert result["skipped"] is False
    cmd = calls[0]
    assert cmd[0] == conda_runtime.conda_exe_path(root)
    assert cmd[1] == "create"
    assert "-p" in cmd and "-y" in cmd
    assert f"python={conda_runtime.VOICE_PYTHON_VERSION}" in cmd
    assert "--override-channels" in cmd
    assert conda_runtime.CONDA_MIRROR_MAIN in cmd
    # 通道只出现一次（默认单通道），且 -c 紧跟通道值
    assert cmd.count("-c") == 1
    assert cmd[cmd.index("-c") + 1] == conda_runtime.CONDA_MIRROR_MAIN


def test_create_voice_env_skips_when_env_exists(tmp_path):
    """环境 python.exe 已存在：幂等跳过，不调用 runner。"""
    root = str(tmp_path)
    _touch(conda_runtime.voice_env_python(root))
    calls = []

    result = conda_runtime.create_voice_env(root, runner=lambda cmd: calls.append(cmd) or (0, ""))

    assert result["created"] is True
    assert result["skipped"] is True
    assert result["reason"] == "already-created"
    assert calls == []


def test_create_voice_env_requires_conda(tmp_path, capsys):
    """内置 conda 未安装：跳过并告警 conda-missing，不抛错。"""
    root = str(tmp_path)

    result = conda_runtime.create_voice_env(root, runner=lambda cmd: (0, ""))

    assert result["created"] is False
    assert result["reason"] == "conda-missing"
    assert "内置 conda 未安装" in capsys.readouterr().out


def test_create_voice_env_custom_channels_and_version(tmp_path):
    """自定义通道与版本：命令按入参生成（多通道依序展开）。"""
    root = str(tmp_path)
    _touch(conda_runtime.conda_exe_path(root))
    calls = []

    def fake_runner(cmd):
        """记录命令并产出环境 python（验证自定义参数）。"""
        calls.append(list(cmd))
        _touch(conda_runtime.voice_env_python(root))
        return 0, ""

    conda_runtime.create_voice_env(
        root,
        runner=fake_runner,
        python_version="3.11",
        conda_channels=["https://example.test/a", "https://example.test/b"],
    )

    cmd = calls[0]
    assert "python=3.11" in cmd
    assert cmd[cmd.index("-c") + 1] == "https://example.test/a"
    assert cmd[cmd.index("-c", cmd.index("-c") + 1) + 1] == "https://example.test/b"


# ------------------------------------------------------------------ #
# rewrite_pip_command：sidecar 解释器命令改写                          #
# ------------------------------------------------------------------ #

def test_rewrite_pip_command_variants():
    """pip install 命令改写为 sidecar 解释器执行；注释/非 pip 命令返回 None。"""
    assert conda_runtime.rewrite_pip_command(
        "pip install torch --index-url https://example.test/whl", r"C:\env\python.exe"
    ) == [r"C:\env\python.exe", "-m", "pip", "install", "torch", "--index-url", "https://example.test/whl"]
    assert conda_runtime.rewrite_pip_command("# 说明性注释", r"C:\env\python.exe") is None
    assert conda_runtime.rewrite_pip_command("conda create -p x", r"C:\env\python.exe") is None
    assert conda_runtime.rewrite_pip_command("pip", r"C:\env\python.exe") is None


# ------------------------------------------------------------------ #
# install_voice_dependencies：GPU 分叉命令装配 + sidecar 内执行          #
# ------------------------------------------------------------------ #

def test_install_voice_dependencies_cuda_chain(tmp_path):
    """cuda 结论：先推理依赖（torch cu128 专用源），再 MeloTTS 链与随包源码安装。"""
    root = str(tmp_path)
    _touch(conda_runtime.voice_env_python(root))
    calls = []

    def fake_runner(cmd):
        """记录 sidecar 内执行的命令（全部成功）。"""
        calls.append(list(cmd))
        return 0, "ok"

    result = conda_runtime.install_voice_dependencies(
        root, "cuda", cuda_version="13.4", channel="mirror", runner=fake_runner
    )

    assert result["installed"] is True
    assert result["errors"] == []
    # 全部命令均经 sidecar 解释器执行
    env_python = conda_runtime.voice_env_python(root)
    assert all(cmd[0] == env_python and cmd[1:4] == ["-m", "pip", "install"] for cmd in calls)
    # 推理依赖：torch 走 pytorch 官方轮子源（镜像不代理，一字不改）
    assert any("torch" in cmd and "https://download.pytorch.org/whl/cu128" in cmd for cmd in calls)
    # 2026-09-25 实装修复：torchaudio 必须同源（MeloTTS/funasr 硬依赖，缺则 melo.api 导入即失败）
    assert any(
        cmd[4] == "torchaudio" and "--index-url" in cmd
        and "https://download.pytorch.org/whl/cu128" in cmd
        for cmd in calls
    )
    # ASR 引擎本体（funasr）必须随语音链路安装，否则识别链路在用户机器上缺席
    assert any(cmd[4] == "funasr==1.4.16" for cmd in calls)
    # llama-cpp-python 必须被跳过（长路径墙必然失败 → 改走预编译二进制路线，规划 §四-15）
    assert not any("llama-cpp-python" in cmd for cmd in calls)
    # MeloTTS 链：钉版包 + 清华索引
    assert any(
        "numpy==1.26.4" in cmd and "librosa==0.9.1" in cmd
        and "https://pypi.tuna.tsinghua.edu.cn/simple" in cmd
        for cmd in calls
    )
    # 随包源码：--no-deps 安装 bundled/melotts_src
    src = os.path.join(conda_runtime.BUNDLED_DIR, conda_runtime.MELOTTS_SRC_DIR_NAME)
    assert any("--no-deps" in cmd and src in cmd for cmd in calls)


def test_install_voice_dependencies_cpu_chain_and_failure_tolerated(tmp_path):
    """cpu 结论：不使用 cuda 专用源；单条失败仅记录 errors 不抛（不阻断）。"""
    root = str(tmp_path)
    _touch(conda_runtime.voice_env_python(root))
    calls = []

    def fake_runner(cmd):
        """首条失败、其余成功，验证失败容忍口径。"""
        calls.append(list(cmd))
        return (-1 if len(calls) == 1 else 0), "boom"

    result = conda_runtime.install_voice_dependencies(root, "cpu", runner=fake_runner)

    assert result["installed"] is False
    assert result["reason"] == "partial-failure"
    assert len(result["errors"]) == 1
    joined = " ".join(calls[0])
    assert "https://download.pytorch.org/whl/cpu" in joined


def test_install_voice_dependencies_env_missing_skips(tmp_path, capsys):
    """sidecar 环境缺失：告警跳过 env-missing，零命令执行。"""
    result = conda_runtime.install_voice_dependencies(str(tmp_path), "cpu", runner=lambda cmd: (0, ""))

    assert result["skipped"] is True
    assert result["reason"] == "env-missing"
    assert result["commands"] == []
    assert "sidecar 环境不存在" in capsys.readouterr().out


# ------------------------------------------------------------------ #
# provision_nltk_data：随包 nltk 数据预置                              #
# ------------------------------------------------------------------ #

def test_provision_nltk_data_copies_and_is_idempotent(tmp_path):
    """预置：从 bundled 源拷到 <root>/data/nltk_data；二次调用幂等跳过。"""
    root = str(tmp_path / "portable")
    bundled = tmp_path / "bundled"
    tagger_dir = bundled / conda_runtime.NLTK_DATA_DIR_NAME / "taggers" / "averaged_perceptron_tagger_eng"
    tagger_dir.mkdir(parents=True)
    (tagger_dir / "tag.pickle").write_bytes(b"payload")
    (bundled / conda_runtime.NLTK_DATA_DIR_NAME / "corpora" / "cmudict").mkdir(parents=True)

    first = conda_runtime.provision_nltk_data(root, bundled_dir=str(bundled))
    assert first["provisioned"] is True
    assert first["skipped"] is False
    copied = os.path.join(
        root, "data", "nltk_data", "taggers", "averaged_perceptron_tagger_eng", "tag.pickle"
    )
    assert os.path.isfile(copied)

    second = conda_runtime.provision_nltk_data(root, bundled_dir=str(bundled))
    assert second["provisioned"] is True
    assert second["skipped"] is True
    assert second["reason"] == "already-present"


def test_provision_nltk_data_source_missing_warns(tmp_path, capsys, monkeypatch):
    """源缺失：告警并返回 source-missing，不抛错（不阻断主安装）。

    注：解析优先级为「显式 bundled_dir → <root>/runtime/_bundled → installer/bundled」，
    开发机上 installer/bundled/nltk_data 真实存在，故须一并屏蔽第三候选才能构造"全缺失"。
    """
    monkeypatch.setattr(conda_runtime, "BUNDLED_DIR", str(tmp_path / "no-repo-bundled"))
    result = conda_runtime.provision_nltk_data(
        str(tmp_path), bundled_dir=str(tmp_path / "no-such-bundled")
    )

    assert result["provisioned"] is False
    assert result["reason"] == "source-missing"
    assert "随包 nltk 数据缺失" in capsys.readouterr().out


def test_provision_nltk_data_empty_target_treated_as_absent(tmp_path):
    """目标为空目录（占位残留）：视为未预置，正常复制补齐。"""
    root = str(tmp_path / "fresh")
    os.makedirs(os.path.join(root, "data", "nltk_data"), exist_ok=True)
    bundled = tmp_path / "b3"
    (bundled / conda_runtime.NLTK_DATA_DIR_NAME / "corpora" / "cmudict").mkdir(parents=True)

    result = conda_runtime.provision_nltk_data(root, bundled_dir=str(bundled))

    assert result["provisioned"] is True
    assert result["skipped"] is False
    assert os.path.isdir(os.path.join(root, "data", "nltk_data", "corpora", "cmudict"))


# ------------------------------------------------------------------ #
# find_bundled_source：随包源解析（分发落点优先）                       #
# ------------------------------------------------------------------ #

def test_find_bundled_source_prefers_distribution_location(tmp_path):
    """优先级：显式 bundled_dir > runtime/_bundled（随包分发）> installer/bundled。"""
    root = str(tmp_path / "portable")
    explicit = tmp_path / "explicit"
    (explicit / "melotts_src").mkdir(parents=True)

    assert conda_runtime.find_bundled_source(
        root, "melotts_src", bundled_dir=str(explicit)
    ) == str(explicit / "melotts_src")

    # 显式源缺失 → 回退分发落点 runtime/_bundled
    (tmp_path / "portable" / "runtime" / "_bundled" / "melotts_src").mkdir(parents=True)
    assert conda_runtime.find_bundled_source(
        root, "melotts_src", bundled_dir=str(tmp_path / "empty")
    ) == os.path.join(root, "runtime", "_bundled", "melotts_src")


def test_find_bundled_source_returns_none_when_absent(tmp_path, monkeypatch):
    """三处候选皆无 → 返回 None（调用方据此告警并跳过）。"""
    monkeypatch.setattr(conda_runtime, "BUNDLED_DIR", str(tmp_path / "no-bundled"))
    assert conda_runtime.find_bundled_source(str(tmp_path), "nope") is None


# ------------------------------------------------------------------ #
# warmup_voice_models：安装期 TTS 预热                                  #
# ------------------------------------------------------------------ #

def _make_voice_env(root):
    """伪造 sidecar 解释器存在（预热的前置条件）。"""
    _touch(os.path.join(root, "runtime", "voice", "python.exe"))


def test_warmup_voice_models_success_and_command_shape(tmp_path, capsys):
    """预热成功：命令形状（sidecar 解释器 -c 脚本 + 根 + 设备）且识别 TTS_WARMUP_OK。"""
    root = str(tmp_path / "portable")
    _make_voice_env(root)
    calls = []

    def fake_runner(cmd):
        calls.append(cmd)
        return 0, "load model...\nTTS_WARMUP_OK 132300\n"

    result = conda_runtime.warmup_voice_models(root, "cuda", runner=fake_runner)

    assert result["warmed"] is True
    assert result["skipped"] is False
    assert len(calls) == 1
    cmd = calls[0]
    assert cmd[0] == conda_runtime.voice_env_python(root)
    assert cmd[1] == "-c"
    assert cmd[2] == conda_runtime.VOICE_WARMUP_SCRIPT
    assert cmd[3] == root
    assert cmd[4] == "cuda"
    # 环境契约与 voice_bridge_client._build_env 对齐（便携原则：权重/语料/临时目录落便携根）
    assert "os.path.join(data, 'hf_cache')" in conda_runtime.VOICE_WARMUP_SCRIPT
    assert "NLTK_DATA" in conda_runtime.VOICE_WARMUP_SCRIPT
    assert "'nltk_data'" in conda_runtime.VOICE_WARMUP_SCRIPT
    assert "TEMP" in conda_runtime.VOICE_WARMUP_SCRIPT


def test_warmup_voice_models_failure_not_raise(tmp_path, capsys):
    """预热失败（超时/断网）：仅告警，warmed=False，不抛错。"""
    root = str(tmp_path / "portable")
    _make_voice_env(root)

    result = conda_runtime.warmup_voice_models(
        root, "cpu", runner=lambda cmd: (1, "connection reset")
    )

    assert result["warmed"] is False
    assert result["reason"] == "warmup-failed"
    assert "未完成" in capsys.readouterr().out


def test_warmup_voice_models_skips_when_cache_present(tmp_path):
    """幂等：HF 缓存中已有 MeloTTS 权重目录 → 跳过且不调用 runner。"""
    root = str(tmp_path / "portable")
    _make_voice_env(root)
    os.makedirs(
        os.path.join(root, "data", "hf_cache", "hub", conda_runtime.WARMUP_CACHE_MARKER),
        exist_ok=True,
    )

    def boom(cmd):  # pragma: no cover - 命中即说明幂等失效
        raise AssertionError("缓存已就位时不应执行预热命令")

    result = conda_runtime.warmup_voice_models(root, "cpu", runner=boom)

    assert result["warmed"] is True
    assert result["skipped"] is True
    assert result["reason"] == "already-warm"


def test_warmup_voice_models_skips_when_env_missing(tmp_path, capsys):
    """sidecar 环境不存在：跳过并告警（不阻断主安装）。"""
    result = conda_runtime.warmup_voice_models(str(tmp_path), "cpu", runner=None)

    assert result["warmed"] is False
    assert result["reason"] == "env-missing"
    assert "跳过 TTS 预热" in capsys.readouterr().out


# ------------------------------------------------------------------ #
# provision_runtime：安装期一次性装配编排                               #
# ------------------------------------------------------------------ #

def test_provision_runtime_order_log_and_report(tmp_path, monkeypatch):
    """装配编排：按「conda → 环境 → 依赖 → nltk → 预热」顺序执行；日志与报告落盘。"""
    root = str(tmp_path / "portable")
    order = []

    monkeypatch.setattr(
        conda_runtime, "_detect_gpu", lambda runner=None: ("cuda", "13.4", "nvidia")
    )
    monkeypatch.setattr(
        conda_runtime, "install_conda",
        lambda r, runner=None: order.append("conda") or {"installed": True},
    )
    monkeypatch.setattr(
        conda_runtime, "create_voice_env",
        lambda r, runner=None: order.append("env") or {"created": True},
    )
    monkeypatch.setattr(
        conda_runtime, "install_voice_dependencies",
        lambda r, recommend, cuda, channel=None, runner=None: order.append("deps")
        or {"installed": True, "errors": []},
    )
    monkeypatch.setattr(
        conda_runtime, "provision_nltk_data",
        lambda r: order.append("nltk") or {"provisioned": True},
    )
    monkeypatch.setattr(
        conda_runtime, "warmup_voice_models",
        lambda r, device, runner=None: order.append(f"warmup:{device}")
        or {"warmed": True},
    )
    # 通道解析走真实实现会读配置：直接 monkeypatch bootstrap 侧的解析入口
    from installer import bootstrap

    monkeypatch.setattr(bootstrap, "resolve_download_channel", lambda root=None: "mirror")

    result = conda_runtime.provision_runtime(root)

    assert order == ["conda", "env", "deps", "nltk", "warmup:cuda"]
    assert result["recommend"] == "cuda"
    # 日志与报告落盘
    log_path = os.path.join(root, "runtime", "provision.log")
    assert os.path.isfile(log_path)
    with open(log_path, encoding="utf-8") as fh:
        log_text = fh.read()
    assert "运行时装配开始" in log_text
    assert "GPU 检测：nvidia → 推荐 cuda" in log_text
    assert "运行时装配结束" in log_text

    with open(os.path.join(root, "data", "install_report.json"), encoding="utf-8") as fh:
        import json

        report = json.load(fh)
    assert report["recommend"] == "cuda"
    assert report["conda_installed"] is True
    assert report["tts_warmed"] is True
    assert report["channel"] == "mirror"


def test_provision_runtime_step_exception_never_raises(tmp_path, monkeypatch):
    """装配链内部异常：仅告警，仍写报告并返回（安装器不会被中断）。"""
    root = str(tmp_path / "portable")
    monkeypatch.setattr(
        conda_runtime, "_detect_gpu", lambda runner=None: ("cpu", None, "cpu")
    )

    def boom_install(r, runner=None):
        raise OSError("磁盘写入失败")

    monkeypatch.setattr(conda_runtime, "install_conda", boom_install)

    result = conda_runtime.provision_runtime(root)

    assert result["recommend"] == "cpu"
    assert os.path.isfile(os.path.join(root, "data", "install_report.json"))


def test_provision_runtime_skips_when_disk_insufficient(tmp_path, monkeypatch):
    """磁盘预检（规划 §四-4）：可用空间不足 → 整链跳过（不阻断），报告与日志留痕。"""
    root = str(tmp_path / "portable")
    monkeypatch.setattr(
        conda_runtime, "_detect_gpu", lambda runner=None: ("cuda", "13.4", "nvidia")
    )
    monkeypatch.setattr(conda_runtime, "_probe_disk_free_gb", lambda r: 3.0)
    called = []
    monkeypatch.setattr(
        conda_runtime, "install_conda",
        lambda r, runner=None: called.append("conda") or {"installed": True},
    )
    monkeypatch.setattr(
        conda_runtime, "create_voice_env",
        lambda r, runner=None: called.append("env") or {"created": True},
    )
    monkeypatch.setattr(
        conda_runtime, "install_voice_dependencies",
        lambda r, recommend, cuda, channel=None, runner=None: called.append("deps")
        or {"installed": True, "errors": []},
    )
    monkeypatch.setattr(
        conda_runtime, "provision_nltk_data",
        lambda r: called.append("nltk") or {"provisioned": True},
    )
    monkeypatch.setattr(
        conda_runtime, "warmup_voice_models",
        lambda r, device, runner=None: called.append("warmup") or {"warmed": True},
    )
    from installer import bootstrap

    monkeypatch.setattr(bootstrap, "resolve_download_channel", lambda root=None: "mirror")

    result = conda_runtime.provision_runtime(root)

    assert called == []  # 装配链全部跳过（不半装、不阻断）
    assert "磁盘可用空间不足" in (result["skipped_reason"] or "")
    import json

    with open(os.path.join(root, "data", "install_report.json"), encoding="utf-8") as fh:
        report = json.load(fh)
    assert "磁盘可用空间不足" in (report["skipped_reason"] or "")
    assert report["conda_installed"] is False
    assert report["tts_warmed"] is False
    with open(os.path.join(root, "runtime", "provision.log"), encoding="utf-8") as fh:
        assert "磁盘可用空间不足" in fh.read()


# ------------------------------------------------------------------ #
# backend_entry.provision_main：安装器调用入口                          #
# ------------------------------------------------------------------ #

def test_provision_main_passes_explicit_root(tmp_path, monkeypatch, capsys):
    """--root 显式传入时按该路径装配，退出码恒 0（失败不阻断安装）。"""
    from installer import backend_entry

    root = tmp_path / "installed"
    seen = {}

    def fake_provision(target, **kwargs):
        seen["root"] = target
        return {}

    monkeypatch.setattr(conda_runtime, "provision_runtime", fake_provision)

    code = backend_entry.provision_main(["--provision-runtime", "--root", str(root)])

    assert code == 0
    assert seen["root"] == str(root)

def test_provision_runtime_log_is_live_during_assembly(tmp_path, monkeypatch):
    """长装可诊断性：装配尚未结束时，provision.log 已能读到前面步骤的输出。

    背景（2026-09-25 实装发现）：日志原先只在进程退出时随文件关闭落盘，
    数十分钟的装配期间 tail 该文件是空的，无法判断卡在哪一步；现 _TeeStream
    每次写入即 flush。本用例在「环境创建」步骤内回读日志，断言可见性。
    """
    root = str(tmp_path / "portable")
    log_path = os.path.join(root, "runtime", "provision.log")
    seen = []

    monkeypatch.setattr(
        conda_runtime, "_detect_gpu", lambda runner=None: ("cpu", None, "cpu")
    )
    monkeypatch.setattr(
        conda_runtime, "install_conda", lambda r, runner=None: {"installed": True}
    )

    def peek_log(r, runner=None):
        with open(log_path, encoding="utf-8") as fh:
            seen.append(fh.read())
        return {"created": True}

    monkeypatch.setattr(conda_runtime, "create_voice_env", peek_log)
    monkeypatch.setattr(
        conda_runtime, "install_voice_dependencies",
        lambda r, recommend, cuda, channel=None, runner=None: {"installed": True, "errors": []},
    )
    monkeypatch.setattr(
        conda_runtime, "provision_nltk_data", lambda r: {"provisioned": True}
    )
    monkeypatch.setattr(
        conda_runtime, "warmup_voice_models", lambda r, device, runner=None: {"warmed": True}
    )

    conda_runtime.provision_runtime(root)

    assert seen, "装配中途应能读到日志"
    assert "运行时装配开始" in seen[0]
    assert "GPU 检测：cpu → 推荐 cpu" in seen[0]
    # 此刻装配未结束：结束标记不应出现（证明读到的确是「进行中」的快照）
    assert "运行时装配结束" not in seen[0]


def test_provision_nltk_data_uses_distribution_location(tmp_path):
    r"""用户机器场景（2026-09-25 实装缺陷回归）：源在 <root>/runtime/_bundled 下也必须被找到。

    实装日志曾出现「随包 nltk 数据缺失（...\runtime\backend\_internal\installer\bundled\
    nltk_data）」——冻结后端内不存在 installer/bundled，故必须优先查分发落点。
    """
    root = str(tmp_path / "installed")
    bundled_src = tmp_path / "installed" / "runtime" / "_bundled" / "nltk_data"
    (bundled_src / "taggers" / "averaged_perceptron_tagger_eng").mkdir(parents=True)
    (bundled_src / "taggers" / "averaged_perceptron_tagger_eng" / "tag.pickle").write_bytes(b"p")

    result = conda_runtime.provision_nltk_data(root)  # 不传 bundled_dir（用户机器口径）

    assert result["provisioned"] is True
    assert result["skipped"] is False
    assert os.path.isfile(
        os.path.join(
            root, "data", "nltk_data", "taggers", "averaged_perceptron_tagger_eng", "tag.pickle"
        )
    )
