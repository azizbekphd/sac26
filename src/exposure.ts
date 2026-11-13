import type { SkyPosition } from './coordinates';

export const archiveReleases = ['dr1', 'qr3'] as const;
export type ArchiveRelease = typeof archiveReleases[number];
export type ArchivePatch = {
  position: SkyPosition;
  fieldSize: number;
  release: ArchiveRelease | null;
  startDate: string | null;
  endDate: string | null;
  minWavelength: number | null;
  maxWavelength: number | null;
};
export type PlaybackPatch = ArchivePatch & { minGapDays: number; maxGapDays: number | null };

export type ArchiveExposure = {
  key: string;
  observationId: string;
  detector: number;
  observedAt: string;
  release: string;
  wavelength: number | null;
  bandwidth?: number | null;
  coverage: number | null;
  sourceUrl: string;
};

export type Exposure = ArchiveExposure & { imageUrl: string; coverage: number; visit?: number };
export type InventoryExposure = ArchiveExposure & { visit: number; wavelengthBounds?: [number, number] | null };
export type Bandpass = { wavelength: number; bandwidth: number };
export type ExposureIssue = 'no-usable-pixels' | 'read-failed';

export type PlaybackUpdate = {
  status: 'searching' | 'ready' | 'no-match' | 'error' | 'cancelled';
  message: string;
  preview: Exposure | null;
  frames: Exposure[];
  comparison: { days: number; overlap: number | null; coverage: number; coverageIncludesBandpass?: boolean } | null;
  partial: boolean;
  mode?: 'matched' | 'exploratory';
  exposureIssues?: Record<string, ExposureIssue>;
  jobId?: string;
};

export type InventoryUpdate = PlaybackUpdate & { exposures?: InventoryExposure[] };
export const maxManualFrames = 12;

async function jsonResponse<T>(response: Response, missingEndpoint?: string): Promise<T> {
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    if (missingEndpoint && response.status === 404 && (!body?.detail || body.detail === 'Not Found')) {
      throw new Error(missingEndpoint);
    }
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

function post(path: string, body: object, signal?: AbortSignal) {
  return fetch(path, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body), signal,
  });
}

async function watchJob<T extends PlaybackUpdate>(
  start: string, body: object, signal: AbortSignal, update: (result: T) => void, missingEndpoint?: string,
) {
  // Read the job ID even if the visitor cancels before the POST response arrives.
  const { id } = await jsonResponse<{ id: string }>(await post(start, body), missingEndpoint);
  const path = `/api/playback/${encodeURIComponent(id)}`;
  let finished = false;
  try {
    for (;;) {
      const result = await jsonResponse<T>(await fetch(path, { signal }));
      if (result.status === 'ready') {
        await Promise.all(result.frames.map(async (frame) => {
          const image = new Image();
          image.src = frame.imageUrl;
          await image.decode();
        })).catch(() => { throw new Error('Could not load the comparison images. Try again.'); });
      }
      signal.throwIfAborted();
      update({ ...result, jobId: id });
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

export function watchPlayback(
  { position, fieldSize, release, startDate, endDate, minWavelength, maxWavelength, minGapDays, maxGapDays }: PlaybackPatch,
  signal: AbortSignal, update: (result: PlaybackUpdate) => void,
) {
  return watchJob('/api/playback', {
    ...position, fieldSize, release, startDate, endDate, minWavelength, maxWavelength, minGapDays, maxGapDays,
  }, signal, update);
}

export function watchInventory(
  { position, fieldSize, release, startDate, endDate, minWavelength, maxWavelength }: ArchivePatch,
  fromPlayback: string | undefined, signal: AbortSignal,
  update: (result: InventoryUpdate) => void,
) {
  return watchJob('/api/manual/inventory', {
    ...position, fieldSize, release, startDate, endDate, minWavelength, maxWavelength, fromPlayback,
  }, signal, update,
    'This server version cannot list exposures for Manual. Restart or update the API server, then retry.');
}

export function watchSelection(
  id: string, keys: string[], signal: AbortSignal, update: (result: PlaybackUpdate) => void,
) {
  return watchJob(`/api/manual/${encodeURIComponent(id)}/comparison`, { keys }, signal, update);
}

export async function loadBandpasses(id: string, keys: string[], signal: AbortSignal) {
  return jsonResponse<Record<string, Bandpass | null>>(
    await post(`/api/manual/${encodeURIComponent(id)}/calibration`, { keys }, signal),
  );
}

export async function loadExposurePreview(id: string, key: string, signal: AbortSignal) {
  const query = new URLSearchParams({ key });
  const response = await fetch(`/api/manual/${encodeURIComponent(id)}/preview?${query}`, { signal });
  if (!response.ok) {
    await jsonResponse(response, 'Exposure previews are unavailable on this server.');
  }
  return response.blob();
}
