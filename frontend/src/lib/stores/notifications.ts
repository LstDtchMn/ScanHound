import { writable } from 'svelte/store';
import { connection } from './connection';

export interface ToastAction {
  label: string;
  run: () => void;
}

export interface Toast {
  id: string;
  title: string;
  body: string;
  priority: string;
  timestamp: number;
  action?: ToastAction;
}

const MAX_TOASTS = 5;
const TOAST_DURATION = 5000;

export const toasts = writable<Toast[]>([]);

connection.on('notification', (data) => {
  addToast(
    data.title as string,
    data.body as string,
    data.priority as string
  );
});

export function addToast(
  title: string,
  body: string,
  priority = 'normal',
  action?: ToastAction
) {
  const id = crypto.randomUUID();
  const toast: Toast = { id, title, body, priority, timestamp: Date.now(), action };

  toasts.update((t) => {
    const next = [toast, ...t].slice(0, MAX_TOASTS);
    return next;
  });

  setTimeout(() => {
    toasts.update((t) => t.filter((x) => x.id !== id));
  }, TOAST_DURATION);
}

export function dismissToast(id: string) {
  toasts.update((t) => t.filter((x) => x.id !== id));
}
