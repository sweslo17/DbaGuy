/*
 * Script paths (spec §4.7): relative to the scripts folder and "/"-separated, as the backend lists them; "" is the
 * scripts folder itself. The backend checks everything else (dots, "..", the `.sql` suffix, conflicts).
 */

const SQL_SUFFIX = ".sql";

/** The last name in the path: `reports/monthly.sql` → `monthly.sql`. */
export function baseName(path: string): string {
  return path.slice(path.lastIndexOf("/") + 1);
}

/** The folder the path is in ("" for the scripts folder). */
export function parentPath(path: string): string {
  const slash = path.lastIndexOf("/");
  return slash < 0 ? "" : path.slice(0, slash);
}

export function joinPath(folder: string, name: string): string {
  return folder ? `${folder}/${name}` : name;
}

/** `path` is `ancestor` itself or lies below it. */
export function isWithin(path: string, ancestor: string): boolean {
  return path === ancestor || path.startsWith(`${ancestor}/`);
}

/** Where `path` (within `from`) is once `from` has moved to `to`. */
export function movedPath(path: string, from: string, to: string): string {
  return `${to}${path.slice(from.length)}`;
}

/** A file name with `.sql` appended when it has no such suffix (in any case). */
export function withSqlSuffix(name: string): string {
  return name.toLowerCase().endsWith(SQL_SUFFIX) ? name : `${name}${SQL_SUFFIX}`;
}

/** The part of a file name before `.sql`: what a rename selects. */
export function stemLength(name: string): number {
  return name.toLowerCase().endsWith(SQL_SUFFIX) ? name.length - SQL_SUFFIX.length : name.length;
}

/** Why a typed (trimmed) name cannot be used, or null: only what the backend would not explain as well. */
export function nameProblem(name: string): string | null {
  if (!name) return "Enter a name";
  if (name.includes("/")) return "A name cannot contain “/”";
  return null;
}
