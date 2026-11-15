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
type PlaybackGap = { minGapDays: number; maxGapDays: number | null };
export type PlaybackPatch = ArchivePatch & PlaybackGap;

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

export type Exposure = ArchiveExposure & {
  imageUrl: string;
  flagsUrl?: string;
  coverage: number;
  visit?: number;
  visitDates?: [string, string] | null;
};
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
  keys?: string[];
  visits?: number[];
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

export type ImageMathOperation = 'stack' | 'difference';

export async function loadImageMath(jobId: string, operation: ImageMathOperation, keys: string[], signal: AbortSignal) {
  const response = await fetchLookup(jobId, signal, () =>
    post(`/api/playback/${encodeURIComponent(jobId)}/image-math`, { operation, keys }, signal));
  if (!response.ok) await jsonResponse(response, 'Image comparison is unavailable.', 'Could not calculate the image. Try again.');
  return response.blob();
}

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
    await fetchLookup(jobId, signal, () => post(`/api/playback/${encodeURIComponent(jobId)}/motion`, {}, signal)),
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

const spectrumUnavailable = 'The spectrum service is unavailable. Try again.';

export function watchSpectrum(
  body: SkyPosition & { startDate: string; endDate: string; release: ArchiveRelease | null },
  signal: AbortSignal, update: (result: SpectrumUpdate) => void,
) {
  // Read the ID even if the user leaves or asks to stop while the job is being submitted.
  const path = post('/api/spectrum', body, AbortSignal.timeout(5000))
    .catch(() => { throw new Error(spectrumUnavailable); })
    .then((response) => jsonResponse<{ id: string }>(response, undefined, spectrumUnavailable))
    .then(({ id }) => `/api/spectrum/${encodeURIComponent(id)}`);
  return {
    finished: path.then((url) => pollSpectrum(url, signal, update)),
    async stop() {
      const url = await path;
      signal.throwIfAborted();
      const response = await post(`${url}/stop`, {}, AbortSignal.any([signal, AbortSignal.timeout(5000)]));
      if (!response.ok) await jsonResponse(response, undefined, spectrumUnavailable);
    },
  };
}

async function pollSpectrum(path: string, signal: AbortSignal, update: (result: SpectrumUpdate) => void) {
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
        if (++failures >= 6) throw new Error(spectrumUnavailable);
        update({ ...(previous ?? { status: 'searching', message: '', processed: 0, total: null, points: [] }),
          reconnecting: true });
      } else {
        const result = await jsonResponse<SpectrumUpdate>(response, undefined, spectrumUnavailable);
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

export class LookupExpiredError extends Error {
  constructor() { super('The archive lookup expired. Try the action again.'); }
}

function checkLookup(response: Response) {
  if (response.status === 404 && response.headers.get('X-Lookup-Expired') === '1') {
    throw new LookupExpiredError();
  }
}

async function jsonResponse<T>(response: Response, missingEndpoint?: string,
  fallback = 'The playback service is unavailable. Try again.'): Promise<T> {
  checkLookup(response);
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

// Render can answer with a loading page or gateway response before FastAPI is ready.
function waking(response: Response) {
  const type = response.headers.get('Content-Type') ?? '';
  return type.includes('text/html') || ([502, 503, 504].includes(response.status) && !type.includes('json'));
}

export async function checkPlaybackJob(id: string, signal: AbortSignal) {
  const waiting = AbortSignal.any([signal, AbortSignal.timeout(90_000)]);
  const path = `/api/playback/${encodeURIComponent(id)}`;
  for (;;) {
    const response = await fetch(path, { signal: waiting, cache: 'no-store' }).catch((error: unknown) => {
      waiting.throwIfAborted();
      if (!(error instanceof TypeError)) throw error;
      return null;
    });
    if (response && !waking(response)) {
      checkLookup(response);
      return;
    }
    await nextPoll(waiting);
  }
}

export async function fetchLookup(id: string, signal: AbortSignal, request: () => Promise<Response>) {
  const response = await request().catch(async (error: unknown) => {
    // Only replay a dropped request if the probe confirms the old job is gone.
    if (error instanceof TypeError && !signal.aborted) await checkPlaybackJob(id, signal);
    throw error;
  });
  if (waking(response)) await checkPlaybackJob(id, signal);
  checkLookup(response);
  return response;
}

async function decodeFrames(result: PlaybackUpdate, id: string, signal: AbortSignal) {
  await Promise.all(result.frames.map(async (frame) => {
    const image = new Image();
    image.src = frame.imageUrl;
    await image.decode();
  })).catch(async () => {
    if (!signal.aborted) await checkPlaybackJob(id, signal);
    throw new Error('Could not load the comparison images. Try again.');
  });
  signal.throwIfAborted();
}

export async function loadSequence(id: string, signal: AbortSignal) {
  const result = await jsonResponse<PlaybackUpdate>(await fetchLookup(id, signal, () =>
    fetch(`/api/playback/${encodeURIComponent(id)}`, { signal })));
  await decodeFrames(result, id, signal);
  return { ...result, jobId: id };
}

export async function stopPlayback(id: string) {
  const response = await fetch(`/api/playback/${encodeURIComponent(id)}`, { method: 'DELETE' });
  if (!response.ok) await jsonResponse(response);
}

async function watchJob<T extends PlaybackUpdate>(
  start: () => Promise<Response>, signal: AbortSignal, update: (result: T) => void, missingEndpoint?: string,
) {
  // Read the job ID even if the visitor cancels before the POST response arrives.
  const { id } = await jsonResponse<{ id: string }>(await start(), missingEndpoint);
  const path = `/api/playback/${encodeURIComponent(id)}`;
  let finished = false;
  try {
    for (;;) {
      const result = await jsonResponse<T>(await fetchLookup(id, signal, () => fetch(path, { signal })));
      if (result.status === 'ready') await decodeFrames(result, id, signal);
      signal.throwIfAborted();
      const latest = { ...result, jobId: id };
      update(latest);
      if (result.status !== 'searching') {
        finished = true;
        return latest;
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
      if (!response || response.status >= 500 || waking(response)) {
        update(previous ? { ...previous, reconnecting: true } : {
          status: 'searching', message: 'Reconnecting to the archive search…', preview: null,
          frames: [], comparison: null, partial: false, sequences: [], reconnecting: true, jobId: id,
        });
      } else {
        const result = await jsonResponse<SequenceSearch>(response);
        signal.throwIfAborted();
        previous = { ...result, jobId: id };
        update(previous);
        if (result.status !== 'searching') return previous;
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
  return watchJob<InventoryUpdate>(() => post('/api/manual/inventory', {
    ...position, fieldSize, release, startDate, endDate, minWavelength, maxWavelength, fromPlayback,
  }), signal, update,
    'This server version cannot list exposures for Manual. Restart or update the API server, then retry.');
}

export function watchSelection(
  id: string, keys: string[], signal: AbortSignal, update: (result: PlaybackUpdate) => void, autoMatch?: PlaybackGap,
) {
  return watchJob(() => fetchLookup(id, signal, () =>
    post(`/api/manual/${encodeURIComponent(id)}/comparison`, { keys, autoMatch })), signal, update);
}

export async function loadBandpasses(id: string, keys: string[], signal: AbortSignal) {
  return jsonResponse<Record<string, Bandpass | null>>(
    await fetchLookup(id, signal, () => post(`/api/manual/${encodeURIComponent(id)}/calibration`, { keys }, signal)),
  );
}

export async function loadExposurePreview(id: string, key: string, signal: AbortSignal) {
  const query = new URLSearchParams({ key });
  const response = await fetchLookup(id, signal, () => fetch(`/api/manual/${encodeURIComponent(id)}/preview?${query}`, { signal }));
  if (!response.ok) {
    await jsonResponse(response, 'Exposure previews are unavailable on this server.');
  }
  return response.blob();
}
