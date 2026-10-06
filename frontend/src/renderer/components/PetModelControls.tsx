import React, { useState } from 'react';
import { RotateCcw, Upload } from 'lucide-react';
import {
  hasVrmFilePicker,
  importPetModel,
  pickVrmFile,
  publishPetModelReload,
  resetPetModel,
} from '../api';

/**
 * PetModelControls — 「更换桌宠模型 / 恢复默认」按钮组（PetPage 与设置页共用）。
 *
 * 链路：pickVrmFile（Electron 桥选 .vrm 文件，浏览器 dev 无此能力时显式提示）→
 * importPetModel（后端备份原模型后覆盖）→ 成功后 publishPetModelReload
 * （跨窗口通知悬浮窗重载）+ onReloaded 回调（本窗口渲染方立即重载页内桌宠）；
 * 失败把后端中文 message 原样展示，不静默。进行中禁用两个按钮防重复提交。
 */
export default function PetModelControls({ onReloaded }: { onReloaded?: () => void }) {
  /** 进行中操作：null 空闲 / import 导入中 / reset 恢复中（防连点） */
  const [busy, setBusy] = useState<'import' | 'reset' | null>(null);
  /** 成功 / 引导类轻量提示 */
  const [hint, setHint] = useState<string | null>(null);
  /** 失败提示（后端中文 message 或通用文案） */
  const [error, setError] = useState<string | null>(null);

  /** 更换模型：选文件 → 导入 → 成功提示 + 触发重载；取消选择不打扰 */
  const handleImport = async () => {
    if (busy) return;
    setError(null);
    setHint(null);
    const hasPicker = hasVrmFilePicker();
    let file: string | null = null;
    try {
      file = await pickVrmFile();
    } catch {
      file = null;
    }
    if (!file) {
      // 桥缺失 = 非 Electron 环境；桥在但返回空 = 用户主动取消，不打扰
      if (!hasPicker) setHint('更换桌宠模型要在桌面应用里才能用哦');
      return;
    }
    setBusy('import');
    try {
      await importPetModel(file);
      publishPetModelReload(); // 跨窗口总线：悬浮窗重载新模型
      onReloaded?.(); // 本窗口（页内桌宠）立即重载
      setHint('模型已更换，桌宠马上换上新模样');
    } catch (err) {
      setError(
        err instanceof Error && err.message
          ? err.message
          : '更换桌宠模型失败…待会儿再试一次就好啦',
      );
    } finally {
      setBusy(null);
    }
  };

  /** 恢复默认模型：POST reset → 成功提示 + 触发重载；无备份时展示后端中文 message */
  const handleReset = async () => {
    if (busy) return;
    setError(null);
    setHint(null);
    setBusy('reset');
    try {
      await resetPetModel();
      publishPetModelReload();
      onReloaded?.();
      setHint('已恢复默认的桌宠模型');
    } catch (err) {
      setError(
        err instanceof Error && err.message ? err.message : '恢复默认模型失败…待会儿再试一次就好啦',
      );
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="flex flex-col gap-1.5">
      <div className="flex flex-wrap gap-2">
        <button
          type="button"
          onClick={() => {
            void handleImport();
          }}
          disabled={busy !== null}
          className="inline-flex items-center gap-1.5 rounded-full border border-[var(--glass-border)] px-4 py-1.5 text-sm text-[var(--text-secondary)] transition hover:text-[var(--text-primary)] disabled:cursor-not-allowed disabled:opacity-50"
        >
          <Upload className="h-3.5 w-3.5" aria-hidden="true" />
          {busy === 'import' ? '导入中…' : '更换桌宠模型'}
        </button>
        <button
          type="button"
          onClick={() => {
            void handleReset();
          }}
          disabled={busy !== null}
          className="inline-flex items-center gap-1.5 rounded-full border border-[var(--glass-border)] px-4 py-1.5 text-sm text-[var(--text-secondary)] transition hover:text-[var(--text-primary)] disabled:cursor-not-allowed disabled:opacity-50"
        >
          <RotateCcw className="h-3.5 w-3.5" aria-hidden="true" />
          {busy === 'reset' ? '恢复中…' : '恢复默认'}
        </button>
      </div>
      {hint && <p className="text-xs text-[var(--text-secondary)]">{hint}</p>}
      {error && <p className="text-xs font-medium text-[var(--color-error)]">{error}</p>}
    </div>
  );
}
