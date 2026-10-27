import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from 'react';
import { useNavigate } from 'react-router-dom';
import type { AuthUser } from '../types';
import { fetchMe, login as apiLogin, register as apiRegister } from '../services/authApi';
import {
  clearAuth,
  getStoredUser,
  getToken,
  setStoredUser,
  setToken,
  setUnauthorizedHandler,
} from '../services/http';

// P2-F2：登录状态容器（AuthContext）
// ---------------------------------------------------------------------
// 职责：把「当前用户 + 令牌」的读写与生命周期集中管理，供登录页、路由守卫、
//       顶栏等各处消费，避免各组件各自读写 localStorage 导致状态不一致。
//
// 关键行为：
//   1. 首次挂载时若本地已有令牌，调 /auth/me 校验有效性；
//      令牌过期/无效则清理本地状态，用户回到未登录（不会出现「假登录」）。
//   2. 注册全局 401 处理器：任何接口返回 401 都自动清理状态并跳登录页，
//      业务组件无需各自判断。

// 对外暴露的鉴权上下文
export interface AuthContextValue {
  user: AuthUser | null; // 当前登录用户（未登录为 null）
  token: string | null; // 当前令牌（未登录为 null）
  initializing: boolean; // 首次令牌校验进行中（用于路由守卫避免误跳转）
  login: (username: string, password: string) => Promise<void>;
  register: (username: string, password: string, email?: string) => Promise<void>;
  logout: () => void;
}

const AuthContext = createContext<AuthContextValue | null>(null);

/**
 * 鉴权状态提供者：必须放在 <BrowserRouter> 内部（内部要用 useNavigate）。
 */
export function AuthProvider({ children }: { children: ReactNode }) {
  const navigate = useNavigate();

  // 初值直接取本地存储，避免刷新瞬间闪一下「未登录」
  const [user, setUser] = useState<AuthUser | null>(getStoredUser);
  const [token, setTokenState] = useState<string | null>(getToken);
  // 仅当本地存在令牌时才需要「校验中」状态（无令牌时直接判定未登录）
  const [initializing, setInitializing] = useState<boolean>(() => getToken() !== null);

  /**
   * 收到 401（令牌缺失/过期/被伪造）时的统一处理：
   * 清理本地状态并跳转登录页。
   */
  const handleUnauthorized = useCallback(() => {
    setTokenState(null);
    setUser(null);
    navigate('/login', { replace: true });
  }, [navigate]);

  // 把 401 处理器注册到 http 层；卸载时注销，避免内存泄漏
  useEffect(() => {
    setUnauthorizedHandler(handleUnauthorized);
    return () => setUnauthorizedHandler(null);
  }, [handleUnauthorized]);

  /**
   * 首次挂载：用 /auth/me 校验本地令牌是否仍有效。
   * 有效 → 补全用户信息；无效 → 清理状态（令牌过期自动退回登录页）。
   */
  useEffect(() => {
    // 无令牌：无需校验，直接结束初始化
    if (!getToken()) {
      setInitializing(false);
      return;
    }
    let cancelled = false;
    void (async () => {
      try {
        const me = await fetchMe();
        if (cancelled) return;
        setUser(me);
        setStoredUser(me); // 用服务端最新信息刷新本地缓存
      } catch {
        // 401 或其它错误：本地令牌不可用，清理后回到未登录
        if (cancelled) return;
        clearAuth();
        setUser(null);
        setTokenState(null);
      } finally {
        if (!cancelled) setInitializing(false);
      }
    })();
    // 卸载时取消后续状态更新，避免在已卸载组件上 setState
    return () => {
      cancelled = true;
    };
  }, []);

  /**
   * 登录：换取令牌后写入本地存储并更新上下文。
   * @param username 登录名
   * @param password 密码
   */
  const login = useCallback(async (username: string, password: string) => {
    const result = await apiLogin(username, password);
    // 先落盘再更新内存状态，保证刷新后仍是登录态
    setToken(result.access_token);
    setStoredUser(result.user);
    setTokenState(result.access_token);
    setUser(result.user);
  }, []);

  /**
   * 注册：仅创建账号（服务端不返回令牌）。
   * 调用方（登录页）在注册成功后需再调用 login 完成登录。
   */
  const register = useCallback(async (username: string, password: string, email?: string) => {
    await apiRegister(username, password, email);
  }, []);

  /**
   * 登出：清理本地令牌与用户信息，并跳转登录页。
   */
  const logout = useCallback(() => {
    clearAuth();
    setTokenState(null);
    setUser(null);
    navigate('/login', { replace: true });
  }, [navigate]);

  // 缓存上下文对象，避免每次渲染都产生新引用导致下游重复渲染
  const value = useMemo<AuthContextValue>(
    () => ({ user, token, initializing, login, register, logout }),
    [user, token, initializing, login, register, logout]
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

/**
 * 获取鉴权上下文。必须在 <AuthProvider> 内部调用。
 */
export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) {
    throw new Error('useAuth 必须在 <AuthProvider> 内部使用');
  }
  return ctx;
}
