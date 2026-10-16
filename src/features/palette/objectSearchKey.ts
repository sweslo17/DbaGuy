/** Quick open's object searches of a connection, by search text (without `q`: all of them, e.g. on Refresh schema). */
export const objectSearchKey = (profileId: string | null, q?: string) =>
  q === undefined ? ["object-search", profileId] : ["object-search", profileId, q];
