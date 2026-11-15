import { useEffect, useEffectEvent, useRef, useState, type MouseEvent, type PointerEvent, type ReactNode } from 'react';

type Point = { x: number; y: number };
type FlagMap = {
  width: number;
  height: number;
  definitions: Record<number, [name: string, description: string]>;
  samples: DataView;
  warningMask: number;
};
type FlagResponse = Omit<FlagMap, 'samples' | 'warningMask'> & { samples: string };

const sampleBytes = 5;
const amber = [255, 183, 71, 135];
const statusLabels = ['Outside detector coverage', 'Masked in this exposure', '', 'Masked in this comparison'];
const directions: Record<string, Point> = {
  ArrowLeft: { x: -1, y: 0 }, ArrowRight: { x: 1, y: 0 },
  ArrowUp: { x: 0, y: -1 }, ArrowDown: { x: 0, y: 1 },
};

async function readFlags(response: Response): Promise<FlagMap> {
  if (!response.ok) throw new Error('Pixel flags unavailable');
  const { width, height, definitions, samples } = await response.json() as FlagResponse;
  const decoded = atob(samples);
  if (!Number.isInteger(width) || width <= 0 || !Number.isInteger(height) || height <= 0 ||
      decoded.length !== width * height * sampleBytes) throw new Error('Invalid pixel flag map');
  const bytes = new Uint8Array(decoded.length);
  for (let index = 0; index < decoded.length; index++) bytes[index] = decoded.charCodeAt(index);
  let warningMask = -1;
  for (const [bit, [name]] of Object.entries(definitions)) {
    if (name === 'SOURCE' || name === 'FULLSAMPLE') warningMask &= ~(1 << Number(bit));
  }
  return { width, height, definitions, samples: new DataView(bytes.buffer), warningMask };
}

function clamp(value: number, size: number) {
  return Math.max(0, Math.min(size - 1, value));
}

function imagePoint(event: MouseEvent<HTMLElement> | PointerEvent<HTMLElement>): Point {
  const box = event.currentTarget.getBoundingClientRect();
  return { x: (event.clientX - box.left) / box.width, y: (event.clientY - box.top) / box.height };
}

function pixelAt(point: Point, map: FlagMap): Point {
  return {
    x: clamp(Math.floor(point.x * map.width), map.width),
    y: clamp(Math.floor(point.y * map.height), map.height),
  };
}

type Props = { url?: string; sourceSelection?: Point | null; children: ReactNode;
  request: (signal: AbortSignal) => Promise<Response> };

export function PixelFlags({ url, sourceSelection, children, request }: Props) {
  const [loaded, setLoaded] = useState<{ url: string; map: FlagMap | null }>();
  const [attempt, setAttempt] = useState(0);
  const [hovered, setHovered] = useState<Point | null>(null);
  const [pinned, setPinned] = useState<Point | null>(null);
  const canvas = useRef<HTMLCanvasElement>(null);
  const readoutPanel = useRef<HTMLDivElement>(null);
  const current = url && loaded?.url === url ? loaded : undefined;
  const map = current?.map;
  const point = hovered ?? sourceSelection ?? pinned;
  const selected = point && map && pixelAt(point, map);
  const load = useEffectEvent(request);

  useEffect(() => {
    if (!url) return;
    const controller = new AbortController();
    void load(controller.signal).then(readFlags).then((map) => {
      if (!controller.signal.aborted) setLoaded({ url, map });
    }).catch(() => {
      if (!controller.signal.aborted) setLoaded({ url, map: null });
    });
    return () => controller.abort();
  }, [url, attempt]);

  useEffect(() => {
    if (!map) return;
    const context = canvas.current?.getContext('2d');
    if (!context) return;
    const overlay = context.createImageData(map.width, map.height);
    for (let index = 0; index < map.width * map.height; index++) {
      if (!(map.samples.getUint32(index * sampleBytes, true) & map.warningMask)) continue;
      overlay.data.set(amber, index * 4);
    }
    context.putImageData(overlay, 0, 0);
  }, [map]);

  useEffect(() => {
    const panel = readoutPanel.current;
    if (!url || !panel?.firstElementChild) return;
    const observer = new ResizeObserver(() => {
      panel.toggleAttribute('data-scrollable', panel.scrollHeight > panel.clientHeight);
    });
    observer.observe(panel);
    observer.observe(panel.firstElementChild);
    return () => observer.disconnect();
  }, [url]);

  useEffect(() => {
    if (readoutPanel.current) readoutPanel.current.scrollTop = 0;
  }, [url, selected?.x, selected?.y]);

  let readout: ReactNode = <p className="lookup-status">{sourceSelection === undefined
    ? 'Hover for details. Click or tap to pin a pixel.'
    : 'Hover for details or select a source.'}</p>;
  let marker = null;
  if (!map) {
    readout = current ? <>
      <p>Could not load pixel flags.</p>
      <button className="control" type="button" onClick={() => {
        setLoaded(undefined);
        setAttempt((value) => value + 1);
      }}>Retry flags</button>
    </> : <p>Loading pixel flags…</p>;
  } else if (selected) {
    const { x, y } = selected;
    const offset = (y * map.width + x) * sampleBytes;
    const flags = map.samples.getUint32(offset, true);
    const status = map.samples.getUint8(offset + 4);
    const active = [];
    for (let bit = 0; bit < 32; bit++) {
      if (flags & (1 << bit)) active.push({ bit, definition: map.definitions[bit] ?? [`Flag bit ${bit}`, ''] });
    }
    marker = <span className="pixel-flag-marker" aria-hidden="true"
      style={{ left: `${(x + .5) / map.width * 100}%`, top: `${(y + .5) / map.height * 100}%` }} />;
    readout = <>
      <p className="pixel-flag-location">Pixel {x + 1}, {y + 1}{!hovered && (sourceSelection ? ' · Source' : ' · Pinned')}</p>
      {statusLabels[status] && <p>{statusLabels[status]}</p>}
      {active.length > 0 ? <ul>{active.map(({ bit, definition: [name, description] }) => (
        <li key={bit}><strong>{name}</strong>{description && <span>{description}</span>}</li>
      ))}</ul> : status !== 0 && <p>No flags set</p>}
    </>;
  }

  return (
    <div className="exposure-image"
      onPointerMove={url ? (event) => {
        if (event.pointerType !== 'touch') setHovered(imagePoint(event));
      } : undefined}
      onPointerLeave={() => setHovered(null)}
      onPointerCancel={() => setHovered(null)}
      onClickCapture={url && sourceSelection !== undefined ? (event) => {
        if (!readoutPanel.current?.contains(event.target as Node)) setHovered(null);
      } : undefined}
      onKeyDownCapture={url && sourceSelection !== undefined ? (event) => {
        if (directions[event.key] && !readoutPanel.current?.contains(event.target as Node)) setHovered(null);
      } : undefined}>
      {children}
      {map && <canvas className="pixel-flag-overlay" ref={canvas} width={map.width} height={map.height} aria-hidden="true" />}
      {url && sourceSelection === undefined && <button className="pixel-flag-target" type="button"
        aria-label="Inspect pixel flags" aria-description="Use arrow keys to move between pixels."
        onClick={(event) => {
          setPinned(event.detail === 0 ? pinned ?? { x: .5, y: .5 } : imagePoint(event));
          setHovered(null);
        }}
        onKeyDown={(event) => {
          if (event.key === 'Escape') {
            setPinned(null);
            setHovered(null);
            return;
          }
          const direction = directions[event.key];
          if (!map || !direction) return;
          event.preventDefault();
          const pixel = selected ?? pixelAt({ x: .5, y: .5 }, map);
          const x = clamp(pixel.x + direction.x, map.width);
          const y = clamp(pixel.y + direction.y, map.height);
          setPinned({ x: (x + .5) / map.width, y: (y + .5) / map.height });
          setHovered(null);
        }} />}
      {marker}
      {url && <div id="pixel-flag-readout" className="pixel-flag-readout" ref={readoutPanel}
        data-side={map && point && point.y >= .5 ? 'top' : 'bottom'} data-selected={selected ? '' : undefined}
        aria-live={hovered ? 'off' : 'polite'}
        onPointerMove={(event) => event.stopPropagation()}
        tabIndex={selected ? 0 : undefined}>
        <div className="pixel-flag-content">
          {!selected && <p className="pixel-flags-legend"><span aria-hidden="true" /> Amber marks warnings</p>}
          {readout}
        </div>
      </div>}
    </div>
  );
}
