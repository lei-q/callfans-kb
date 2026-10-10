/**
 * 生成类型的编译冒烟 + 未来 api-client 的雏形。
 *
 * 类型全部来自 src/types/api.d.ts（由 ../docs/api/openapi.json 生成，
 * 事实源是 kb/api_schema.py）——手写类型 = 违约，CI 漂移检查会打红。
 *
 * `npx tsc --noEmit` 校验本文件，保证生成物可用且类型链完整。
 */
import type { components, paths } from "./types/api.d.ts";

type Health = components["schemas"]["Health"];
type Decision = components["schemas"]["Decision"];
type AccountOverview = components["schemas"]["AccountOverview"];

/** 最小 typed fetch：签名绑定 spec，后端改字段这里立刻编译红。 */
export async function kbFetch<P extends keyof paths, M extends keyof paths[P]>(
  base: string,
  path: P,
  method: M,
  body?: unknown,
  params?: Record<string, string>,
): Promise<unknown> {
  const qs = params
    ? "?" +
      Object.entries(params)
        .map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(v)}`)
        .join("&")
    : "";
  const res = await fetch(base + path + qs, {
    method: method as string,
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!res.ok) {
    throw new Error(`kb api ${String(method)} ${String(path)} -> ${res.status}`);
  }
  return res.json();
}

export async function getHealth(base: string): Promise<Health> {
  return (await kbFetch(base, "/health", "get")) as Health;
}

export async function decide(
  base: string,
  input: components["schemas"]["DecideInput"],
): Promise<Decision> {
  return (await kbFetch(base, "/decide", "post", input)) as Decision;
}

export async function getAccount(
  base: string,
  account: string,
): Promise<AccountOverview> {
  return (await kbFetch(base, "/account", "get", undefined, {
    account,
  })) as AccountOverview;
}
