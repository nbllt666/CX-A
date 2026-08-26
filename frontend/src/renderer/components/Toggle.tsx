import React from 'react';

interface ToggleProps {
  checked: boolean;
  onChange: (next: boolean) => void;
  label?: string;
  /** 纯展示用（骨架占位，开关仅本地状态） */
  disabled?: boolean;
}

/**
 * 二次元风格开关。用于设置页的本地模式 / 授权类布尔项。
 */
export default function Toggle({ checked, onChange, label, disabled = false }: ToggleProps) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      disabled={disabled}
      onClick={() => onChange(!checked)}
      className={[
        'relative h-6 w-11 shrink-0 rounded-full transition-colors duration-250 focus:outline-none focus:ring-2 focus:ring-[var(--color-accent)] focus:ring-offset-1',
        checked
          ? 'bg-gradient-to-r from-[var(--color-secondary)] to-[var(--color-primary)]'
          : 'bg-[var(--gray-200)]',
        disabled ? 'cursor-not-allowed opacity-50' : 'cursor-pointer',
      ].join(' ')}
    >
      <span
        className={[
          'absolute top-0.5 left-0.5 flex h-5 w-5 items-center justify-center rounded-full bg-white shadow transition-transform duration-250',
          checked ? 'translate-x-5' : '',
        ].join(' ')}
      >
        {checked && (
          <span className="inline-block h-2 w-2 rounded-full bg-gradient-to-br from-[var(--color-primary)] to-[var(--color-secondary)]" />
        )}
      </span>
    </button>
  );
}