// Type surface of the host-provided app SDK (external at build time, injected by Kiro Crew).
declare module '@kirocrew/app-sdk' {
  export interface AppApi {
    raw(path: string, init?: RequestInit): Promise<Response>
    request<T = unknown>(path: string, init?: RequestInit): Promise<T>
    get<T = unknown>(path: string, init?: RequestInit): Promise<T>
    post<T = unknown>(path: string, body?: unknown, init?: RequestInit): Promise<T>
  }
  export function useAppApi(): AppApi
}
