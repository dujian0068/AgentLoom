export async function api<T = any>(
  path: string,
  options: RequestInit = {},
): Promise<T> {
  const response = await fetch("/api" + path, {
    credentials: "same-origin",
    ...options,
    headers: {
      ...(options.body instanceof FormData
        ? {}
        : options.body
          ? { "Content-Type": "application/json" }
          : {}),
      ...options.headers,
    },
  });
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    throw new Error(
      typeof data.detail === "string"
        ? data.detail
        : `请求失败 (${response.status})`,
    );
  }
  return response.json();
}
export const post = (path: string, value: any = {}) =>
  api(path, { method: "POST", body: JSON.stringify(value) });
export const put = (path: string, value: any) =>
  api(path, { method: "PUT", body: JSON.stringify(value) });
