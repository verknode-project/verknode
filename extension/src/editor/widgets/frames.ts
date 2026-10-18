import { createContext } from 'react';
import { PageConfig } from '@jupyterlab/coreutils';

/**
 * Pages run in sandboxed frames (views, "html" widgets) refer to files the server serves (e.g.
 * the 3D structure viewers under verknode/vendor) by this placeholder for the server's base URL.
 */
export const BASE_PLACEHOLDER = '__VERKNODE_BASE__';

export function withBaseUrl(html: string): string {
  return html.includes(BASE_PLACEHOLDER) ? html.split(BASE_PLACEHOLDER).join(PageConfig.getBaseUrl()) : html;
}

/** Fetches a canvas display that is too big to come with every view ({deferred: key} in the view). */
export const DisplayLoader = createContext<((nodeId: number) => Promise<{ key: string | null; data: any }>) | null>(null);
