import type { AuthUser, LoginResult } from '../types';
import { requestJson } from './http';

// P2-F2：用户鉴权 API 服务
// 职责：封装后端 /auth 三个接口（注册 / 登录 / 当前用户），
//       统一走 /api 前缀（Vite 开发代理与 nginx 生产反代都会剥离 /api 前缀转发到 FastAPI）。
//
// 令牌的存储不在本文件完成——登录成功后由上层（hooks/useAuth 的 AuthProvider）
// 负责写入 localStorage，保持「网络层只管通信、状态层只管状态」的职责分离。
//
// 后端契约（见 backend/app/api/auth.py）：
//   POST /auth/register  201 → UserOut
//   POST /auth/login     200 → TokenResponse { access_token, token_type, expires_in, user }
//   GET  /auth/me        200 → UserOut（需 Authorization 头）

/**
 * 注册新账号。
 * 说明：角色由服务端固定为 'user'，前端不可指定（杜绝客户端提权）。
 * @param username 登录名（3~64 字符）
 * @param password 密码（8~72 位，需含字母和数字）
 * @param email 可选邮箱
 * @returns 注册成功的用户信息（**不含令牌**，需再调用 login 登录）
 */
export async function register(
  username: string,
  password: string,
  email?: string
): Promise<AuthUser> {
  return requestJson<AuthUser>('/auth/register', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username, password, email: email || null }),
  });
}

/**
 * 登录：校验用户名密码并换取 JWT 访问令牌。
 * @param username 登录名
 * @param password 密码
 * @returns 令牌与用户信息（由上层负责持久化令牌）
 */
export async function login(username: string, password: string): Promise<LoginResult> {
  return requestJson<LoginResult>('/auth/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username, password }),
  });
}

/**
 * 获取当前登录用户（校验本地令牌是否仍然有效）。
 * 用途：刷新页面时用它判断 token 是否过期；过期会抛 401 并由全局处理器跳转登录页。
 * @returns 当前用户信息
 */
export async function fetchMe(): Promise<AuthUser> {
  return requestJson<AuthUser>('/auth/me', {
    method: 'GET',
    headers: { Accept: 'application/json' },
  });
}
