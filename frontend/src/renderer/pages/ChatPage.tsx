import React, { useEffect, useRef, useState } from 'react';
import { Bot, Mic, User, Volume2 } from 'lucide-react';
import type { ChatMessage } from '../mock';
import { sendChatMessage, synthesizeSpeech, transcribeAudio } from '../api';
import { startRecording, type RecordingSession } from '../audioRecorder';
import Toggle from '../components/Toggle';

/**
 * 聊天页（/chat）：消息气泡 + 输入框 + 真实回复链路（Task H3）。
 *
 * 真实链路原则（不再伪造对话）：
 * - messages 初始为空数组，提供空态引导；
 * - 发送经 api.sendChatMessage 走真实 POST /api/chat/message（表情聊天端点）；
 *   后端组装云端流式回复并解析 [emotion:x] 标签，返回 {clean_text, mood, raw}；
 *   无 api_key / 云端不可达时后端返回固定友好文案 + mood=calm（offline:true），
 *   仍作为伴侣气泡真实展示（后端真实回传，非本地伪造）；
 * - 请求失败 / 占位响应 → 该条用户消息标「未送达」，提示条常显。
 *
 * 视觉口径（对齐设计参考项目）：
 * - 气泡头像是 **32px 圆角方形 + 淡色底 + lucide 图标**（用户 User / 伴侣 Bot），
 *   不使用 emoji 圆圈；页头不再挂卡通形象（桌宠由悬浮窗/桌宠页的真实 VRM 承载）。
 * - 文案不出现技术栈/实现细节（「后端」「通道」等字样），只说用户能感知的事实。
 */
export default function ChatPage() {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [draft, setDraft] = useState('');
  const [sending, setSending] = useState(false);
  /** 聊天通道实际连通状态：unknown=尚未探测；connected=收到过真实回复；unavailable=最近一次发送不可达 */
  const [channel, setChannel] = useState<'unknown' | 'connected' | 'unavailable'>('unknown');
  const listRef = useRef<HTMLDivElement>(null);
  /** 语音输入：录音中标记 + 当前录音会话句柄 */
  const [recording, setRecording] = useState(false);
  const sessionRef = useRef<RecordingSession | null>(null);
  /** 朗读：自动朗读开关 + 正在播放的消息 id + 当前播放器 */
  const [autoSpeak, setAutoSpeak] = useState(false);
  const [speakingId, setSpeakingId] = useState<string | null>(null);
  const playerRef = useRef<HTMLAudioElement | null>(null);

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
        const replyId = `c-${Date.now()}`;
        setMessages((prev) => [
          ...prev,
          {
            id: replyId,
            role: 'companion',
            content: reply,
            time: formatTime(new Date()),
          },
        ]);
        // 自动朗读（开关打开时；朗读失败静默降级，不影响消息展示）
        if (autoSpeak) {
          void speak(replyId, reply);
        }
      } else {
        // 守卫端点占位响应（无可用回复字段）：不做假回复
        setChannel('unavailable');
        markFailed(meId);
      }
    } catch (err) {
      // 不可达：标注该条消息未送达，不伪造任何回复文本
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

  /** 停止当前朗读（若有）。 */
  function stopSpeaking() {
    const player = playerRef.current;
    playerRef.current = null;
    setSpeakingId(null);
    if (player) {
      try {
        player.pause();
      } catch {
        /* 忽略：播放器已结束或不可暂停 */
      }
    }
  }

  /** 朗读指定消息（先停当前播放；失败仅记日志，不影响聊天链路）。 */
  async function speak(messageId: string, text: string) {
    stopSpeaking();
    if (!text.trim()) return;
    setSpeakingId(messageId);
    try {
      const result = await synthesizeSpeech(text);
      if (result?.ok !== true || !result.audio_base64) {
        throw new Error(result?.message || '朗读服务暂不可用');
      }
      const player = new Audio(
        `data:${result.mime || 'audio/wav'};base64,${result.audio_base64}`,
      );
      playerRef.current = player;
      player.onended = () => setSpeakingId(null);
      player.onerror = () => setSpeakingId(null);
      await player.play();
    } catch (err) {
      console.error('[Chat] 朗读失败:', err);
      setSpeakingId(null);
    }
  }

  /** 麦克风：点击开始录音，再点停止并把识别文本填入输入框。 */
  async function toggleMic() {
    if (recording) {
      const session = sessionRef.current;
      sessionRef.current = null;
      setRecording(false);
      if (!session) return;
      try {
        const audio = await session.stop();
        const result = await transcribeAudio(audio.audioBase64, audio.sampleRate);
        if (result?.ok === true && typeof result.text === 'string') {
          setDraft(result.text);
        }
      } catch (err) {
        console.error('[Chat] 语音输入失败:', err);
      }
      return;
    }
    try {
      sessionRef.current = await startRecording();
      setRecording(true);
    } catch (err) {
      console.error('[Chat] 麦克风不可用:', err);
    }
  }

  return (
    <div className="flex h-full flex-col p-5">
      {/* 标题区（页头不挂形象：桌宠在悬浮窗与桌宠页里以真实 3D 呈现） */}
      <div className="mb-3 flex items-start justify-between gap-3">
        <div>
          <h1 className="text-xl font-bold text-gradient">聊天</h1>
          <p className="text-sm text-[var(--text-secondary)]">想聊什么都可以，我会好好记着的</p>
        </div>
        <label className="flex shrink-0 items-center gap-2 pt-1 text-xs text-[var(--text-secondary)]">
          <span>朗读回复</span>
          <Toggle checked={autoSpeak} onChange={setAutoSpeak} label="朗读回复" />
        </label>
      </div>

      {/* 连接状态提示条：style 对齐 MemoriesPage 离线横幅；直到确认真连通才隐藏 */}
      {channel !== 'connected' && (
        <div className="mb-3 rounded-xl border border-[var(--glass-border)] bg-[rgba(124,216,255,0.08)] px-3 py-2 text-xs text-[var(--text-secondary)]">
          现在连不上 TA，消息暂时送不到哦～等连接恢复后就能正常聊天了
        </div>
      )}

      {/* 消息列表 */}
      <div ref={listRef} className="glass-panel flex-1 overflow-y-auto p-4">
        <div className="flex flex-col gap-3">
          {messages.map((m) => (
            <MessageBubble
              key={m.id}
              msg={m}
              speaking={speakingId === m.id}
              onSpeak={
                m.role === 'companion'
                  ? () => {
                      void speak(m.id, m.content);
                    }
                  : undefined
              }
            />
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
          aria-label={recording ? '停止语音输入' : '语音输入'}
          title={recording ? '停止并识别' : '语音输入'}
          onClick={() => void toggleMic()}
          className={[
            'flex h-10 w-10 shrink-0 items-center justify-center rounded-full transition-all duration-200 hover:scale-105 active:scale-95',
            recording
              ? 'animate-pulse bg-[rgba(255,145,210,0.22)] text-[var(--color-primary)]'
              : 'text-[var(--text-secondary)] hover:bg-[rgba(255,255,255,0.12)] hover:text-[var(--color-primary)]',
          ].join(' ')}
        >
          <Mic className="h-4 w-4" aria-hidden="true" />
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
 * 从响应中提取明确的回复文本；形状不符 / 无内容一律返回 null（绝不伪造）。
 *
 * H3 表情端点：clean_text 为解析后的干净回复（已识别情绪标签已剥离），优先采用；
 * 兼容旧端点 reply / reply_text 字段。
 *
 * 守卫端点会返回 200 + `{"ok":false,"error":"chat_service_disabled",...}` 这类失败说明：
 * ok === false 或存在非空 error 字段即判定为不可用返回 null，走「未送达」路径，
 * 绝不把故障说明文本（message/content 等）渲染成伴侣气泡。
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

function formatTime(d: Date): string {
  return d.toTimeString().slice(0, 5);
}

/** 气泡头像：32px 圆角方形 + 淡色底 + lucide 图标（对齐设计参考项目的写法）。 */
function MessageBubble({
  msg,
  speaking = false,
  onSpeak,
}: {
  msg: ChatMessage;
  /** 该条是否正在朗读（播放态显示为高亮呼吸） */
  speaking?: boolean;
  /** 伴侣气泡的朗读回调；缺省不渲染朗读按钮 */
  onSpeak?: () => void;
}) {
  const isMe = msg.role === 'me';
  const failed = msg.status === 'failed';
  return (
    <div className={`animate-fade-up flex gap-2.5 ${isMe ? 'flex-row-reverse' : ''}`}>
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
        <span className="mt-1 flex items-center gap-1.5 px-1 text-[11px] text-[var(--text-tertiary)]">
          {failed && <span className="font-medium text-[var(--color-error)]">未送达</span>}
          <span>{msg.time}</span>
          {onSpeak && (
            <button
              type="button"
              aria-label={speaking ? '停止朗读' : '朗读'}
              title={speaking ? '停止朗读' : '朗读'}
              onClick={onSpeak}
              className={[
                'flex h-5 w-5 items-center justify-center rounded-full transition-colors duration-200',
                speaking
                  ? 'animate-pulse text-[var(--color-primary)]'
                  : 'text-[var(--text-tertiary)] hover:text-[var(--color-primary)]',
              ].join(' ')}
            >
              <Volume2 className="h-3.5 w-3.5" aria-hidden="true" />
            </button>
          )}
        </span>
      </div>
    </div>
  );
}