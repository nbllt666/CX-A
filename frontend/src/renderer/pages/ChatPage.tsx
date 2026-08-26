import React, { useRef, useState } from 'react';
import { MOCK_CHAT_MESSAGES } from '../mock';
import type { ChatMessage } from '../mock';
import { API_ENDPOINTS, IS_BACKEND_READY } from '../api';

/**
 * 聊天页（/chat）：消息气泡 + 输入框 + 语音按钮占位。
 * 后端未就绪时以 mock 驱动；发送后走本地伪交互，后续接 API_ENDPOINTS.chat.
 */
export default function ChatPage() {
  const [messages, setMessages] = useState<ChatMessage[]>(MOCK_CHAT_MESSAGES);
  const [draft, setDraft] = useState('');
  const listRef = useRef<HTMLDivElement>(null);

  function send() {
    const text = draft.trim();
    if (!text) return;
    const now = new Date();
    const me: ChatMessage = {
      id: `me-${now.getTime()}`,
      role: 'me',
      content: text,
      time: now.toTimeString().slice(0, 5),
    };
    setMessages((prev) => [...prev, me]);
    setDraft('');
    // mock 伴侣回应占位
    setTimeout(() => {
      setMessages((prev) => [
        ...prev,
        {
          id: `c-${Date.now()}`,
          role: 'companion',
          content: '嗯嗯，我在听呢～（这里后续会接入真实回复）',
          time: new Date().toTimeString().slice(0, 5),
        },
      ]);
    }, 400);
  }

  return (
    <div className="flex h-full flex-col p-5">
      <div className="mb-3 flex items-center justify-between">
        <div>
          <h1 className="text-xl font-bold text-gradient">聊天</h1>
          <p className="text-sm text-[var(--text-secondary)]">
            想聊什么都可以，我会好好记着的
          </p>
        </div>
        {!IS_BACKEND_READY && (
          <span className="rounded-full bg-[rgba(124,216,255,0.16)] px-3 py-1 text-xs text-[var(--text-tertiary)]">
            演示数据模式 · 后端就绪后接入 API
          </span>
        )}
      </div>

      {/* 消息列表 */}
      <div ref={listRef} className="glass-panel flex-1 overflow-y-auto p-4">
        <div className="flex flex-col gap-3">
          {messages.map((m) => (
            <MessageBubble key={m.id} msg={m} />
          ))}
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
          onKeyDown={(e) => e.key === 'Enter' && send()}
          placeholder="跟你的伴侣说点什么吧…"
          className="h-10 flex-1 rounded-xl border border-[var(--glass-border)] bg-[rgba(255,255,255,0.5)] px-3 text-sm outline-none transition focus:ring-2 focus:ring-[var(--color-accent)]"
        />
        <button
          type="button"
          onClick={send}
          className="h-10 shrink-0 rounded-xl bg-gradient-to-r from-[var(--color-secondary)] to-[var(--color-primary)] px-5 text-sm font-medium text-white transition-all duration-200 hover:opacity-90 active:scale-95"
        >
          发送
        </button>
      </div>
    </div>
  );
}

function MessageBubble({ msg }: { msg: ChatMessage }) {
  const isMe = msg.role === 'me';
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
          ].join(' ')}
        >
          {msg.content}
        </div>
        <span className="mt-1 px-1 text-[11px] text-[var(--text-tertiary)]">{msg.time}</span>
      </div>
    </div>
  );
}