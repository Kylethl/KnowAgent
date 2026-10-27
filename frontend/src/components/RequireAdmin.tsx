import type { ReactNode } from 'react';
import { Navigate } from 'react-router-dom';
import { Button, Result } from 'antd';
import { useAuth } from '../hooks/useAuth';

// P3-F1：管理员路由守卫（RBAC 前端侧）
// ---------------------------------------------------------------------
// 职责：保护 /admin 路由——非管理员不得进入管理后台页。
//
// 前端守卫的定位（重要）：
//   它只是「体验优化」，**不是**安全边界。真正的权限裁决在后端
//   core/security.require_admin（非 admin 一律 403）。
//   前端做一层判断，是为了避免普通用户点进后台页后看到一堆 403 报错；
//   即便有人绕过前端直连接口，后端仍会拦截。
//
// 与 RequireAuth 的分工：
//   RequireAuth  判「是否登录」（未登录 → /login）
//   RequireAdmin 判「是否管理员」（已登录但非 admin → 无权限页）
//   两者嵌套使用即可覆盖 /admin 的完整访问控制。

/**
 * 管理员路由守卫。
 * @param children 仅管理员可访问的页面内容
 */
export default function RequireAdmin({ children }: { children: ReactNode }) {
  const { user, initializing } = useAuth();

  // 令牌校验中：不渲染结果页，避免「先闪一下无权限」再跳走
  if (initializing) return null;

  // 未登录：交给登录页（RequireAuth 会处理，这里兜底防御直接访问的场景）
  if (!user) return <Navigate to="/login" replace />;

  // 已登录但非管理员：展示无权限结果页，提供返回对话页的出口
  if (user.role !== 'admin') {
    return (
      <Result
        status="403"
        title="无访问权限"
        subTitle="管理后台仅管理员可用，当前账号为普通用户。"
        extra={
          <Button type="primary" href="/">
            返回对话页
          </Button>
        }
      />
    );
  }

  return <>{children}</>;
}
