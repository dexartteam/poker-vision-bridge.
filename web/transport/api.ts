import type { Profile } from '../core/profile';
export class VisionAPI {
  private id = '';
  private token = '';
  private async request(path: string, method = 'GET', body?: unknown, auth?: string): Promise<any> {
    const response = await fetch(`/v1/vision${path}`, {
      method,
      headers: {
        'Content-Type': 'application/json',
        ...(auth ? { Authorization: `Bearer ${auth}` } : {}),
        ...(this.token ? { 'X-Vision-Session': this.token } : {}),
      },
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: AbortSignal.timeout(20000),
    });
    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      throw new Error(typeof data.detail === 'string' ? data.detail : `HTTP ${response.status}`);
    }
    return response.json();
  }
  status() {
    return this.request('/status');
  }
  async start(auth: string, profile: Profile, epoch: string) {
    const result = await this.request('/sessions', 'POST', { profile, capture_epoch: epoch }, auth);
    this.id = result.session_id;
    this.token = result.session_token;
  }
  reset(profile: Profile, epoch: string) {
    return this.request(`/sessions/${this.id}`, 'PUT', { profile, capture_epoch: epoch });
  }
  change(event: unknown) {
    return this.request(`/sessions/${this.id}/changes`, 'POST', event);
  }
  frame(frame: unknown) {
    return this.request(`/sessions/${this.id}/frames`, 'POST', frame);
  }
  state() {
    return this.request(`/sessions/${this.id}`);
  }
  recording() {
    if (!this.id) return Promise.reject(new Error('no_session'));
    return this.request(`/sessions/${this.id}/recording`);
  }
  async close() {
    if (!this.id) return;
    try {
      await this.request(`/sessions/${this.id}`, 'DELETE');
    } finally {
      this.id = '';
      this.token = '';
    }
  }
}
