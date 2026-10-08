import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// Vite 配置：React 插件 + 开发服务器（端口 5173）
//
// 本地联调约定（与《需求清单》第四节「nginx 反代 /api」保持一致）：
//   - 前端所有接口统一走 /api 前缀（如 /api/chat）；
//   - 生产由 nginx 剥离 /api 前缀后转发到 FastAPI；
//   - 开发期这里用 Vite 代理做同样的事：/api/* → http://localhost:8000/*（剥前缀）。
//
// ⚠️ 关键：必须 rewrite 去掉 /api 前缀，否则 /api/chat 会被原样转发到
//    后端 http://localhost:8000/api/chat，而后端路由是 /chat，结果 404。
//    这曾是导致「前后端完全不通」的根因。

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true,
        // 剥离 /api 前缀：/api/chat → http://localhost:8000/chat
        rewrite: (path) => path.replace(/^\/api/, ''),
        // SSE 流式支持：关闭代理缓冲，保证后端逐片下发时前端能实时收到（打字机效果）
        configure: (proxy) => {
          proxy.on('proxyRes', (proxyRes) => {
            // 透传后端的关键响应头，避免任何中间层（含未来 nginx）缓冲流
            proxyRes.headers['Cache-Control'] = 'no-cache, no-transform';
            proxyRes.headers['X-Accel-Buffering'] = 'no';
          });
        },
      },
    },
  },
});
