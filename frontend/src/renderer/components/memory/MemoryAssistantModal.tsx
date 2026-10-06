import React, { useState } from 'react';
import { Bot, Send, User, X } from 'lucide-react';
import { sendChatMessage } from '../../api';

/**
 * 记忆管理助手对话面板（Task 7）：走既有聊天链路 POST /api/chat/message，
 * 固定 agent_id='memory-agent'（后端 persona + [memory:op] 指令标签工具环）。
 * 气泡为 ChatPage MessageBubble 简化版（无朗读 / 无表情）；失败不伪造回复，
 * 用户气泡标记「未送达」并显示静态提示行。
 */

interface AssistantMsg {
  role: 'me' | 'bot';
  content: string;
  /** 发送失败标记（对齐 ChatPage「未送达」口径） */
  failed?: boolean;
}

export default function MemoryAssistantModal({
  onClose,
}: {
  onClose: () => void;
}) {
  const [messages, setMessages] = useState<AssistantMsg[]>([]);
  const [input, setInput] = useState('');
  const [sending, setSending] = useState(false);

  const lastFailed = messages.length > 0 && messages[messages.length - 1].failed === true;

  async function send() {
    const text = input.trim();
    if (!text || sending) return;
    setMessages((m) => [...m, { role: 'me', content: text }]);
    setInput('');
    setSending(true);
    try {
      const res = await sendChatMessage({ message: text, agent_id: 'memory-agent' });
      if (res.ok && typeof res.clean_text === 'string' && res.clean_text.trim()) {
        setMessages((m) => [...m, { role: 'bot', content: res.clean_text }]);
      } else {
        // ok:false / 空回复：标记未送达，不伪造 bot 气泡
        setMessages((m) =>
          m.map((x, i) => (i === m.length - 1 ? { ...x, failed: true } : x)),
        );
      }
    } catch {
      setMessages((m) =>
        m.map((x, i) => (i === m.length - 1 ? { ...x, failed: true } : x)),
      );
    } finally {
      setSending(false);
    }
  }

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-end bg-black/40 p-4 sm:justify-center"
      role="dialog"
      aria-modal="true"
      aria-label="记忆管理助手"
    >
      <div className="glass-panel flex h-[70vh] max-h-[70vh] w-full max-w-md flex-col rounded-2xl p-4">
        <div className="mb-3 flex items-center justify-between">
          <div className="flex items-center gap-2">
            <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-[rgba(124,216,255,0.16)] text-[var(--color-accent)]">
              <Bot className="h-4 w-4" />
            </div>
            <div>
              <h2 className="text-sm font-bold text-[var(--text-primary)]">记忆管理助手</h2>
              <p className="text-xs text-[var(--text-tertiary)]">用一句话帮您查找、新增或整理记忆</p>
            </div>
          </div>
          <button
            type="button"
            aria-label="关闭助手"
            onClick={onClose}
            className="flex h-7 w-7 items-center justify-center rounded-full text-[var(--text-tertiary)] transition hover:bg-[var(--glass-bg-strong)] hover:text-[var(--text-primary)]"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        <div className="mb-3 flex-1 space-y-3 overflow-y-auto pr-1">
          {messages.length === 0 && (
            <p className="py-8 text-center text-sm text-[var(--text-tertiary)]">
              试试问我：最近我们一起做了什么？
            </p>
          )}
          {messages.map((msg, i) => {
            const isMe = msg.role === 'me';
            return (
              <div key={i} className={`flex gap-2.5 ${isMe ? 'flex-row-reverse' : ''}`}>
                <div
                  className={`flex h-8 w-8 shrink-0 items-center justify-center rounded-lg ${
                    isMe
                      ? 'bg-[rgba(255,145,210,0.18)] text-[var(--color-primary)]'
                      : 'bg-[rgba(124,216,255,0.16)] text-[var(--color-accent)]'
                  }`}
                  aria-hidden="true"
                >
                  {isMe ? <User className="h-4 w-4" /> : <Bot className="h-4 w-4" />}
                </div>
                <div className={`flex max-w-[70%] flex-col ${isMe ? 'items-end' : 'items-start'}`}>
                  <div
                    className={[
                      'selectable whitespace-pre-wrap rounded-2xl px-3.5 py-2 text-sm leading-relaxed',
                      isMe
                        ? 'bg-gradient-to-r from-[var(--color-secondary)] to-[var(--color-primary)] text-white'
                        : 'bg-[var(--glass-bg-strong)] text-[var(--text-primary)] shadow-sm',
                      msg.failed && 'opacity-70',
                    ].join(' ')}
                  >
                    {msg.content}
                  </div>
                  {msg.failed && (
                    <span className="mt-1 px-1 text-[11px] font-medium text-[var(--color-error)]">
                      未送达
                    </span>
                  )}
                </div>
              </div>
            );
          })}
        </div>

        {lastFailed && (
          <p role="alert" className="mb-2 text-xs text-[var(--color-error)]">
            助手暂时无法回应，请稍后再试
          </p>
        )}

        <div className="flex gap-2">
          <input
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && !e.nativeEvent.isComposing) send();
            }}
            placeholder="对助手说点什么…"
            aria-label="助手输入框"
            className="h-10 flex-1 rounded-xl border border-[var(--glass-border)] bg-[var(--glass-bg-strong)] px-3 text-sm text-[var(--text-primary)] outline-none transition focus:ring-2 focus:ring-[var(--color-accent)]"
          />
          <button
            type="button"
            aria-label="发送"
            onClick={send}
            disabled={sending || input.trim() === ''}
            className="flex h-10 w-10 items-center justify-center rounded-xl bg-gradient-to-r from-[var(--color-secondary)] to-[var(--color-primary)] text-white transition disabled:cursor-not-allowed disabled:opacity-50"
          >
            <Send className="h-4 w-4" />
          </button>
        </div>
      </div>
    </div>
  );
}
