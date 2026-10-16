/**
 * Clipboard for copied nodes, shared by all workflow editors of the page
 * (copy in one workflow, paste in another). Also mirrored to the system
 * clipboard where the browser allows it (secure contexts).
 */
let fragment: any = null;
let pasteCount = 0;
let pending: Promise<unknown> | null = null;

/** Register an in-flight copy so a quick paste waits for it. */
export function trackCopy<T>(p: Promise<T>): Promise<T> {
  pending = p;
  void p.finally(() => {
    if (pending === p) {
      pending = null;
    }
  });
  return p;
}

export function setFragment(f: any): void {
  fragment = f;
  pasteCount = 0;
  try {
    void navigator.clipboard?.writeText(JSON.stringify(f)).catch(() => undefined);
  } catch {
    // no system clipboard access
  }
}

/** Fragment to paste, with an increasing offset for repeated pastes. */
export async function getFragment(): Promise<{ fragment: any; offset: number } | null> {
  if (pending) {
    await pending.catch(() => undefined);
  }
  let f = fragment;
  if (!f) {
    try {
      const text = await navigator.clipboard?.readText();
      const parsed = text ? JSON.parse(text) : null;
      if (parsed?.format === 'verknode-fragment/1') {
        f = fragment = parsed;
      }
    } catch {
      // not available or not a fragment
    }
  }
  if (!f) {
    return null;
  }
  pasteCount += 1;
  return { fragment: f, offset: 40 * pasteCount };
}

export function hasFragment(): boolean {
  return fragment !== null;
}

/** Copy text to the system clipboard, with a fallback for insecure (http) contexts. */
export async function copyText(text: string): Promise<void> {
  try {
    await navigator.clipboard.writeText(text);
    return;
  } catch {
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    document.execCommand('copy');
    ta.remove();
  }
}
