import { URLExt } from '@jupyterlab/coreutils';
import { ServerConnection } from '@jupyterlab/services';

/**
 * Call the workflow engine REST API (/verknode/api/...).
 */
export async function requestAPI<T>(endPoint: string, init: RequestInit = {}): Promise<T> {
  const settings = ServerConnection.makeSettings();
  const url = URLExt.join(settings.baseUrl, 'verknode', 'api', endPoint);
  const response = await ServerConnection.makeRequest(url, init, settings);
  if (response.status === 204) {
    return undefined as T;
  }
  const text = await response.text();
  let data: any = text;
  if (text.length > 0) {
    try {
      data = JSON.parse(text);
    } catch {
      // not JSON
    }
  }
  if (!response.ok) {
    throw new Error(data?.message || data?.reason || text || response.statusText);
  }
  return data as T;
}

export function postJSON<T>(endPoint: string, body: unknown): Promise<T> {
  return requestAPI<T>(endPoint, { method: 'POST', body: JSON.stringify(body) });
}
