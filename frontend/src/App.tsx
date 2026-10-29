import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom';
import { ConfigProvider, theme } from 'antd';
import zhCN from 'antd/locale/zh_CN';
import ChatPage from './pages/ChatPage';
import LoginPage from './pages/LoginPage';
import AdminPage from './pages/AdminPage';
import OpsPage from './pages/OpsPage';
import RequireAuth from './components/RequireAuth';
import RequireAdmin from './components/RequireAdmin';
import { AuthProvider } from './hooks/useAuth';

// 应用根组件
// 用 antd 的 ConfigProvider 注入全局主题与中文语言包，
// 主色取「豆包蓝」(#386BF3)，统一圆角，确保整体简洁风格一致。
//
// P2-F2 路由结构：
//   /login  登录 / 注册页（唯一不需要登录即可访问的业务页）
//   /       对话页，由 <RequireAuth> 守卫——未登录自动跳转 /login
//   *       兜底重定向到 /
//
// P3-F1 新增：
//   /admin  管理员审核后台，由 <RequireAuth> + <RequireAdmin> 双重守卫——
//           未登录跳 /login，已登录但非管理员展示 403 无权限页。
//           （真正的权限裁决在后端 require_admin，前端守卫仅为体验优化。）
//
// P7 新增：
//   /ops    系统状态页（监控 / 备份 / 审计 / 合规自检），同样双重守卫。
//           放在独立路由而不是塞进 /admin 的原因：
//             「审核内容」与「运维系统」是两类工作，混在一个页面会让
//             日常审核动线被一堆监控卡片打断；分开也便于按角色单独授权。
//
// 层级说明：<AuthProvider> 内部要用 useNavigate（401 时跳登录页），
//          因此必须放在 <BrowserRouter> **内部**。
export default function App() {
  return (
    <ConfigProvider
      locale={zhCN} // 中文语言包：日期选择、空状态等文案本地化
      theme={{
        token: {
          colorPrimary: '#386BF3', // 主色：豆包蓝
          borderRadius: 10, // 统一圆角，贴合豆包风
        },
        algorithm: theme.defaultAlgorithm, // 默认浅色算法（需求约定浅色简洁 UI）
      }}
    >
      <BrowserRouter>
        <AuthProvider>
          <Routes>
            {/* 登录 / 注册：无需鉴权 */}
            <Route path="/login" element={<LoginPage />} />
            {/* 对话页：需登录，未登录由守卫跳转登录页 */}
            <Route
              path="/"
              element={
                <RequireAuth>
                  <ChatPage />
                </RequireAuth>
              }
            />
            {/* P3-F1：管理员审核后台——需登录 + 需管理员角色 */}
            <Route
              path="/admin"
              element={
                <RequireAuth>
                  <RequireAdmin>
                    <AdminPage />
                  </RequireAdmin>
                </RequireAuth>
              }
            />
            {/* P7：系统状态页（监控 / 备份 / 审计 / 合规），仅管理员 */}
            <Route
              path="/ops"
              element={
                <RequireAuth>
                  <RequireAdmin>
                    <OpsPage />
                  </RequireAdmin>
                </RequireAuth>
              }
            />
            {/* 未知路径兜底回首页 */}
            <Route path="*" element={<Navigate to="/" replace />} />
          </Routes>
        </AuthProvider>
      </BrowserRouter>
    </ConfigProvider>
  );
}
