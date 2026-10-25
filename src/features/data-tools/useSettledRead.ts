import { keepPreviousData, useQuery, type UseQueryResult } from "@tanstack/react-query";
import { useMemo } from "react";
import { useDebouncedValue } from "@/lib/useDebouncedValue";

/**
 * A dialog's preview is read again once the choices have stayed the same this long: a run of clicks or a path being
 * typed is one catalog read on the metadata connections, not one per change.
 */
export const PLAN_DEBOUNCE_MS = 400;

export interface SettledRead<Req, Res> {
  /** What the choices ask for now; null while no read can be asked (something is missing). */
  request: Req | null;
  read: UseQueryResult<Res, Error>;
  /** The answer on show: none while no read can be asked or the last one failed. It may be of choices before these. */
  shown: Res | null;
  /** The answer on show is for other choices than these: they are settling, or being read. Dim it; never act on it. */
  behind: boolean;
  /** The answer for exactly these choices, or null: the only one an action (Dump, Copy) may take. */
  current: Res | null;
}

/**
 * The preview read both data-tools forms make (the dump's plan, the copy's preview): `requestKey` is the request's JSON
 * (null: none can be asked), made by the caller in a `useMemo` over what it reads, so a form change the request does not
 * contain (a path still being typed) keeps the same key and nothing dims or waits for it. The request is asked once it
 * has settled; the answer of the choices before stays, dimmed, while the next is read, and is never `current`.
 */
export function useSettledRead<Req extends object, Res>(
  name: string,
  requestKey: string | null,
  read: (body: Req, signal: AbortSignal) => Promise<Res>,
): SettledRead<Req, Res> {
  const request = useMemo(() => (requestKey === null ? null : (JSON.parse(requestKey) as Req)), [requestKey]);
  const body = useDebouncedValue(request, PLAN_DEBOUNCE_MS);
  const query = useQuery({
    queryKey: [name, body],
    queryFn: ({ signal }) => read(body!, signal),
    enabled: body !== null,
    retry: false,
    gcTime: 0,
    // The answer of the choices before stays (dimmed) while the new one is read: the dialog does not jump at each click
    placeholderData: keepPreviousData,
  });
  // None while no read can be asked: a disabled query still hands back the previous answer as its placeholder
  const shown = request !== null && !query.isError ? (query.data ?? null) : null;
  const behind = body !== request || query.isPlaceholderData;
  return { request, read: query, shown, behind, current: behind ? null : shown };
}
