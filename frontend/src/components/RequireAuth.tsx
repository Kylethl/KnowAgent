import type { ReactNode } from 'react';
import { Navigate, useLocation } from 'react-router-dom';
import { Spin } from 'antd';
import { useAuth } from '../hooks/useAuth';

// P2-F2：路由守卫
// ---------------------------------------------------------------------
// 职责：保护需要登录的页面——未登录时重定向到 /login，
//       并记住来源路径，登录后可以跳回原来想去的页面。
//
// 为什么需要 initializing 判断：
//   刷新页面时本地可能有令牌，但需要调 /auth/me 确认它是否仍有效；
//   这段校验期间若直接判定「未登录」会误跳登录页（表现为刷新即被踢出），
//   因此校验中先渲染加载态，校验结束再决定放行还是跳转。

/**
 * 受保护路由的包裹组件。
 * @param children 需要登录后才能访问的页面内容
 */
export default function RequireAuth({ children }: { children: ReactNode }) {
  const { user, initializing } = useAuth();
  const location = useLocation();

  // 首次令牌校验中：展示加载态，避免误判未登录而闪烁跳转
  if (initializing) {
    return (
      <div
        style={{
          minHeight: '100vh',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          background: '#f7f8fa',
        }}
      >
        <Spin size="large" tip="正在校验登录状态…">
          {/* Spin 的 tip 需要包裹子元素才显示文案，这里用空占位撑起布局 */}
          <div style={{ padding: 24 }} />
        </Spin>
      </div>
    );
  }

  // 未登录：跳登录页，并通过 state 记住来源路径（登录后跳回）
  if (!user) {
    return <Navigate to="/login" replace state={{ from: location.pathname }} />;
  }

  return <>{children}</>;
}
