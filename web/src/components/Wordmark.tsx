/** The mark, which appears in the search panel and again in the tour. */
export function Wordmark({ children }: { children?: React.ReactNode }) {
  return (
    <div className="brand">
      <svg className="mark" viewBox="0 0 16 16" aria-hidden="true">
        <path d="M3.6 6.8 8 1.8l4.4 5" />
        <path d="M1 10.6c1.75 0 1.75-1.7 3.5-1.7s1.75 1.7 3.5 1.7 1.75-1.7 3.5-1.7 1.75 1.7 3.5 1.7" />
        <path
          d="M1 14c1.75 0 1.75-1.7 3.5-1.7S6.25 14 8 14s1.75-1.7 3.5-1.7S13.25 14 15 14"
          opacity=".45"
        />
      </svg>
      <b>Floodline</b>
      {children}
    </div>
  );
}
