export interface ChangelogEntry {
  version: string;
  date: string; // ISO YYYY-MM-DD
  summary: string;
  changes: string[];
}

export const changelog: ChangelogEntry[] = [
  {
    version: "2.1.1",
    date: "2026-06-27",
    summary: "Settings tab fix & review fixes",
    changes: [
      "Fixed Renaming tab showing Background Scan content (scroll position was preserved across tab switches)",
      "Settings tab nav is now sticky — always visible when scrolling long tabs",
      "Accepting an episode correction now uses the corrected episode's title in the filename",
      "Hardened static-file path checks; build output no longer tracked in git",
    ],
  },
  {
    version: "2.1.0",
    date: "2026-06-27",
    summary: "Episode intelligence & UI polish",
    changes: [
      "StatusBar connection dots — Plex, Meta, JD always visible on every screen",
      "Fixed combined episode filename bug (S01E01E02 now generates correctly)",
      "DB persistence for suggested_correction, combined_episode, split_file",
      "Accept buttons for combined-episode and episode-correction proposals",
      "New API: /rename/jobs/{id}/accept-combined and accept-correction",
    ],
  },
  {
    version: "2.0.0",
    date: "2026-06-20",
    summary: "Episode intelligence engine",
    changes: [
      "Runtime-gated episode/season correction via TMDB runtime matching",
      "Combined double-episode detection (S01E01E02 when runtime ≈ 2× single ep)",
      "Split file detection — flags Part 1/Part 2 sibling pairs",
      "LLM disambiguator for tied episode candidates (Ollama)",
      "Download page hint extraction — detects combined/split before download",
      "Multi-episode filename parsing (E01E02, E01-E02, Part 1 patterns)",
      "Tighter runtime confidence thresholds (neutral zone 10% not 15%)",
    ],
  },
  {
    version: "1.9.0",
    date: "2026-06-19",
    summary: "Scraper reliability",
    changes: [
      "Fixed Chromium/ChromeDriver version mismatch causing zero-link grabs",
      "Fixed winreg-only version detection for undetected-chromedriver",
    ],
  },
];

export const latestVersion = changelog[0];
