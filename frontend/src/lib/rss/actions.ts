export type RssActionState = {
  state: string;
  links?: string[];
};

export function canCancelAction(action: RssActionState): boolean {
  return ['queued', 'retrieving_links', 'links_ready'].includes(action.state);
}

export function canRetryAction(action: RssActionState): boolean {
  return ['failed', 'cancelled'].includes(action.state);
}

export function canCopyActionLinks(action: RssActionState): boolean {
  return action.state === 'links_ready' && Boolean(action.links?.length);
}
