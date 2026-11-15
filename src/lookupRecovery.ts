import {
  loadSequence, LookupExpiredError, watchInventory, watchPlayback, watchSelection,
  type ArchivePatch, type Exposure, type InventoryUpdate, type PlaybackPatch, type PlaybackUpdate,
} from './exposure';

const unavailable = 'The original exposures could not be restored.';

// Keep the workspace's selection IDs stable while replacing server jobs. Callers
// sharing a missing job wait for the same rebuild and each retry its own action once.
export class LookupRecovery<T> {
  private lifetime = new AbortController();
  private replacements = new Map<string, T>();
  private pending = new Map<string, Promise<T>>();

  constructor(private changed: (pending: boolean) => void) {}

  get(id: string | undefined) {
    return id === undefined ? undefined : this.replacements.get(id);
  }

  reset() {
    this.lifetime.abort();
    this.lifetime = new AbortController();
    this.pending.clear();
    this.replacements.clear();
    this.changed(false);
  }

  async run<R>(id: string, signal: AbortSignal, action: (replacement: T | undefined) => Promise<R>,
    rebuild: (signal: AbortSignal) => Promise<T>): Promise<R> {
    const lifetime = this.lifetime.signal;
    const before = this.get(id);
    try {
      return await action(before);
    } catch (error) {
      if (!(error instanceof LookupExpiredError)) throw error;
    }
    signal.throwIfAborted();
    lifetime.throwIfAborted();
    let replacement = this.get(id);
    if (replacement === before) {
      let pending = this.pending.get(id);
      if (!pending) {
        const deadline = AbortSignal.any([lifetime, AbortSignal.timeout(300_000)]);
        pending = rebuild(deadline).then((result) => {
          deadline.throwIfAborted();
          this.replacements.set(id, result);
          return result;
        }).finally(() => {
          if (!lifetime.aborted) {
            this.pending.delete(id);
            this.changed(this.pending.size > 0);
          }
        });
        this.pending.set(id, pending);
        this.changed(true);
      }
      replacement = await pending;
    }
    signal.throwIfAborted();
    lifetime.throwIfAborted();
    return action(replacement);
  }
}

export async function restoreInventory(patch: ArchivePatch, signal: AbortSignal) {
  const inventory = await watchInventory(patch, undefined, signal, () => {});
  if (inventory.status !== 'ready' || !inventory.jobId) {
    throw new Error(inventory.message || 'The exposure list could not be restored.');
  }
  return { ...inventory, jobId: inventory.jobId };
}

export function requireInventoryKeys(inventory: InventoryUpdate | undefined, keys: string[]) {
  if (inventory && keys.some((key) => !inventory.exposures?.some((item) => item.key === key))) {
    throw new Error(unavailable);
  }
}

function sameKeys(first: string[], second: string[]) {
  return first.length === second.length && first.every((key, index) => key === second[index]);
}

export async function restoreComparison(patch: PlaybackPatch, keys: string[], mode: PlaybackUpdate['mode'],
  auto: boolean, signal: AbortSignal) {
  if (auto) {
    const search = await watchPlayback(patch, signal, () => {});
    const match = (search.sequenceCandidates ?? search.sequences ?? [])
      .find((sequence) => sequence.keys && sameKeys(sequence.keys, keys));
    if (match) {
      const result = await loadSequence(match.id, signal);
      if (result.status === 'ready' && result.mode === mode && sameKeys(result.frames.map((frame) => frame.key), keys)) {
        return result;
      }
    }
  }
  // A fresh Auto search can rank a different sequence first or omit one it had
  // previously found. Render the original products directly if they still qualify.
  const inventory = await restoreInventory(patch, signal);
  requireInventoryKeys(inventory, keys);
  const autoMatch = auto ? { minGapDays: patch.minGapDays, maxGapDays: patch.maxGapDays } : undefined;
  const result = await watchSelection(inventory.jobId, keys, signal, () => {}, autoMatch);
  if (result.status !== 'ready' || result.mode !== mode || !sameKeys(result.frames.map((frame) => frame.key), keys)) {
    throw new Error(unavailable);
  }
  return result;
}

export function correspondingExposure(result: PlaybackUpdate, original: Exposure) {
  return result.frames.find((frame) => frame.key === original.key)
    ?? (result.preview?.key === original.key ? result.preview : undefined);
}
