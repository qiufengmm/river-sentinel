/** 项目统一时区：飞云江断面采用 Asia/Shanghai，避免浏览器本地时区隐式漂移。 */
export const PROJECT_TIME_ZONE = 'Asia/Shanghai';

function pad(value: number): string {
  return value < 10 ? `0${value}` : String(value);
}

/**
 * 计算某个“墙上时间”在指定时区的偏移量（分钟）。
 *
 * 将 Date 的 UTC 分量视作目标时区的墙上时间，与目标时区实际时刻求差得到偏移。
 */
export function zoneOffsetMinutes(wall: Date, timeZone: string = PROJECT_TIME_ZONE): number {
  const formatter = new Intl.DateTimeFormat('en-US', {
    timeZone,
    hour12: false,
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  });
  const parts = formatter.formatToParts(wall);
  const read = (type: string): number => {
    const found = parts.find((part) => part.type === type);
    const value = Number(found?.value ?? '0');
    return type === 'hour' && value === 24 ? 0 : value;
  };
  const asUtc = Date.UTC(
    read('year'),
    read('month') - 1,
    read('day'),
    read('hour'),
    read('minute'),
    read('second'),
  );
  return Math.round((asUtc - wall.getTime()) / 60000);
}

function offsetLabel(minutes: number): string {
  const sign = minutes < 0 ? '-' : '+';
  const absolute = Math.abs(minutes);
  return `${sign}${pad(Math.floor(absolute / 60))}:${pad(absolute % 60)}`;
}

/**
 * 把 `datetime-local` 产生的本地时间（YYYY-MM-DDTHH:mm 或带秒）转换为带项目时区偏移的 ISO 字符串。
 *
 * 例：`2026-01-01T11:00` → `2026-01-01T11:00:00+08:00`。无法解析时返回 null，由调用方降级。
 */
export function toIsoWithTimeZone(
  localInput: string | null | undefined,
  timeZone: string = PROJECT_TIME_ZONE,
): string | null {
  const trimmed = (localInput ?? '').trim();
  if (!trimmed) return null;
  // 已带时区偏移或 Z 的 ISO 串直接透传，不再二次转换。
  if (/(?:Z|[+-]\d{2}:?\d{2})$/.test(trimmed)) return trimmed;
  const match = /^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2}))?$/.exec(trimmed);
  if (!match) return null;
  const [, year, month, day, hour, minute, second] = match;
  const wall = new Date(
    Date.UTC(Number(year), Number(month) - 1, Number(day), Number(hour), Number(minute), Number(second ?? '0')),
  );
  if (Number.isNaN(wall.getTime())) return null;
  const offset = zoneOffsetMinutes(wall, timeZone);
  const base = `${year}-${month}-${day}T${hour}:${minute}:${pad(Number(second ?? '0'))}`;
  return `${base}${offsetLabel(offset)}`;
}

/** 生成 `datetime-local` 输入框的默认值（按项目时区显示，避免本地时区漂移）。 */
export function toLocalInput(value: Date | string | null, timeZone: string = PROJECT_TIME_ZONE): string {
  const date = value instanceof Date ? value : value ? new Date(value) : null;
  if (!date || Number.isNaN(date.getTime())) return '';
  const offset = zoneOffsetMinutes(date, timeZone);
  const shifted = new Date(date.getTime() + offset * 60000);
  return `${shifted.getUTCFullYear()}-${pad(shifted.getUTCMonth() + 1)}-${pad(shifted.getUTCDate())}T${pad(
    shifted.getUTCHours(),
  )}:${pad(shifted.getUTCMinutes())}`;
}
