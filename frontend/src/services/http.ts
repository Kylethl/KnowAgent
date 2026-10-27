import type { AuthUser } from '../types';

// P2-F2：统一 HTTP 请求与令牌存储基础设施
// ---------------------------------------------------------------------
// 职责：把「/api 前缀」「JWT 令牌的读写」「Authorization 头注入」
//      「错误解析」「401 全局处理」收敛到一处，供各业务 service 复用，
//      避免每个 service 各写一套 fetch + 错误处理 + 令牌拼接。
//
// 令牌存储方案说明（对应《需求清单》P2-F2「token 存储（localStorage/HttpOnly cookie）」）：
//   本阶段采用 **localStorage**：
//     - 优点：实现简单，前端可主动读取并在登出时清理；与 Bearer 头天然配合；
//     - 风险：可能被 XSS 脚本读取（HttpOnly cookie 可防此点，但需要后端
//       配合下发/清理 Cookie，且 SSE(EventSource) 与跨域场景更麻烦）。
//     - 缓解：① 不在页面渲染中直接输出令牌；② 令牌有效期有限（默认 1 天）；
//             ③ P2-Dep1 全站 HTTPS 防中间人窃取。
//   若后续要升级为 HttpOnly Cookie，只需替换本文件的 getToken/setToken 实现，
//   上层 service 无需改动（它们一律通过 apiFetch / requestJson 发请求）。

// 接口地址前缀：与 vite.config.ts 的代理约定一致，统一 /api 前缀
export const API_PREFIX: string = import.meta.env.VITE_API_BASE ?? '/api';

// localStorage 键名：加应用前缀避免与其它站点/环境冲突
const TOKEN_KEY = 'knowagent_access_token';
const USER_KEY = 'knowagent_user';

/**
 * 带 HTTP 状态码的错误类型。
 * 相比原生 Error 额外携带 status，便于上层区分「401 需重新登录」等场景。
 */
export class ApiError extends Error {
  status: number;
  // 后端在 409 等场景下回传的冲突字段（如 'username'），便于前端高亮输入框
  field?: string;

  constructor(message: string, status: number, field?: string) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.field = field;
  }
}

// ---------------------------------------------------------------------
// 令牌与用户信息的本地存储（localStorage）
// 读取失败（隐私模式 / 存储被禁用）一律降级为 null，不阻断页面渲染。
// ---------------------------------------------------------------------

/** 读取本地令牌；不存在或存储不可用时返回 null。 */
export function getToken(): string | null {
  try {
    return window.localStorage.getItem(TOKEN_KEY);
  } catch {
    return null;
  }
}

/** 写入本地令牌。 */
export function setToken(token: string): void {
  try {
    window.localStorage.setItem(TOKEN_KEY, token);
  } catch {
    // 存储不可用时静默失败：此时功能退化为「每次刷新需重新登录」
  }
}

/** 清除本地令牌。 */
export function clearToken(): void {
  try {
    window.localStorage.removeItem(TOKEN_KEY);
  } catch {
    // 同上，忽略存储异常
  }
}

/** 读取本地缓存的用户信息（避免刷新时闪一下未登录态）。 */
export function getStoredUser(): AuthUser | null {
  try {
    const raw = window.localStorage.getItem(USER_KEY);
    if (!raw) return null;
    return JSON.parse(raw) as AuthUser;
  } catch {
    return null;
  }
}

/** 缓存用户信息到本地。 */
export function setStoredUser(user: AuthUser): void {
  try {
    window.localStorage.setItem(USER_KEY, JSON.stringify(user));
  } catch {
    // 忽略存储异常
  }
}

/** 清除本地用户信息。 */
export function clearStoredUser(): void {
  try {
    window.localStorage.removeItem(USER_KEY);
  } catch {
    // 忽略存储异常
  }
}

/** 一次性清理令牌与用户信息（登出 / 401 时使用）。 */
export function clearAuth(): void {
  clearToken();
  clearStoredUser();
}

// ---------------------------------------------------------------------
// 401 全局处理
// ---------------------------------------------------------------------
// 设计：业务 service 只管抛错，跳转登录页的动作由 AuthProvider 注入的回调执行。
// 这样避免每个调用点都写一遍「判断 401 → 清 token → navigate('/login')」。

let unauthorizedHandler: (() => void) | null = null;

/**
 * 注册 401 处理器（由 AuthProvider 在挂载时调用）。
 * @param handler 收到 401 时执行的回调（通常是清理状态并跳转登录页）
 */
export function setUnauthorizedHandler(handler: (() => void) | null): void {
  unauthorizedHandler = handler;
}

/** 判断错误是否为「未认证/令牌失效」。 */
export function isUnauthorized(err: unknown): boolean {
  return err instanceof ApiError && err.status === 401;
}

/**
 * 手动触发 401 处理：清理本地令牌并通知全局处理器跳转登录页。
 * 供不走 requestJson 的请求使用（如 XMLHttpRequest 上传拿不到进度以外的封装）。
 */
export function triggerUnauthorized(): void {
  clearAuth();
  unauthorizedHandler?.();
}

// ---------------------------------------------------------------------
// 请求封装
// ---------------------------------------------------------------------

/**
 * 构造带鉴权头的请求头。
 * 有令牌时自动附加 Authorization: Bearer <token>；无令牌时只保留传入的头。
 */
export function authHeaders(extra: Record<string, string> = {}): Record<string, string> {
  const token = getToken();
  if (!token) return { ...extra };
  return { ...extra, Authorization: `Bearer ${token}` };
}

/**
 * 发起带鉴权头的请求（不抛错，返回原始 Response）。
 * 仅在需要自行处理响应（如流式、blob）时使用；一般业务请用 requestJson。
 */
export async function apiFetch(path: string, init: RequestInit = {}): Promise<Response> {
  const headers = authHeaders((init.headers as Record<string, string> | undefined) ?? {});
  return fetch(`${API_PREFIX}${path}`, { ...init, headers });
}

/**
 * 把非 2xx 响应解析为 ApiError（优先取后端 detail 字段）。
 */
async function toApiError(resp: Response): Promise<ApiError> {
  let detail = `请求失败（${resp.status}）`;
  let field: string | undefined;
  try {
    const data = (await resp.json()) as { detail?: unknown; field?: unknown };
    if (typeof data.detail === 'string') detail = data.detail;
    if (typeof data.field === 'string') field = data.field;
  } catch {
    // 响应体非 JSON（如 nginx HTML 错误页）时沿用默认文案
  }
  return new ApiError(detail, resp.status, field);
}

/**
 * 发起请求并解析 JSON 响应；非 2xx 抛 ApiError。
 * 遇到 401 会先清理本地令牌并触发全局未授权处理器（跳转登录页）。
 */
export async function requestJson<T>(path: string, init: RequestInit = {}): Promise<T> {
  const resp = await apiFetch(path, init);
  if (!resp.ok) {
    const err = await toApiError(resp);
    if (err.status === 401) {
      // 令牌失效：清理本地状态并交给全局处理器跳转登录页
      triggerUnauthorized();
    }
    throw err;
  }
  return (await resp.json()) as T;
}

/**
 * 发起请求但忽略响应体（用于 204 No Content，如删除会话）。
 * 非 2xx 同样抛 ApiError，并处理 401。
 */
export async function requestVoid(path: string, init: RequestInit = {}): Promise<void> {
  const resp = await apiFetch(path, init);
  if (!resp.ok) {
    const err = await toApiError(resp);
    if (err.status === 401) {
      triggerUnauthorized();
    }
    throw err;
  }
  // 204 等无响应体场景不做 JSON 解析
}
