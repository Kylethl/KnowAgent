import { ConfigProvider, theme } from 'antd';
import zhCN from 'antd/locale/zh_CN';
import ChatPage from './pages/ChatPage';

// 应用根组件
// 用 antd 的 ConfigProvider 注入全局主题与中文语言包，
// 主色取「豆包蓝」(#386BF3)，统一圆角，确保整体简洁风格一致。
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
      <ChatPage />
    </ConfigProvider>
  );
}
