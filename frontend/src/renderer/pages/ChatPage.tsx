import React, { useEffect, useRef, useState } from 'react';
import type { ChatMessage } from '../mock';
import { sendChatMessage } from '../api';
import PetAvatar from '../components/PetAvatar';
import type { PetMood } from '../components/PetAvatar';

/**
 * 聊天页（/chat）：消息气泡 + 输入框 + 桌宠表情联动（Task H3）。
 *
 * 真实链路原则（不再伪造对话）：
 * - messages 初始为空数组，提供空态引导；
 * - 发送经 api.sendChatMessage 走真实 POST /api/chat/message（表情聊天端点）；
 *   后端组装云端流式回复并解析 [emotion:x] 标签，返回 {clean_text, mood, raw}；
 *   无 api_key / 云端不可达时后端返回固定友好文案 + mood=calm（offline:true），
 *   仍作为伴侣气泡真实展示（后端真实回传，非本地伪造）；
 * - 表情联动：PetAvatar 的 mood 由最近一次伴侣回复驱动，未知档位（含后端
 *   angry——前端无该档）回落 calm；talking 自发送起保持到回复渲染完成
 *   （真实 TTS 音频接线点见 send 内注释）；
 * - 请求失败 / 占位响应 → 该条用户消息标「未送达」，提示条常显。
 */
export default function ChatPage() {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [draft, setDraft] = useState('');
  const [sending, setSending] = useState(false);
  /** 聊天通道实际连通状态：unknown=尚未探测；connected=收到过真实回复；unavailable=最近一次发送不可达 */
  const [channel, setChannel] = useState<'unknown' | 'connected' | 'unavailable'>('unknown');
  /** 桌宠表情档位：由最近一次伴侣回复的 mood 驱动（Task H3），默认平静 */
  const [mood, setMood] = useState<PetMood>('calm');
  /** 口型说话态：发送后置 true，回复渲染完成（或失败降级）复位（Task H3 务实实现） */
  const [talking, setTalking] = useState(false);
  const listRef = useRef<HTMLDivElement>(null);

  // F-7（第三轮体检批次6）：消息变化后自动滚动到底部——修复前 listRef 为
  // 死引用，消息超一屏后新气泡（尤其伴侣回复）出现在视口外。
  // scrollTo 存在性防御：jsdom 测试环境未实现 Element.scrollTo
  useEffect(() => {
    const el = listRef.current;
    if (el && typeof el.scrollTo === 'function') {
      el.scrollTo({ top: el.scrollHeight });
    }
  }, [messages.length]);

  async function send() {
    const text = draft.trim();
    if (!text || sending) return;
    setDraft('');
    setSending(true);

    // 口型联动（H3 务实实现）：发送即进入说话态，回复渲染完成（或失败降级）
    // 在 finally 中复位。真实 TTS 音频接线点：未来后端返回音频（如响应携带
    // audio_base64 或走独立 /api/voice/synthesize）时，把复位时机改为
    // onTtsEnd 回调——即 <audio> 元素的 ended 事件触发 setTalking(false)，
    // 播放期间保持 talking=true 驱动口型动画（播放开始点在音频可播放时）。
    setTalking(true);

    const now = new Date();
    const meId = `me-${now.getTime()}`;
    setMessages((prev) => [
      ...prev,
      { id: meId, role: 'me', content: text, time: formatTime(now), status: 'sent' },
    ]);

    try {
      const data = await sendChatMessage({ message: text });
      const reply = extractReplyText(data);
      if (reply) {
        setChannel('connected');
        // 表情联动：mood 由本次回复驱动（未知档位回落 calm，见 extractMood）
        setMood(extractMood(data));
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
      // 回复渲染完成（含离线文案展示）即复位口型；真实音频场景见上方接线点注释
      setTalking(false);
    }
  }

  function markFailed(id: string) {
    setMessages((prev) =>
      prev.map((m) => (m.id === id ? { ...m, status: 'failed' as const } : m)),
    );
  }

  return (
    <div className="flex h-full flex-col p-5">
      {/* 标题区 + 桌宠表情联动（Task H3）：mood 由最近一次伴侣回复驱动，
          talking 为口型占位动画（真实 TTS 音频接线点见 send 内注释） */}
      <div className="mb-3 flex items-start justify-between gap-3">
        <div>
          <h1 className="text-xl font-bold text-gradient">聊天</h1>
          <p className="text-sm text-[var(--text-secondary)]">
            想聊什么都可以，我会好好记着的
          </p>
        </div>
        <div className="shrink-0" aria-label="桌宠表情">
          <PetAvatar mood={mood} talking={talking} size={96} />
        </div>
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
          onKeyDown={(e) => {
            // F-6（第三轮体检批次6）：中文输入法组合期（选候选词）的 Enter 不触发发送
            if (e.key === 'Enter' && !e.nativeEvent.isComposing) void send();
          }}
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

/**
 * 从后端响应中提取明确的回复文本；形状不符 / 无内容一律返回 null（绝不伪造）。
 *
 * H3 表情端点：clean_text 为后端解析后的干净回复（已识别情绪标签已剥离），
 * 优先采用；兼容旧端点 reply / reply_text 字段。
 *
 * 守卫端点会返回 200 + `{"ok":false,"error":"chat_service_disabled","message":"聊天服务未启用…"}`
 * 这类失败说明：ok === false 或存在非空 error 字段即判定为不可用返回 null，
 * 走「未送达」路径，绝不把故障说明文本（message/content 等）渲染成伴侣气泡。
 */
function extractReplyText(data: unknown): string | null {
  if (!data || typeof data !== 'object') return null;
  const rec = data as Record<string, unknown>;
  // 失败响应守卫：ok === false 或显式 error 字符串 → 一律视为不可用
  if (rec.ok === false) return null;
  if (typeof rec.error === 'string' && rec.error.trim()) return null;
  // H3 表情端点：clean_text 优先（为空时回落旧字段，防止空气泡）
  if (typeof rec.clean_text === 'string' && rec.clean_text.trim()) return rec.clean_text;
  // 候选 key 收窄为回复专用字段，防止 message/content 等通用字段被误当回复
  for (const key of ['reply', 'reply_text']) {
    const val = rec[key];
    if (typeof val === 'string' && val.trim()) return val;
  }
  return null;
}

/** 前端表情档位白名单（PetAvatar 6 档；后端情绪集另含 angry，回落 calm） */
const ALLOWED_MOODS: readonly string[] = ['happy', 'calm', 'sad', 'surprised', 'shy', 'sleepy'];

/**
 * 从后端响应中提取表情档位；非白名单值（含后端 angry——前端无该档）
 * 一律回落 calm（对齐 spec「未知情绪降级」语义）。
 */
function extractMood(data: unknown): PetMood {
  const rec = data as Record<string, unknown> | null;
  const mood = typeof rec?.mood === 'string' ? rec.mood : '';
  return ALLOWED_MOODS.includes(mood) ? (mood as PetMood) : 'calm';
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
            'selectable rounded-2xl px-3.5 py-2 text-sm leading-relaxed whitespace-pre-wrap',
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
