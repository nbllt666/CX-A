import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, cleanup } from '@testing-library/react';
import ChatPage from '../../src/renderer/pages/ChatPage';
import { API_ENDPOINTS } from '../../src/renderer/api';

/**
 * Test1 · ChatPage 发送消息后 fetch 失败的真实链路降级。
 *
 * 断言（mock 网络层为 rejected fetch，全程无伪造伴侣回复）：
 *  1. 用户气泡出现且带「未送达」标记；
 *  2. 「聊天通道尚未接入」提示条可见；
 *  3. 不出现任何伴侣回复气泡（🐱 头像 / 回复文本）。
 */
describe('ChatPage：发送消息后 fetch 失败', () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    // mock 网络层：后端不可达（reject），模拟真实「8600 未起」场景
    fetchMock = vi.fn().mockRejectedValue(new Error('ECONNREFUSED: backend not running'));
    vi.stubGlobal('fetch', fetchMock);
    vi.spyOn(console, 'error').mockImplementation(() => {});
    window.localStorage.clear();
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it('用户气泡带未送达标记、提示条出现、无伪造伴侣回复', async () => {
    render(<ChatPage />);

    // 空态引导可见
    expect(screen.getByText(/还没有聊天记录/)).toBeInTheDocument();

    // 输入并发送
    const input = screen.getByPlaceholderText('跟你的伴侣说点什么吧…');
    fireEvent.change(input, { target: { value: '你好呀' } });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));

    // 网络层被真实调用一次：POST /api/chat/messages
    await vi.waitFor(() => {
      expect(fetchMock).toHaveBeenCalledTimes(1);
    });
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe(API_ENDPOINTS.chat.sendMessage);
    expect(init.method).toBe('POST');
    expect(String(init.body)).toContain('你好呀');

    // 用户气泡出现且内容正确
    const myBubble = await screen.findByText('你好呀');
    expect(myBubble).toBeInTheDocument();

    // 带「未送达」失败标记（气泡元信息行内的红色小字）
    const failedMark = await screen.findByText(/未送达 · 聊天通道尚未接入/);
    expect(failedMark).toBeInTheDocument();

    // 「聊天通道尚未接入」提示条常显（channel !== 'connected'）
    const banner = await screen.findByText(/消息暂时送不到/);
    expect(banner).toBeInTheDocument();
    expect(banner.textContent).toContain('聊天通道尚未接入');

    // 全程无伪造伴侣回复：不存在 🐱 伴侣头像节点（fetch 已 reject，companion 分支不可能触达）
    expect(screen.queryByText('🐱')).not.toBeInTheDocument();

    // 消息总数仍为 1 条（只有用户气泡）
    const bubbles = document.querySelectorAll('.rounded-2xl.px-3\\.5');
    expect(bubbles.length).toBe(1);

    // 失败后按钮恢复可点（sending 态复位）
    expect(screen.getByRole('button', { name: '发送' })).not.toBeDisabled();
  });

  it('占位响应（2xx 但无回复字段）同样标未送达、不伪造回复', async () => {
    fetchMock.mockResolvedValue(
      new Response(JSON.stringify({ error: 'chat service disabled' }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    );
    render(<ChatPage />);
    fireEvent.change(screen.getByPlaceholderText('跟你的伴侣说点什么吧…'), {
      target: { value: '在吗' },
    });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));

    await screen.findByText(/未送达 · 聊天通道尚未接入/);
    expect(screen.queryByText('🐱')).not.toBeInTheDocument();
    expect(screen.getByText(/消息暂时送不到/)).toBeInTheDocument();
  });

  it('ok:false 守卫响应不渲染伴侣回复（故障说明文本不得当回复展示）', async () => {
    // 后端守卫端点真实形状：200 + ok:false + error + message（失败说明文本）
    fetchMock.mockResolvedValue(
      new Response(
        JSON.stringify({
          ok: false,
          error: 'chat_service_disabled',
          message: '聊天服务未启用，请先启动后端',
        }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      ),
    );
    render(<ChatPage />);
    fireEvent.change(screen.getByPlaceholderText('跟你的伴侣说点什么吧…'), {
      target: { value: '在吗' },
    });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));

    // 走 unavailable + markFailed 路径：用户气泡带未送达标记
    await screen.findByText(/未送达 · 聊天通道尚未接入/);
    // 故障说明文本绝不渲染成伴侣气泡
    expect(screen.queryByText(/聊天服务未启用/)).not.toBeInTheDocument();
    expect(screen.queryByText(/chat_service_disabled/)).not.toBeInTheDocument();
    // 无伴侣头像节点
    expect(screen.queryByText('🐱')).not.toBeInTheDocument();
    // 「聊天通道尚未接入」提示条常显
    expect(screen.getByText(/消息暂时送不到/)).toBeInTheDocument();
    // 只有用户自己的一条气泡
    const bubbles = document.querySelectorAll('.rounded-2xl.px-3\\.5');
    expect(bubbles.length).toBe(1);
  });
});
