import { parseCoordinate, type CoordinateFields, type SkyPosition } from './coordinates';
import { archiveReleases, type ArchiveRelease, type PlaybackPatch } from './exposure';

export const fieldSizes = [3, 6, 12, 24, 48] as const;
export const defaultFieldSize = 6;
export const emptyFields: CoordinateFields = { ra: '', dec: '' };
export const emptyFilters = { startDate: '', endDate: '', minWavelength: '', maxWavelength: '', minGapDays: '', maxGapDays: '' };

type FieldSize = typeof fieldSizes[number];
type FilterFields = typeof emptyFilters;
export type FilterField = keyof FilterFields;
export type PatchDraft = {
  preset: string;
  fields: CoordinateFields;
  fieldSize: FieldSize;
  release: ArchiveRelease | null;
  filters: FilterFields;
};
export type SubmittedPatch = PlaybackPatch & { coordinates: CoordinateFields; fieldSize: FieldSize };
export const emptyDraft: PatchDraft = {
  preset: '', fields: emptyFields, fieldSize: defaultFieldSize, release: null, filters: emptyFilters,
};

const dateFields = ['startDate', 'endDate'] as const;
const numericFields = ['minWavelength', 'maxWavelength', 'minGapDays', 'maxGapDays'] as const;
const queryKeys = ['ra', 'dec', 'fieldSize', 'release', ...dateFields, ...numericFields] as const;
type QueryKey = typeof queryKeys[number];
const decimal = /^[+-]?(?:[0-9]+(?:\.[0-9]+)?|\.[0-9]+)$/;
const isDecimal = (value: string) => decimal.test(value) && Number.isFinite(Number(value));

export function submittedPatch(draft: PatchDraft, position: SkyPosition, manual = false): SubmittedPatch {
  const { startDate, endDate, minWavelength, maxWavelength, minGapDays, maxGapDays } = draft.filters;
  return {
    position,
    coordinates: { ra: draft.fields.ra.trim(), dec: draft.fields.dec.trim() },
    fieldSize: draft.fieldSize,
    release: draft.release,
    startDate: startDate || null,
    endDate: endDate || null,
    minWavelength: minWavelength ? Number(minWavelength) : null,
    maxWavelength: maxWavelength ? Number(maxWavelength) : null,
    minGapDays: manual ? 0 : Number(minGapDays),
    maxGapDays: !manual && maxGapDays ? Number(maxGapDays) : null,
  };
}

function validDate(value: string) {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value) || value.startsWith('0000-')) return false;
  const date = new Date(`${value}T00:00:00Z`);
  return Number.isFinite(date.getTime()) && date.toISOString().slice(0, 10) === value;
}

export function filterProblem(patch: SubmittedPatch): { field: FilterField; message: string } | undefined {
  const { startDate, endDate, minWavelength, maxWavelength, minGapDays, maxGapDays } = patch;
  if (startDate && !validDate(startDate)) return { field: 'startDate', message: 'Enter a valid start date.' };
  if (endDate && !validDate(endDate)) return { field: 'endDate', message: 'Enter a valid end date.' };
  if (startDate && endDate && startDate > endDate)
    return { field: 'endDate', message: 'Enter an end date on or after the start date.' };
  if (minWavelength !== null && (!Number.isFinite(minWavelength) || minWavelength <= 0))
    return { field: 'minWavelength', message: 'Enter a wavelength greater than zero.' };
  if (maxWavelength !== null && (!Number.isFinite(maxWavelength) || maxWavelength <= 0 || maxWavelength < (minWavelength ?? 0)))
    return { field: 'maxWavelength', message: 'Enter a positive maximum wavelength at least as large as the minimum.' };
  if (!Number.isFinite(minGapDays) || minGapDays < 0)
    return { field: 'minGapDays', message: 'Enter a minimum time gap of zero days or more.' };
  if (maxGapDays !== null && (!Number.isFinite(maxGapDays) || maxGapDays < minGapDays))
    return { field: 'maxGapDays', message: 'Enter a maximum time gap at least as large as the minimum.' };
}

type PatchLocation =
  | { kind: 'picker' }
  | { kind: 'invalid'; draft: PatchDraft; message: string }
  | { kind: 'query'; draft: PatchDraft; patch: SubmittedPatch };

export function readPatchQuery(query: URLSearchParams, required = false): PatchLocation {
  if (!required && !queryKeys.some((key) => query.has(key))) return { kind: 'picker' };
  const problems: string[] = [];
  const draft = { ...emptyDraft, fields: { ...emptyFields }, filters: { ...emptyFilters } };

  function read(key: QueryKey, accepts: (value: string) => boolean, rule: string, keepInvalid = false) {
    const values = query.getAll(key);
    if (!values.length && key !== 'ra' && key !== 'dec') return '';
    if (values.length > 1) problems.push(`Use only one ${key} parameter.`);
    else if (values.length !== 1 || !accepts(values[0])) problems.push(rule);
    else return values[0];
    return keepInvalid && values.length === 1 ? values[0] : '';
  }

  draft.fields.ra = read('ra', (value) => isDecimal(value) && Number(value) >= 0 && Number(value) < 360,
    'Set ra to a decimal angle from 0° to less than 360°.', true);
  draft.fields.dec = read('dec', (value) => isDecimal(value) && Math.abs(Number(value)) <= 90,
    'Set dec to a decimal angle from −90° to +90°.', true);
  const size = read('fieldSize', (value) => fieldSizes.some((size) => String(size) === value),
    'Set fieldSize to 3, 6, 12, 24, or 48 arcminutes.');
  draft.fieldSize = fieldSizes.find((item) => String(item) === size) ?? defaultFieldSize;
  const release = read('release', (value) => archiveReleases.some((item) => item === value), 'Set release to dr1 or qr3.');
  draft.release = archiveReleases.find((item) => item === release) ?? null;
  for (const key of dateFields) draft.filters[key] = read(key, validDate, `Set ${key} to a valid date in YYYY-MM-DD.`);
  for (const key of numericFields) draft.filters[key] = read(key, isDecimal, `Set ${key} to a decimal number.`);
  const patch = submittedPatch(draft, { ra: Number(draft.fields.ra), dec: Number(draft.fields.dec) });
  const filter = filterProblem(patch);
  if (filter) problems.push(`${filter.field}: ${filter.message}`);
  if (problems.length) return { kind: 'invalid', draft, message: problems.join(' ') };
  return { kind: 'query', draft, patch };
}

function decimalString(value: number) {
  const [mantissa, exponent] = String(value).split('e');
  if (!exponent) return mantissa;
  const sign = mantissa.startsWith('-') ? '-' : '';
  const [whole, fractional = ''] = mantissa.slice(sign.length).split('.');
  const digits = whole + fractional;
  const point = whole.length + Number(exponent);
  if (point <= 0) return `${sign}0.${'0'.repeat(-point)}${digits}`;
  if (point >= digits.length) return sign + digits + '0'.repeat(point - digits.length);
  return `${sign}${digits.slice(0, point)}.${digits.slice(point)}`;
}

export function patchQuery(patch: SubmittedPatch) {
  const query = new URLSearchParams({
    ra: decimalString(patch.position.ra), dec: decimalString(patch.position.dec), fieldSize: String(patch.fieldSize),
  });
  for (const key of ['release', ...dateFields] as const) {
    if (patch[key]) query.set(key, patch[key]);
  }
  for (const key of numericFields) {
    const value = patch[key];
    if (value !== null && (key !== 'minGapDays' || value !== 0)) query.set(key, decimalString(value));
  }
  return query;
}

export function draftQuery(draft: PatchDraft) {
  const query = new URLSearchParams();
  for (const axis of ['ra', 'dec'] as const) {
    const raw = draft.fields[axis].trim();
    if (!raw) continue;
    const degrees = parseCoordinate(raw, axis);
    query.set(axis, degrees === null ? raw : decimalString(degrees));
  }
  if (draft.fieldSize !== defaultFieldSize || query.size) query.set('fieldSize', String(draft.fieldSize));
  if (draft.release) query.set('release', draft.release);
  for (const key of dateFields) {
    if (draft.filters[key]) query.set(key, draft.filters[key]);
  }
  for (const key of numericFields) {
    const raw = draft.filters[key];
    if (!raw) continue;
    const value = Number(raw);
    query.set(key, Number.isFinite(value) ? decimalString(value) : raw);
  }
  return query;
}

export function clearPatchQuery(url: URL) {
  for (const key of queryKeys) url.searchParams.delete(key);
}
