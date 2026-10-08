/** A readable message from a failed API call. The API answers errors as {"detail": …}: a sentence, or
 * pydantic's list of validation errors (the first one's message is shown). */
export function apiErrorText(error: unknown): string {
  const raw = String(error instanceof Error ? error.message : error);
  try {
    const detail = JSON.parse(raw).detail;
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail) && detail[0]?.msg) return String(detail[0].msg).replace(/^Value error, /, "");
    return detail === undefined ? raw : JSON.stringify(detail);
  } catch {
    return raw;
  }
}
