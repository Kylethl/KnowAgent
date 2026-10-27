import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Alert, Button, Card, Form, Input, Tabs, Typography, message } from 'antd';
import { LockOutlined, RobotOutlined, UserOutlined } from '@ant-design/icons';
import { useAuth } from '../hooks/useAuth';

// P2-F2：登录 / 注册页
// ---------------------------------------------------------------------
// 职责：提供账号入口（登录 / 注册两个 Tab），登录成功后写入令牌并跳回对话页。
// 对应《需求清单》P2 功能2 前端部分：「登录/注册页、token 存储、路由守卫」。
//
// 交互约定：
//   - 注册成功后自动用相同账号登录，省去用户再输一次密码；
//   - 后端返回的可读错误（detail）直接展示在页面顶部 Alert，
//     特殊错误（409 用户名占用）由后端回传 field，可进一步高亮输入框。

// 登录表单字段
interface LoginFormValues {
  username: string;
  password: string;
}

// 注册表单字段（多一个确认密码与可选邮箱）
interface RegisterFormValues {
  username: string;
  password: string;
  confirm: string;
  email?: string;
}

export default function LoginPage() {
  const { login, register } = useAuth();
  const navigate = useNavigate();

  // 当前 Tab：login（登录）/ register（注册）
  const [activeTab, setActiveTab] = useState<'login' | 'register'>('login');
  // 提交中标记：禁用按钮防重复提交
  const [submitting, setSubmitting] = useState(false);
  // 页面级错误提示文案（后端返回的 detail）
  const [error, setError] = useState<string | null>(null);

  /**
   * 统一错误展示：把异常转成可读文案并显示在页面顶部。
   * 401 统一提示「用户名或密码错误」之外的场景也走这里。
   */
  const showError = (err: unknown, fallback: string) => {
    const msg = err instanceof Error ? err.message : fallback;
    setError(msg || fallback);
  };

  /**
   * 提交登录：调后端换取令牌，成功后跳回对话页。
   */
  const handleLogin = async (values: LoginFormValues) => {
    setSubmitting(true);
    setError(null);
    try {
      await login(values.username.trim(), values.password);
      message.success('登录成功');
      // replace：登录页不留在历史栈里，避免「返回」又回到登录页
      navigate('/', { replace: true });
    } catch (err) {
      // 登录失败后端统一返回「用户名或密码错误」，此处直接展示
      showError(err, '登录失败，请重试');
    } finally {
      setSubmitting(false);
    }
  };

  /**
   * 提交注册：先注册，成功后立即用同一账号自动登录。
   */
  const handleRegister = async (values: RegisterFormValues) => {
    setSubmitting(true);
    setError(null);
    try {
      await register(values.username.trim(), values.password, values.email?.trim() || undefined);
      // 注册接口不返回令牌，这里补一次登录让用户直接进入对话页
      await login(values.username.trim(), values.password);
      message.success('注册成功，已自动登录');
      navigate('/', { replace: true });
    } catch (err) {
      // 409（用户名/邮箱已占用）由后端 detail 给出明确文案
      showError(err, '注册失败，请重试');
    } finally {
      setSubmitting(false);
    }
  };

  return (
    // 整页居中：浅灰背景 + 居中卡片，与对话页风格一致
    <div
      style={{
        minHeight: '100vh',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        background: '#f7f8fa',
        padding: 16,
      }}
    >
      <Card style={{ width: 380, boxShadow: '0 2px 12px rgba(0,0,0,0.06)' }}>
        {/* 品牌区：与顶栏一致的机器人图标 + 产品名 */}
        <div style={{ textAlign: 'center', marginBottom: 16 }}>
          <RobotOutlined style={{ fontSize: 32, color: '#386BF3' }} />
          <Typography.Title level={4} style={{ margin: '8px 0 0' }}>
            知识库问答
          </Typography.Title>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            登录后可保存会话历史
          </Typography.Text>
        </div>

        {/* 错误提示：展示后端返回的可读错误 */}
        {error && (
          <Alert
            type="error"
            showIcon
            message={error}
            style={{ marginBottom: 12 }}
            // 已认证相关错误给出「重新登录」语义，其它原样展示
            closable
            onClose={() => setError(null)}
          />
        )}

        <Tabs
          activeKey={activeTab}
          onChange={(key) => {
            setActiveTab(key as 'login' | 'register');
            setError(null); // 切换 Tab 时清掉上一次的错误
          }}
          centered
          items={[
            {
              key: 'login',
              label: '登录',
              children: (
                <Form<LoginFormValues>
                  name="login"
                  layout="vertical"
                  onFinish={handleLogin}
                  autoComplete="off"
                  requiredMark={false}
                >
                  <Form.Item
                    name="username"
                    label="用户名"
                    rules={[{ required: true, message: '请输入用户名' }]}
                  >
                    <Input
                      prefix={<UserOutlined />}
                      placeholder="请输入用户名"
                      size="large"
                      autoComplete="username"
                    />
                  </Form.Item>
                  <Form.Item
                    name="password"
                    label="密码"
                    rules={[{ required: true, message: '请输入密码' }]}
                  >
                    <Input.Password
                      prefix={<LockOutlined />}
                      placeholder="请输入密码"
                      size="large"
                      autoComplete="current-password"
                    />
                  </Form.Item>
                  <Button type="primary" htmlType="submit" block size="large" loading={submitting}>
                    登录
                  </Button>
                </Form>
              ),
            },
            {
              key: 'register',
              label: '注册',
              children: (
                <Form<RegisterFormValues>
                  name="register"
                  layout="vertical"
                  onFinish={handleRegister}
                  autoComplete="off"
                  requiredMark={false}
                >
                  <Form.Item
                    name="username"
                    label="用户名"
                    // 与后端 RegisterRequest 的约束保持一致（3~64 字符）
                    rules={[
                      { required: true, message: '请输入用户名' },
                      { min: 3, max: 64, message: '用户名需 3~64 个字符' },
                    ]}
                  >
                    <Input prefix={<UserOutlined />} placeholder="3~64 个字符" size="large" />
                  </Form.Item>
                  <Form.Item
                    name="password"
                    label="密码"
                    // 与后端校验规则一致：8~72 位且含字母和数字
                    rules={[
                      { required: true, message: '请输入密码' },
                      { min: 8, max: 72, message: '密码需 8~72 位' },
                      {
                        validator: (_, value: string) =>
                          !value || (/[A-Za-z]/.test(value) && /\d/.test(value))
                            ? Promise.resolve()
                            : Promise.reject(new Error('密码需同时包含字母和数字')),
                      },
                    ]}
                  >
                    <Input.Password
                      prefix={<LockOutlined />}
                      placeholder="8~72 位，含字母和数字"
                      size="large"
                      autoComplete="new-password"
                    />
                  </Form.Item>
                  <Form.Item
                    name="confirm"
                    label="确认密码"
                    dependencies={['password']}
                    rules={[
                      { required: true, message: '请再次输入密码' },
                      // 两次输入一致性校验（前端先行拦截，减少无效请求）
                      ({ getFieldValue }) => ({
                        validator: (_, value: string) =>
                          !value || value === getFieldValue('password')
                            ? Promise.resolve()
                            : Promise.reject(new Error('两次输入的密码不一致')),
                      }),
                    ]}
                  >
                    <Input.Password
                      prefix={<LockOutlined />}
                      placeholder="请再次输入密码"
                      size="large"
                      autoComplete="new-password"
                    />
                  </Form.Item>
                  <Form.Item
                    name="email"
                    label="邮箱（可选）"
                    rules={[{ type: 'email', message: '邮箱格式不正确' }]}
                  >
                    <Input placeholder="选填，用于找回账号" size="large" />
                  </Form.Item>
                  <Button type="primary" htmlType="submit" block size="large" loading={submitting}>
                    注册并登录
                  </Button>
                </Form>
              ),
            },
          ]}
        />
      </Card>
    </div>
  );
}
