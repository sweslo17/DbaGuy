import createClient, { type Client, type Middleware } from "openapi-fetch";
import { getBackendInfo } from "../backend";
import type { paths } from "./schema";

export class ApiError extends Error {
  constructor(
    message: string,
    readonly code: string,
    readonly status: number,
    readonly detail?: unknown,
  ) {
    super(message);
  }
}

/** The message to show for any thrown value: ApiError messages are already user-facing. */
export function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

const authMiddleware: Middleware = {
  async onRequest({ request }) {
    const { token } = await getBackendInfo();
    request.headers.set("Authorization", `Bearer ${token}`);
    return request;
  },
};

let clientPromise: Promise<Client<paths>> | null = null;
let clientBaseUrl: string | null = null;

/** Returns a typed client bound to the current backend URL. */
export async function api(): Promise<Client<paths>> {
  const { baseUrl } = await getBackendInfo();
  if (!clientPromise || clientBaseUrl !== baseUrl) {
    const client = createClient<paths>({ baseUrl });
    client.use(authMiddleware);
    clientBaseUrl = baseUrl;
    clientPromise = Promise.resolve(client);
  }
  return clientPromise;
}

interface ErrorBody {
  error?: { code?: string; message?: string; detail?: unknown };
}

/** Throws ApiError for non-2xx responses, otherwise returns the data. */
export function unwrap<T>(result: { data?: T; error?: unknown; response: Response }): T {
  if (result.error !== undefined || !result.response.ok) {
    const body = (result.error ?? {}) as ErrorBody;
    throw new ApiError(
      body.error?.message ?? `Request failed (${result.response.status})`,
      body.error?.code ?? "http_error",
      result.response.status,
      body.error?.detail,
    );
  }
  return result.data as T;
}
