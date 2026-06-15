import { writable, derived } from 'svelte/store';
import { connection } from './connection';

export interface LogEntry {
  level: string;
  message: string;
  timestamp: string;
}

const MAX_LOGS = 500;

export const logs = writable<LogEntry[]>([]);
export const logLevelFilter = writable<string>('all');
export const logPanelOpen = writable(false);

connection.on('log', (data) => {
  const entry: LogEntry = {
    level: data.level as string,
    message: data.message as string,
    timestamp: data.timestamp as string
  };
  logs.update((l) => {
    const next = [...l, entry];
    return next.length > MAX_LOGS ? next.slice(-MAX_LOGS) : next;
  });
});

export const filteredLogs = derived(
  [logs, logLevelFilter],
  ([$logs, $filter]) => {
    if ($filter === 'all') return $logs;
    return $logs.filter((l) => l.level === $filter);
  }
);

export function clearLogs() {
  logs.set([]);
}
