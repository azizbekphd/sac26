import type { SkyPosition } from './coordinates';

export type Exposure = {
  imageUrl: string;
  observationId: string;
  detector: number;
  observedAt: string;
  release: string;
  wavelength: number | null;
  coverage: number;
  sourceUrl: string;
};

export type PlaybackUpdate = {
  status: 'searching' | 'ready' | 'no-match' | 'error' | 'cancelled';
  message: string;
  preview: Exposure | null;
  frames: [] | [Exposure, Exposure];
  comparison: { days: number; overlap: number; coverage: number } | null;
  partial: boolean;
};

async function jsonResponse<T>(response: Response): Promise<T> {
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    throw new Error(typeof body?.detail === 'string' ? body.detail : 'The playback service is unavailable. Try again.');
  }
  return response.json();
}

function nextPoll(signal: AbortSignal) {
  return new Promise<void>((resolve, reject) => {
    const timer = window.setTimeout(() => {
      signal.removeEventListener('abort', cancel);
      resolve();
    }, 500);
    function cancel() {
      window.clearTimeout(timer);
      reject(signal.reason);
    }
    if (signal.aborted) cancel();
    else signal.addEventListener('abort', cancel, { once: true });
  });
}

export async function watchPlayback(
  position: SkyPosition, fieldSize: number, signal: AbortSignal, update: (result: PlaybackUpdate) => void,
  target?: 'wise-0855',
) {
  // Read the job ID even if the visitor cancels before the POST response arrives.
  const { id } = await jsonResponse<{ id: string }>(await fetch('/api/playback', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ...position, fieldSize, target }),
  }));
  const path = `/api/playback/${encodeURIComponent(id)}`;
  let finished = false;
  try {
    for (;;) {
      const result = await jsonResponse<PlaybackUpdate>(await fetch(path, { signal }));
      if (result.status === 'ready') {
        await Promise.all(result.frames.map(async (frame) => {
          const image = new Image();
          image.src = frame.imageUrl;
          await image.decode();
        })).catch(() => { throw new Error('Could not load the comparison images. Try again.'); });
      }
      signal.throwIfAborted();
      update(result);
      if (result.status !== 'searching') {
        finished = true;
        return;
      }
      await nextPoll(signal);
    }
  } finally {
    if (!finished) void fetch(path, { method: 'DELETE', keepalive: true }).catch(() => {});
  }
}
