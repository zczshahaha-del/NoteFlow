export const appBasePath = import.meta.env?.BASE_URL || "/";

/** Keep stored Markdown portable between root and subpath deployments. */
export function attachmentHref(href: string): string {
  return /^\/api\/attachments\/[\w-]+\/content(?:[?#]|$)/.test(href)
    ? appBasePath + href.slice(1)
    : href;
}
