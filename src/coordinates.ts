export type CoordinateFields = { ra: string; dec: string };
export type SkyPosition = { ra: number; dec: number };

export const presets: { name: string; fields: CoordinateFields }[] = [
  // Projected ICRS positions at epoch J2026.0 for the 2025–2026 observations.
  { name: 'Barnard’s Star', fields: { ra: '17h57m47.10s', dec: '+04d46m05.5s' } },
  { name: 'Epsilon Indi B', fields: { ra: '22h04m23.18s', dec: '−56d48m03.0s' } },
  { name: 'WISE 0855', fields: { ra: '08h55m02.37s', dec: '−7d14m32.1s' } },
  { name: 'RW Aurigae', fields: { ra: '05h07m49.566s', dec: '+30d24m05.177s' } },
  { name: 'R Draconis', fields: { ra: '16h32m40.226s', dec: '+66d45m17.943s' } },
];

const decimal = /^[+-]?(?:\d+(?:\.\d+)?|\.\d+)\s*°?$/;
const hms = /^(\d{1,2})\s*h\s*(\d{1,2})\s*m\s*(\d{1,2}(?:\.\d+)?)\s*s$/i;
const dms = /^([+-]?\d{1,2})\s*[d°]\s*(\d{1,2})\s*[m′']\s*(\d{1,2}(?:\.\d+)?)\s*[s″"]$/i;
const colon = /^([+-]?\d{1,2})\s*:\s*(\d{1,2})\s*:\s*(\d{1,2}(?:\.\d+)?)$/;
const spaced = /^([+-]?\d{1,2})\s+(\d{1,2})\s+(\d{1,2}(?:\.\d+)?)$/;

export function parseCoordinate(raw: string, axis: keyof SkyPosition): number | null {
  const value = raw.trim().replace(/−/g, '-');
  const isRa = axis === 'ra';

  if (decimal.test(value)) {
    const degrees = Number(value.replace(/°$/, ''));
    const valid = isRa ? degrees >= 0 && degrees < 360 : Math.abs(degrees) <= 90;
    return valid ? degrees : null;
  }

  const parts = (isRa ? hms : dms).exec(value) ?? colon.exec(value) ?? spaced.exec(value);
  if (!parts || (isRa && /^[+-]/.test(parts[1]))) return null;

  const [, first, minuteText, secondText] = parts;
  const minutes = Number(minuteText);
  const seconds = Number(secondText);
  if (minutes >= 60 || seconds >= 60) return null;

  const magnitude = Math.abs(Number(first)) + minutes / 60 + seconds / 3600;
  if (isRa) return magnitude < 24 ? magnitude * 15 : null;
  return magnitude <= 90 ? (first.startsWith('-') ? -magnitude : magnitude) : null;
}

export function parsePosition(fields: CoordinateFields): SkyPosition | null {
  const ra = parseCoordinate(fields.ra, 'ra');
  const dec = parseCoordinate(fields.dec, 'dec');
  return ra === null || dec === null ? null : { ra, dec };
}

export function formatPosition(position: SkyPosition): CoordinateFields {
  const ra = Number(position.ra.toFixed(6));
  return { ra: (ra === 360 ? 0 : ra).toFixed(6), dec: position.dec.toFixed(6) };
}

export function positionInPatch(center: SkyPosition, fieldSize: number, x: number, y: number): SkyPosition {
  // Invert the north-up TAN projection used to render the comparison PNG.
  const radians = Math.PI / 180;
  const east = (.5 - x) * fieldSize * radians / 60;
  const north = (.5 - y) * fieldSize * radians / 60;
  const declination = center.dec * radians;
  const denominator = Math.cos(declination) - north * Math.sin(declination);
  const ra = center.ra + Math.atan2(east, denominator) / radians;
  const dec = Math.atan2(Math.sin(declination) + north * Math.cos(declination), Math.hypot(east, denominator)) / radians;
  return { ra: (ra + 360) % 360, dec };
}
