/** Every call to the service. Errors carry the server's `detail`, which is where the
 *  useful half of a failure lives - "no 30 m 3DEP tiles cover this unit" rather than
 *  "Internal Server Error". */
export async function api<T>(path: string): Promise<T> {
  const response = await fetch(path);
  if (!response.ok) {
    let detail: string = response.statusText;
    try {
      const body = (await response.json()) as { detail?: string };
      detail = body.detail ?? detail;
    } catch {
      /* a non-JSON error body; the status text is all there is */
    }
    throw new Error(detail);
  }
  return (await response.json()) as T;
}
