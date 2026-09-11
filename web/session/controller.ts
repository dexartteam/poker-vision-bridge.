import { DEFAULTS, type Detection, type Pixels, type Reason } from '../core/detector';
import type { Profile } from '../core/profile';
import { VisionAPI } from '../transport/api';
import { VideoFreshness } from '../capture/freshness';

type Callbacks = {
  detection: (d: Detection) => void;
  state: (s: any) => void;
  error: (message: string) => void;
  invalidated: () => void;
  stopped?: () => void;
  recorded?: (recording: unknown) => void;
};
type Captured = {
  pixels: Pixels;
  source: {
    capture_epoch: string;
    calibration_id: string;
    frame_id: string;
    frame_seq: number;
    visual_revision: number;
    captured_at: number;
  };
  image: string;
  stable: boolean;
  reason: Reason;
};

export class SessionController {
  private worker: Worker;
  private api = new VisionAPI();
  private epoch = crypto.randomUUID();
  private sequence = 0;
  private revision = 0;
  private stopped = false;
  private busy = false;
  private sending = false;
  private changing = false;
  private nextChangeAt = 0;
  private nextUploadAt = 0;
  private freshness: VideoFreshness | null = null;
  private polling = false;
  private timer = 0;
  private pollTimer = 0;
  private forced: Reason | null = null;
  private queued: Captured | null = null;
  private changeRevision: number | null = null;
  private frames = new Map<string, Captured>();
  private outcomes = new Set<string>();
  private lastSent = -Infinity;
  private failures = 0;
  private retryAt = Infinity;
  private full = document.createElement('canvas');
  private small = document.createElement('canvas');
  private capture: { pixels: Pixels; at: number; seq: number } | null = null;

  constructor(
    private source: () => CanvasImageSource,
    private profile: Profile,
    private callbacks: Callbacks,
    diagnostic: boolean,
  ) {
    this.full.width = profile.width;
    this.full.height = profile.height;
    this.small.width = Math.min(640, profile.width);
    this.small.height = Math.round((profile.height * this.small.width) / profile.width);
    this.worker = new Worker(new URL('../detection/worker.ts', import.meta.url), {
      type: 'module',
    });
    this.worker.postMessage({
      type: 'init',
      regions: profile.regions,
      config: { ...DEFAULTS, safetyPollMs: diagnostic ? 2000 : 15000 },
    });
    this.worker.onmessage = (e) => {
      void this.result(e.data);
    };
    this.worker.onerror = () => {
      this.callbacks.error('Ошибка Worker. Перезапустите наблюдение.');
      void this.stop();
    };
  }
  async start(token: string) {
    await this.api.start(token, this.profile, this.epoch);
    if (this.stopped) {
      await this.api.close();
      return;
    }
    const input = this.source();
    if (input instanceof HTMLVideoElement) this.freshness = new VideoFreshness(input);
    this.timer = window.setInterval(() => this.tick(), 200);
    this.pollTimer = window.setInterval(() => void this.poll(), 500);
    this.tick();
  }
  async stop() {
    if (this.stopped) return;
    this.stopped = true;
    clearInterval(this.timer);
    clearInterval(this.pollTimer);
    this.worker.terminate();
    this.frames.clear();
    this.queued = null;
    this.freshness?.close();
    this.callbacks.invalidated();
    this.callbacks.stopped?.();
    try {
      this.callbacks.recorded?.(await this.api.recording());
    } catch {
      /* Offline sessions cannot be exported. */
    }
    await this.api.close().catch(() => {});
  }
  force() {
    this.forced = 'manual';
  }
  recording() {
    return this.api.recording();
  }

  private tick() {
    if (this.stopped || this.busy || document.hidden) return;
    try {
      const at = this.freshness ? this.freshness.take(Date.now()) : Date.now();
      if (at === null) return;
      this.full.getContext('2d')!.drawImage(this.source(), 0, 0, this.full.width, this.full.height);
      const ctx = this.small.getContext('2d', { willReadFrequently: true })!;
      ctx.drawImage(this.full, 0, 0, this.small.width, this.small.height);
      const pixels = {
        data: ctx.getImageData(0, 0, this.small.width, this.small.height).data,
        width: this.small.width,
        height: this.small.height,
      };
      this.capture = { pixels, at, seq: ++this.sequence };
      if (Date.now() >= this.retryAt && !this.forced) {
        this.forced = 'retry';
        this.retryAt = Infinity;
      }
      this.busy = true;
      this.worker.postMessage({
        type: 'frame',
        pixels,
        seq: this.sequence,
        now: performance.now(),
        force: this.forced,
      });
    } catch (error) {
      this.callbacks.error(error instanceof Error ? error.message : 'Не удалось получить кадр.');
      void this.stop();
    }
  }
  private async result(message: any) {
    if (this.stopped) return;
    if (message.type === 'error') {
      this.busy = false;
      this.callbacks.error(message.message);
      await this.stop();
      return;
    }
    if (message.type !== 'result' || !this.capture || message.seq !== this.capture.seq) return;
    const d = message.result as Detection;
    this.revision = d.revision;
    this.callbacks.detection(d);
    if (d.changed) {
      this.callbacks.invalidated();
      this.changeRevision = d.revision;
      if (this.queued && this.queued.source.visual_revision < d.revision) this.queued = null;
    }
    if (d.candidate) {
      this.forced = null;
      this.queued = {
        pixels: this.capture.pixels,
        image: this.full.toDataURL('image/jpeg', 0.9),
        stable: d.stable,
        reason: d.candidate,
        source: {
          capture_epoch: this.epoch,
          calibration_id: this.profile.calibration_id,
          frame_id: crypto.randomUUID(),
          frame_seq: this.capture.seq,
          visual_revision: d.revision,
          captured_at: this.capture.at,
        },
      };
    }
    this.busy = false;
    void this.sendChange();
    void this.pump();
  }
  private async sendChange() {
    if (
      this.changing ||
      this.stopped ||
      this.changeRevision === null ||
      Date.now() < this.nextChangeAt
    )
      return;
    this.changing = true;
    try {
      while (!this.stopped && this.changeRevision !== null) {
        const revision: number = this.changeRevision;
        await this.api.change({
          capture_epoch: this.epoch,
          calibration_id: this.profile.calibration_id,
          visual_revision: revision,
        });
        if (this.changeRevision === revision) this.changeRevision = null;
      }
    } catch (error) {
      this.nextChangeAt = Date.now() + 2000;
      if (!this.stopped) {
        this.callbacks.invalidated();
        this.callbacks.error(String(error));
      }
    } finally {
      this.changing = false;
      void this.pump();
    }
  }
  private async pump() {
    if (
      this.sending ||
      this.stopped ||
      this.changeRevision !== null ||
      Date.now() < this.nextUploadAt
    )
      return;
    this.sending = true;
    try {
      while (!this.stopped && this.changeRevision === null && this.queued) {
        if (this.queued) {
          const frame = this.queued;
          this.queued = null;
          const { pixels, ...wire } = frame;
          this.frames.set(frame.source.frame_id, frame);
          while (this.frames.size > 8) this.frames.delete(this.frames.keys().next().value!);
          await this.api.frame(wire);
          this.lastSent = Date.now();
        }
      }
    } catch (error) {
      if (!this.stopped) {
        this.callbacks.invalidated();
        this.callbacks.error(error instanceof Error ? error.message : 'Ошибка соединения');
        this.worker.postMessage({ type: 'release', revision: this.revision });
        this.retryAt = Date.now() + Math.min(30000, 2000 * 2 ** Math.min(this.failures++, 4));
        this.nextUploadAt = this.retryAt;
      }
    } finally {
      this.sending = false;
    }
  }
  private async poll() {
    if (this.polling || this.stopped) return;
    this.polling = true;
    try {
      const response = await this.api.state();
      if (this.stopped) return;
      if (
        response.state.capture_epoch === this.epoch &&
        response.state.visual_revision === this.revision &&
        this.changeRevision === null
      )
        this.callbacks.state(response);
      for (const outcome of response.outcomes) {
        if (this.outcomes.has(outcome.frame_id)) continue;
        this.outcomes.add(outcome.frame_id);
        while (this.outcomes.size > 100) this.outcomes.delete(this.outcomes.values().next().value!);
        const frame = this.frames.get(outcome.frame_id);
        if (frame && frame.source.visual_revision === this.revision) {
          this.worker.postMessage(
            outcome.baseline_accepted
              ? { type: 'accept', revision: this.revision, pixels: frame.pixels }
              : { type: 'release', revision: this.revision },
          );
          if (outcome.status === 'recognized') {
            this.failures = 0;
            this.retryAt = Infinity;
          } else if (!['replaced', 'superseded'].includes(outcome.status)) {
            this.callbacks.error(outcome.status);
            this.retryAt = Date.now() + Math.min(30000, 2000 * 2 ** Math.min(this.failures++, 4));
          }
        }
        this.frames.delete(outcome.frame_id);
      }
      if (
        !response.queue.active &&
        !response.queue.pending &&
        !this.queued &&
        Date.now() - this.lastSent >= 1000 &&
        response.state.needs_confirmation &&
        response.state.visual_revision === this.revision
      )
        this.forced ??= 'confirmation';
    } catch (error) {
      if (!this.stopped) {
        this.callbacks.invalidated();
        this.callbacks.error(error instanceof Error ? error.message : 'Нет связи с сервером');
      }
    } finally {
      this.polling = false;
    }
  }
}
