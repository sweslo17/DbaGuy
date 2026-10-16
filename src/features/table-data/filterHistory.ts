import { pathKey, type PathSegment } from "@/lib/api/types";

/**
 * The last filters applied to one relation, newest first. A convenience kept in `localStorage` (ruling R-02b-1): it
 * may be unavailable or lost, so every access is best effort and never throws.
 */
const MAX_FILTERS = 20;

const storageKey = (profileId: string, path: PathSegment[]) => `dbaguy.filters.${profileId}.${pathKey(path)}`;

export function readFilterHistory(profileId: string, path: PathSegment[]): string[] {
  try {
    const stored: unknown = JSON.parse(localStorage.getItem(storageKey(profileId, path)) ?? "[]");
    return Array.isArray(stored) ? stored.filter((item): item is string => typeof item === "string") : [];
  } catch {
    return [];
  }
}

function writeFilterHistory(profileId: string, path: PathSegment[], filters: string[]): void {
  try {
    if (filters.length) localStorage.setItem(storageKey(profileId, path), JSON.stringify(filters));
    else localStorage.removeItem(storageKey(profileId, path));
  } catch (error) {
    console.warn("Could not save the filter history", error);
  }
}

/** Puts `filter` first (once) and returns the new history. */
export function rememberFilter(profileId: string, path: PathSegment[], filter: string): string[] {
  const others = readFilterHistory(profileId, path).filter((item) => item !== filter);
  const filters = [filter, ...others].slice(0, MAX_FILTERS);
  writeFilterHistory(profileId, path, filters);
  return filters;
}

export function clearFilterHistory(profileId: string, path: PathSegment[]): void {
  writeFilterHistory(profileId, path, []);
}
