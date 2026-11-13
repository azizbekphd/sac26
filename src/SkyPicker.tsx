import { useEffect, useRef, useState } from 'react';
import type { SkyPosition } from './coordinates';

const survey = 'https://irsa.ipac.caltech.edu/data/hips/CDS/DSS2/color';

function patchCorners({ ra, dec }: SkyPosition, fieldSize: number): [number, number][] {
  const radians = Math.PI / 180;
  const halfWidth = fieldSize * radians / 120;
  const sinDec = Math.sin(dec * radians);
  const cosDec = Math.cos(dec * radians);
  // Convert the corners of a north-up tangent-plane square to sky coordinates.
  return [[-1, -1], [1, -1], [1, 1], [-1, 1]].map(([east, north]) => {
    const x = east * halfWidth;
    const y = north * halfWidth;
    const denominator = cosDec - y * sinDec;
    const longitude = ra + Math.atan2(x, denominator) / radians;
    const latitude = Math.atan2(sinDec + y * cosDec, Math.hypot(denominator, x)) / radians;
    return [(longitude + 360) % 360, latitude];
  });
}

export function SkyPicker({ active, presetPosition, draftPosition, fieldSize, onPick }: {
  active: boolean;
  presetPosition: SkyPosition | null;
  draftPosition: SkyPosition | null;
  fieldSize: number;
  onPick: (position: SkyPosition) => void;
}) {
  const hostRef = useRef<HTMLDivElement>(null);
  const atlasRef = useRef<ReturnType<typeof import('aladin-lite').default.aladin> | null>(null);
  const markRef = useRef<((position: SkyPosition | null, size: number) => void) | null>(null);
  const [status, setStatus] = useState<'loading' | 'ready' | 'unavailable'>('loading');

  useEffect(() => {
    const atlas = atlasRef.current;
    if (status !== 'ready' || !atlas || !presetPosition) return;
    atlas.gotoRaDec(presetPosition.ra, presetPosition.dec);
    atlas.setFoV(20);
  }, [presetPosition, status]);

  useEffect(() => {
    if (status === 'ready') markRef.current?.(draftPosition, fieldSize);
  }, [draftPosition, fieldSize, status]);

  useEffect(() => {
    const host = hostRef.current;
    if (!host) return;

    const controller = new AbortController();
    setStatus('loading');

    async function openAtlas(element: HTMLDivElement) {
      try {
        const [A] = await Promise.all([
          import('aladin-lite').then(async ({ default: library }) => {
            await library.init;
            return library;
          }),
          // Aladin can render an empty viewport when the HiPS service is blocked.
          // Check an actual overview image so the picker can explain this failure.
          fetch(`${survey}/Norder3/Allsky.jpg`, {
            method: 'HEAD',
            signal: AbortSignal.any([controller.signal, AbortSignal.timeout(12_000)]),
          }).then((response) => {
            if (!response.ok || !response.headers.get('content-type')?.startsWith('image/')) {
              throw new Error('DSS2 survey imagery is unavailable');
            }
          }),
        ]);
        if (controller.signal.aborted) return;

        const overviewFov = matchMedia('(max-width: 760px)').matches ? 42 : 95;
        const atlas = A.aladin(element, {
          survey,
          target: '85.30251 -2.25468',
          fov: overviewFov,
          cooFrame: 'ICRSd',
          mode: 'dark',
          showReticle: false,
          showLayersControl: false,
          showSettingsControl: false,
          showFullscreenControl: false,
          showShareControl: false,
          showSimbadPointerControl: false,
          showCooGridControl: false,
          showCooLocation: false,
          showFrame: false,
          showFov: false,
          showProjectionControl: false,
          showStatusBar: false,
        });
        atlasRef.current = atlas;

        const marker = A.catalog({
          name: 'Selected patch',
          color: '#e8e8e8',
          hoverColor: '#ffffff',
          selectionColor: '#ffffff',
          sourceSize: 20,
          shape: 'circle',
        });
        atlas.addCatalog(marker);
        const outline = A.graphicOverlay({ name: 'Patch footprint', color: '#f3f3f3', lineWidth: 1 });
        atlas.addOverlay(outline);
        markRef.current = (position, size) => {
          marker.removeAll();
          outline.removeAll();
          if (position) {
            marker.addSources([A.source(position.ra, position.dec)]);
            outline.add(A.polygon(patchCorners(position, size)));
          }
          outline.reportChange();
        };

        atlas.on('click', ({ ra, dec, isDragging }) => {
          if (isDragging || !Number.isFinite(ra) || !Number.isFinite(dec)) return;
          const position = { ra: ((ra % 360) + 360) % 360, dec };
          onPick(position);
        });
        setStatus('ready');
      } catch {
        if (!controller.signal.aborted) setStatus('unavailable');
      }
    }

    void openAtlas(host);
    return () => {
      controller.abort();
      atlasRef.current?.off('click');
    };
  }, [onPick]);

  return (
    <section className="sky-picker" data-active={active} aria-label="Sky map" inert={!active}>
      <div className="map-host" ref={hostRef} />
      <div className="map-info">
        {status === 'ready' ? (
          <>
            <span>Pick a point on the sky</span>
            <a href="https://alasky.cds.unistra.fr/MocServer/query?ID=CDS%2FP%2FDSS2%2Fcolor&fmt=html&get=record" target="_blank" rel="noreferrer">
              DSS2 color · STScI/NASA · CDS
            </a>
          </>
        ) : (
          <span role="status">
            {status === 'loading' ? 'Loading sky map…' : 'Sky map unavailable. Use a preset or enter coordinates.'}
          </span>
        )}
      </div>
    </section>
  );
}
