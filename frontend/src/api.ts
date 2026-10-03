export function apiUrl(path: string): string {
  if (!import.meta.env.DEV) return path
  return `${window.location.protocol}//${window.location.hostname}:9021${path}`
}

export async function readApiJson<T>(response: Response): Promise<T> {
  const contentType = response.headers.get('content-type') ?? ''
  if (!response.ok) {
    const detail = contentType.includes('application/json')
      ? JSON.stringify(await response.json())
      : (await response.text()).slice(0, 180)
    throw new Error(`API ${response.status}: ${detail}`)
  }
  if (!contentType.includes('application/json')) {
    throw new Error('The API returned a web page instead of JSON. Confirm the backend is running on port 9021, then refresh.')
  }
  return response.json() as Promise<T>
}
