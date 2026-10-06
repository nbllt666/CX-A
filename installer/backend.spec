# -*- mode: python ; coding: utf-8 -*-
"""CX-A 后端 PyInstaller 构建配置（正式可提交版本，路径无关）。

路径推导：PyInstaller 执行 spec 时注入内置全局变量 ``SPECPATH``（spec 所在
目录的绝对路径字符串）。本文件位于 <项目根>/installer/，故：
    SPEC_DIR    = installer/
    PROJECT_ROOT = installer/ 的上级
任何机器 / 任意 clone 路径下执行 ``pyinstaller installer/backend.spec``
均可复现构建，不依赖硬编码盘符。

对应关系（原命令行参数 -> spec）：
    --onedir --name backend   -> EXE(exclude_binaries=True) + COLLECT(name='backend')
    --paths <项目根>          -> Analysis(pathex=[PROJECT_ROOT])
    --exclude-module X ...    -> Analysis(excludes=[...])
可选重依赖（funasr/melo/llama_cpp/torch）均为延迟导入、缺失自动降级，排除以减小
产物体积。

20261005 人类裁决「LanceDB 默认 + 禁止降级」：lancedb / numpy **解除排除**——
冻结包必须真实可用 LanceVectorStore（lancedb 依赖链 pyarrow/numpy 一并自动跟进
Analysis），否则启动硬失败（_build_vector_store 禁止降级语义）。体积 +150~250MB
为既定代价。
"""

import os

#: spec 所在目录（PyInstaller 注入的内置变量）：installer/
SPEC_DIR = SPECPATH
#: 项目根目录：installer/ 的上级
PROJECT_ROOT = os.path.dirname(SPEC_DIR)


a = Analysis(
    [os.path.join(SPEC_DIR, 'backend_entry.py')],
    pathex=[PROJECT_ROOT],
    binaries=[],
    datas=[],
    hiddenimports=['lancedb', 'pyarrow', 'numpy'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['funasr', 'melo', 'llama_cpp', 'torch'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='backend',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='backend',
)
