import React from 'react';
import ReactDOM from 'react-dom/client';
import App from './App';
import './index.css';

// 应用入口：将 React 根组件挂载到 index.html 中的 #root 节点
// React.StrictMode 仅在开发期做额外检查，不影响生产构建
ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
);
