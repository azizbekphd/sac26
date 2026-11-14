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

export type Exposure = ArchiveExposure & { imageUrl: string; coverage: number; visit?: number; visitDates?: [string, string] | null };
export type InventoryExposure = ArchiveExposure & { visit: number; wavelengthBounds?: [number, number] | null };
export type Bandpass = { wavelength: number; bandwidth: number };
export type ExposureIssue = 'no-usable-pixels' | 'read-failed';

export type PlaybackUpdate = {
  status: 'searching' | 'ready' | 'no-match' | 'error' | 'cancelled';
  message: string;
  preview: Exposure | null;
  frames: Exposure[];
  comparison: { days: number; overlap: number | null; coverage: number; coverageIncludesBandpass?: boolean;
    bandpass?: [number, number] } | null;
  partial: boolean;
  mode?: 'matched' | 'exploratory';
  exposureIssues?: Record<string, ExposureIssue>;
  motionAvailable?: boolean;
  motionUnavailableReason?: string;
  motionResult?: MotionResult;
  jobId?: string;
};

export type SequenceSummary = {
  id: string;
  count: number;
  visitCount?: number | null;
  exposureIds: string[];
  start: string;
  end: string;
  bandpass: [number, number] | null;
};

export type SequenceSearch = PlaybackUpdate & {
  sequences: SequenceSummary[];
  sequenceCandidates?: SequenceSummary[];
  withdrawnSequenceIds?: string[];
  reconnecting?: boolean;
};

export function exposureIdentity(exposure: ArchiveExposure) {
  return `${exposure.observationId}:D${exposure.detector}`;
}

export function countVisits(frames: Exposure[]) {
  const visits = frames.map((frame) => frame.visit);
  return visits.every((visit) => visit != null) ? new Set(visits).size : undefined;
}

export type InventoryUpdate = PlaybackUpdate & { exposures?: InventoryExposure[] };
export type MotionCandidate = {
  measurements: { frameIndex: number; skyPosition: [number, number]; imagePosition: [number, number] }[];
  displacement: number;
  uncertainty: number;
  score: number;
};
export type MotionResult =
  | { status: 'inspected' | 'partial'; candidates: MotionCandidate[]; visits: (string | null)[] }
  | { status: 'rejected' | 'incomplete'; reason: string; candidates: []; visits: (string | null)[] };

export async function inspectMotion(jobId: string, signal: AbortSignal) {
  return jsonResponse<MotionResult>(
    await post(`/api/playback/${encodeURIComponent(jobId)}/motion`, {}, signal),
    'Motion analysis is unavailable for this comparison.',
  );
}

export type SpectrumPoint = Omit<ArchiveExposure, 'wavelength' | 'bandwidth'> & Bandpass & {
  visit: number;
  flux: number;
  uncertainty: number;
  flags: string[];
};
export type SpectrumUpdate = {
  status: 'searching' | 'ready' | 'partial' | 'empty' | 'timeout' | 'error' | 'cancelled';
  message: string;
  processed: number;
  total: number | null;
  points: SpectrumPoint[];
  reconnecting?: boolean;
};

export async function watchSpectrum(
  body: SkyPosition & { startDate: string; endDate: string; release: ArchiveRelease | null },
  signal: AbortSignal, update: (result: SpectrumUpdate) => void,
) {
  const unavailable = 'The spectrum service is unavailable. Try again.';
  // Finish reading the ID even if the user cancels during submission, so the job can be deleted.
  const { id } = await jsonResponse<{ id: string }>(await post('/api/spectrum', body), undefined, unavailable);
  const path = `/api/spectrum/${encodeURIComponent(id)}`;
  let previous: SpectrumUpdate | undefined;
  let failures = 0;
  try {
    for (;;) {
      signal.throwIfAborted();
      const response = await fetch(path, { signal: AbortSignal.any([signal, AbortSignal.timeout(5000)]) })
        .catch((error: unknown) => {
          signal.throwIfAborted();
          if (!(error instanceof TypeError || (error instanceof DOMException && error.name === 'TimeoutError'))) throw error;
          return null;
        });
      signal.throwIfAborted();
      if (!response || response.status >= 500) {
        if (++failures >= 6) throw new Error(unavailable);
        update({ ...(previous ?? { status: 'searching', message: '', processed: 0, total: null, points: [] }),
          reconnecting: true });
      } else {
        const result = await jsonResponse<SpectrumUpdate>(response, undefined, unavailable);
        signal.throwIfAborted();
        previous = result;
        failures = 0;
        update(result);
        if (result.status !== 'searching') return;
      }
      await nextPoll(signal);
    }
  } finally {
    void fetch(path, { method: 'DELETE', keepalive: true }).catch(() => {});
  }
}

export const maxManualFrames = 12;

async function jsonResponse<T>(response: Response, missingEndpoint?: string,
  fallback = 'The playback service is unavailable. Try again.'): Promise<T> {
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    if (missingEndpoint && response.status === 404 && (!body?.detail || body.detail === 'Not Found')) {
      throw new Error(missingEndpoint);
    }
    throw new Error(typeof body?.detail === 'string' ? body.detail : fallback);
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

async function decodeFrames(result: PlaybackUpdate, signal: AbortSignal) {
  await Promise.all(result.frames.map(async (frame) => {
    const image = new Image();
    image.src = frame.imageUrl;
    await image.decode();
  })).catch(() => { throw new Error('Could not load the comparison images. Try again.'); });
  signal.throwIfAborted();
}

export async function loadSequence(id: string, signal: AbortSignal) {
  const result = await jsonResponse<PlaybackUpdate>(await fetch(`/api/playback/${encodeURIComponent(id)}`, { signal }));
  await decodeFrames(result, signal);
  return { ...result, jobId: id };
}

export async function stopPlayback(id: string) {
  const response = await fetch(`/api/playback/${encodeURIComponent(id)}`, { method: 'DELETE' });
  if (!response.ok) await jsonResponse(response);
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
      if (result.status === 'ready') await decodeFrames(result, signal);
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

export async function watchPlayback(
  { position, fieldSize, release, startDate, endDate, minWavelength, maxWavelength, minGapDays, maxGapDays }: PlaybackPatch,
  signal: AbortSignal, update: (result: SequenceSearch) => void,
) {
  const { id } = await jsonResponse<{ id: string }>(await post('/api/playback', {
    ...position, fieldSize, release, startDate, endDate, minWavelength, maxWavelength, minGapDays, maxGapDays,
  }));
  const path = `/api/playback/${encodeURIComponent(id)}`;
  let previous: SequenceSearch | undefined;
  try {
    for (;;) {
      signal.throwIfAborted();
      const response = await fetch(path, { signal }).catch((error: unknown) => {
        signal.throwIfAborted();
        if (!(error instanceof TypeError)) throw error;
        return null;
      });
      if (!response || response.status >= 500) {
        update(previous ? { ...previous, reconnecting: true } : {
          status: 'searching', message: 'Reconnecting to the archive search…', preview: null,
          frames: [], comparison: null, partial: false, sequences: [], reconnecting: true, jobId: id,
        });
      } else {
        const result = await jsonResponse<SequenceSearch>(response);
        signal.throwIfAborted();
        previous = { ...result, jobId: id };
        update(previous);
        if (result.status !== 'searching') return;
      }
      await nextPoll(signal);
    }
  } finally {
    if (signal.aborted) void fetch(path, { method: 'DELETE', keepalive: true }).catch(() => {});
  }
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
