import React, { useRef, useState } from 'react';
import type { ChatMessage } from '../mock';
import { sendMessage } from '../api';

/**
 * 聊天页（/chat）：消息气泡 + 输入框 + 语音按钮占位。
 *
 * 真实链路原则（不再伪造对话）：
 * - messages 初始为空数组，提供空态引导；
 * - 发送经 api.sendMessage 走真实 POST /api/chat/messages；
 * - 后端 /api/chat/* 当前为「未启用守卫」占位端点：
 *   - 响应中含明确的回复字段 → 展示为伴侣气泡（这是后端真实回传，非本地伪造）；
 *   - 请求失败 / 占位响应 → 该条用户消息标「未送达」，提示条常显「聊天通道尚未接入」。
 */
export default function ChatPage() {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [draft, setDraft] = useState('');
  const [sending, setSending] = useState(false);
  /** 聊天通道实际连通状态：unknown=尚未探测；connected=收到过真实回复；unavailable=最近一次发送不可达 */
  const [channel, setChannel] = useState<'unknown' | 'connected' | 'unavailable'>('unknown');
  const listRef = useRef<HTMLDivElement>(null);

  async function send() {
    const text = draft.trim();
    if (!text || sending) return;
    setDraft('');
    setSending(true);

    const now = new Date();
    const meId = `me-${now.getTime()}`;
    setMessages((prev) => [
      ...prev,
      { id: meId, role: 'me', content: text, time: formatTime(now), status: 'sent' },
    ]);

    try {
      const data = await sendMessage({ content: text });
      const reply = extractReplyText(data);
      if (reply) {
        setChannel('connected');
        setMessages((prev) => [
          ...prev,
          {
            id: `c-${Date.now()}`,
            role: 'companion',
            content: reply,
            time: formatTime(new Date()),
          },
        ]);
      } else {
        // 守卫端点占位响应（无可用回复字段）：不做假回复
        setChannel('unavailable');
        markFailed(meId);
      }
    } catch (err) {
      // 后端不可达：标注该条消息未送达，不伪造任何回复文本
      console.error('[Chat] 消息发送失败:', err);
      setChannel('unavailable');
      markFailed(meId);
    } finally {
      setSending(false);
    }
  }

  function markFailed(id: string) {
    setMessages((prev) =>
      prev.map((m) => (m.id === id ? { ...m, status: 'failed' as const } : m)),
    );
  }

  return (
    <div className="flex h-full flex-col p-5">
      <div className="mb-3">
        <h1 className="text-xl font-bold text-gradient">聊天</h1>
        <p className="text-sm text-[var(--text-secondary)]">
          想聊什么都可以，我会好好记着的
        </p>
      </div>

      {/* 通道状态提示条：style 对齐 MemoriesPage 离线横幅；直到确认真连通才隐藏 */}
      {channel !== 'connected' && (
        <div className="mb-3 rounded-xl border border-[var(--glass-border)] bg-[rgba(124,216,255,0.08)] px-3 py-2 text-xs text-[var(--text-secondary)]">
          聊天通道尚未接入，消息暂时送不到 TA
          那里哦～后端接入后会在这里真实回应（绝不假装回答）
        </div>
      )}

      {/* 消息列表 */}
      <div ref={listRef} className="glass-panel flex-1 overflow-y-auto p-4">
        <div className="flex flex-col gap-3">
          {messages.map((m) => (
            <MessageBubble key={m.id} msg={m} />
          ))}
          {messages.length === 0 && (
            <p className="py-16 text-center text-sm text-[var(--text-tertiary)]">
              还没有聊天记录，跟 TA 说句你好吧～
            </p>
          )}
        </div>
      </div>

      {/* 输入区 */}
      <div className="glass-panel-strong mt-3 flex items-center gap-2 p-2">
        <button
          type="button"
          aria-label="语音输入（占位）"
          className="flex h-10 w-10 shrink-0 items-center justify-center rounded-full text-lg transition-all duration-200 hover:scale-105 hover:bg-[rgba(255,255,255,0.12)] active:scale-95"
          title="语音输入（后续接入）"
        >
          🎙️
        </button>
        <input
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && void send()}
          placeholder="跟你的伴侣说点什么吧…"
          className="h-10 flex-1 rounded-xl border border-[var(--glass-border)] bg-[rgba(255,255,255,0.5)] px-3 text-sm outline-none transition focus:ring-2 focus:ring-[var(--color-accent)]"
        />
        <button
          type="button"
          onClick={() => void send()}
          disabled={sending}
          className="h-10 shrink-0 rounded-xl bg-gradient-to-r from-[var(--color-secondary)] to-[var(--color-primary)] px-5 text-sm font-medium text-white transition-all duration-200 hover:opacity-90 active:scale-95 disabled:cursor-not-allowed disabled:opacity-60"
        >
          {sending ? '发送中…' : '发送'}
        </button>
      </div>
    </div>
  );
}

/** 从后端响应中提取明确的回复文本；形状不符 / 无内容一律返回 null（绝不伪造）。 */
function extractReplyText(data: unknown): string | null {
  if (!data || typeof data !== 'object') return null;
  const rec = data as Record<string, unknown>;
  for (const key of ['reply', 'reply_text', 'text', 'content', 'message']) {
    const val = rec[key];
    if (typeof val === 'string' && val.trim()) return val;
  }
  return null;
}

function formatTime(d: Date): string {
  return d.toTimeString().slice(0, 5);
}

function MessageBubble({ msg }: { msg: ChatMessage }) {
  const isMe = msg.role === 'me';
  const failed = msg.status === 'failed';
  return (
    <div className={`animate-fade-up flex gap-2.5 ${isMe ? 'flex-row-reverse' : ''}`}>
      <div
        className={`flex h-8 w-8 shrink-0 items-center justify-center rounded-full text-base shadow ${
          isMe
            ? 'bg-gradient-to-br from-[var(--color-primary)] to-[var(--color-secondary)]'
            : 'bg-gradient-to-br from-[var(--color-accent)] to-[var(--color-secondary)]'
        }`}
      >
        {isMe ? '😊' : '🐱'}
      </div>
      <div className={`max-w-[70%] flex flex-col ${isMe ? 'items-end' : 'items-start'}`}>
        <div
          className={[
            'rounded-2xl px-3.5 py-2 text-sm leading-relaxed whitespace-pre-wrap',
            isMe
              ? 'bg-gradient-to-r from-[var(--color-secondary)] to-[var(--color-primary)] text-white'
              : 'bg-[var(--glass-bg-strong)] text-[var(--text-primary)] shadow-sm',
            failed && 'opacity-70',
          ].join(' ')}
        >
          {msg.content}
        </div>
        <span className="mt-1 flex gap-1.5 px-1 text-[11px] text-[var(--text-tertiary)]">
          {failed && (
            <span className="font-medium text-[var(--color-error)]">未送达 · 聊天通道尚未接入</span>
          )}
          <span>{msg.time}</span>
        </span>
      </div>
    </div>
  );
}
