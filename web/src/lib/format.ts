/** Currency, at the magnitudes this model produces: billions down to dollars. */
export function money(value: number | null | undefined, unit: string): string {
  if (value == null) return "—";
  if (value >= 1e9) return `${unit} ${(value / 1e9).toFixed(2)}bn`;
  if (value >= 1e6) return `${unit} ${(value / 1e6).toFixed(0)}M`;
  return `${unit} ${Math.round(value).toLocaleString()}`;
}
