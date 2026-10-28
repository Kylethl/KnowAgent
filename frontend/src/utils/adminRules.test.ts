// 行级业务规则单测（vitest）
// ---------------------------------------------------------------------
// 为什么有这些用例：
//   线上真实故障——提交人 u_4ff45c 的《部署手册.md》卡在 indexing，后台只剩「预览」可点。
//   根因之一就是「重新入库」按钮的 disabled 条件写成 `status === 'indexing'`，
//   把「真的在跑」与「上次任务已中断」一视同仁地禁用。
//   这类缺陷 100% 在 UI 分支里，tsc 与 build 都发现不了，只能靠单测锁死。
//
// 覆盖策略：只测纯函数（不渲染组件、不连后端），因此：
//   - 无需 jsdom / @testing-library，运行在 node 环境即可；
//   - 用例即业务规则清单，改坏条件立刻红。

import { describe, expect, it } from 'vitest';
import { canReindex, canUnpublish, isIndexingStuck, stuckMinutes } from './adminRules';
import type { AdminDocument } from '../types';

/** 构造一个满足规则函数入参的最小行对象（只保留判定需要的字段）。 */
function row(overrides: Partial<AdminDocument>): Pick<
  AdminDocument,
  'status' | 'review_status' | 'indexing_stale' | 'indexing_elapsed_seconds'
> {
  return {
    status: 'indexed',
    review_status: 'approved',
    indexing_stale: false,
    indexing_elapsed_seconds: null,
    ...overrides,
  };
}

describe('isIndexingStuck', () => {
  it('非入库中状态永远不算卡住', () => {
    // 即使后端脏数据把 indexing_stale 置 true，只要状态不是 indexing 也不该提示卡住
    expect(isIndexingStuck(row({ status: 'indexed', indexing_stale: true }))).toBe(false);
    expect(isIndexingStuck(row({ status: 'approved' }))).toBe(false);
    expect(isIndexingStuck(row({ status: 'pending' }))).toBe(false);
  });

  it('真在入库中（未超时）不算卡住', () => {
    expect(isIndexingStuck(row({ status: 'indexing', indexing_stale: false }))).toBe(false);
  });

  it('入库中且后端判定僵死时为 true', () => {
    expect(isIndexingStuck(row({ status: 'indexing', indexing_stale: true }))).toBe(true);
  });
});

describe('canReindex', () => {
  it('已审核通过且已入库：可重新入库', () => {
    expect(canReindex(row({ status: 'indexed' }))).toBe(true);
  });

  it('未审核通过：不可入库（后端会 409）', () => {
    expect(canReindex(row({ status: 'pending', review_status: 'pending' }))).toBe(false);
    expect(canReindex(row({ status: 'rejected', review_status: 'rejected' }))).toBe(false);
  });

  it('真在入库中：禁用，避免并发重跑重复计费', () => {
    expect(canReindex(row({ status: 'indexing', indexing_stale: false }))).toBe(false);
  });

  // ★ 核心回归用例：历史 bug 就是漏了这一条
  it('入库僵死：必须放行，否则文档永久卡住、只剩预览可用', () => {
    expect(canReindex(row({ status: 'indexing', indexing_stale: true }))).toBe(true);
  });

  it('审核未通过优先于僵死：即使僵死也不可入库', () => {
    // 僵死只解决「状态解锁」，不能绕过审核前置条件
    expect(
      canReindex(row({ status: 'indexing', indexing_stale: true, review_status: 'rejected' }))
    ).toBe(false);
  });
});

describe('canUnpublish', () => {
  it('已入库 / 未入库：可下线', () => {
    expect(canUnpublish(row({ status: 'indexed' }))).toBe(true);
    expect(canUnpublish(row({ status: 'approved' }))).toBe(true);
  });

  it('真在入库中：不可下线（会与后台任务冲突）', () => {
    expect(canUnpublish(row({ status: 'indexing', indexing_stale: false }))).toBe(false);
  });

  it('入库僵死：可下线（否则两种处置手段被同一状态同时堵死）', () => {
    expect(canUnpublish(row({ status: 'indexing', indexing_stale: true }))).toBe(true);
  });
});

describe('stuckMinutes', () => {
  it('按秒换算为分钟并四舍五入', () => {
    // 线上实际值：停在入库中约 16 小时
    expect(stuckMinutes(57335)).toBe(956);
    expect(stuckMinutes(90)).toBe(2);
  });

  it('至少返回 1：避免出现「已中断 0 分钟」这种不可读文案', () => {
    expect(stuckMinutes(0)).toBe(1);
    expect(stuckMinutes(20)).toBe(1);
  });

  it('无值 / 非法值返回 null，由调用方决定兜底文案', () => {
    expect(stuckMinutes(null)).toBeNull();
    expect(stuckMinutes(undefined)).toBeNull();
    expect(stuckMinutes(Number.NaN)).toBeNull();
  });
});
