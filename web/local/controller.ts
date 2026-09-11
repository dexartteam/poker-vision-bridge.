import { DEFAULTS, type Detection, type Pixels, type Reason } from '../core/detector';
import { VideoFreshness } from '../capture/freshness';
import {
  validateLocalProfile,
  type Frame,
  type LocalObservation,
  type LocalProfile,
} from './contracts';
import { BrowserRecognizer } from './recognizer';
import { LocalGate, type OutdatedReason } from './gate';
import type { CardTemplate } from './cards';
import type { SymbolSet } from './pokerstars/symbols';
type Callbacks = {
  state: (o: LocalObservation) => void;
  detection: (d: Detection) => void;
  invalidated: (reason: 'changed' | 'stopped') => void;
  outdated?: (o: LocalObservation, reason: OutdatedReason) => void;
  error: (s: string) => void;
  stopped: () => void;
  count: (n: number) => void;
};
export class LocalController {
  private profile: LocalProfile;
  private epoch = crypto.randomUUID();
  private detector: Worker;
  private recognizer: BrowserRecognizer;
  private gate: LocalGate;
  private closed = false;
  private timer = 0;
  private freshness: VideoFreshness | null = null;
  private capture: { frame: Frame; small: Pixels } | null = null;
  private busy = false;
  private seq = 0;
  private requests = 0;
  private forced: Reason | null = null;
  private full = document.createElement('canvas');
  private small = document.createElement('canvas');
  private events: LocalObservation[] = [];
  private stopEvent = () => {
    void this.stop();
  };
  constructor(
    private video: HTMLVideoElement,
    profile: LocalProfile,
    private kind: 'camera' | 'recording',
    private callbacks: Callbacks,
    templates: CardTemplate[] = [],
    symbols: SymbolSet | null = null,
  ) {
    this.profile = validateLocalProfile(profile);
    this.recognizer = new BrowserRecognizer(templates, symbols);
    this.full.width = profile.width;
    this.full.height = profile.height;
    this.small.width = Math.min(640, profile.width);
    this.small.height = Math.round((profile.height * this.small.width) / profile.width);
    this.detector = new Worker(new URL('../detection/worker.ts', import.meta.url), {
      type: 'module',
    });
    this.detector.postMessage({
      type: 'init',
      config: { ...DEFAULTS, safetyPollMs: 2000 },
      regions: Object.entries(this.profile.regions)
        .filter(([name]) => name !== 'chat')
        .map(([name, r]) => ({
          name,
          ...r,
          ignore: false,
        })),
    });
    this.gate = new LocalGate(
      this.epoch,
      profile.id,
      () => Date.now(),
      (o) => {
        this.events.push(o);
        if (this.events.length > 100) this.events.shift();
        this.callbacks.state(o);
        if (o.stable && o.evidence === 'single_frame' && o.ui_mode === 'live')
          this.forced = 'confirmation';
      },
      (error) => {
        this.callbacks.error(
          error instanceof Error ? error.message : 'Ошибка локального распознавания',
        );
        void this.stop();
      },
      1000,
      (frame) => {
        if (!this.closed)
          this.detector.postMessage({ type: 'release', revision: frame.source.revision });
      },
      (o, reason) => {
        this.events.push(structuredClone(o));
        if (this.events.length > 100) this.events.shift();
        this.callbacks.outdated?.(o, reason);
      },
    );
    this.detector.onmessage = (e) => this.result(e.data);
    this.detector.onerror = () => {
      this.callbacks.error('Ошибка детектора');
      void this.stop();
    };
  }
  async start() {
    for (const event of ['seeking', 'pause', 'ended', 'error'])
      this.video.addEventListener(event, this.stopEvent);
    await this.recognizer.start();
    if (this.closed) return;
    this.freshness = new VideoFreshness(this.video);
    this.timer = window.setInterval(() => this.tick(), 200);
  }
  async stop() {
    if (this.closed) return;
    this.closed = true;
    this.gate.close();
    clearInterval(this.timer);
    this.freshness?.close();
    this.detector.terminate();
    this.capture = null;
    for (const event of ['seeking', 'pause', 'ended', 'error'])
      this.video.removeEventListener(event, this.stopEvent);
    this.callbacks.invalidated('stopped');
    this.callbacks.stopped();
    await this.recognizer.close();
  }
  force() {
    this.forced = 'manual';
  }
  recording() {
    return {
      schema_version: 'local-vision.recording.v1',
      profile: this.profile,
      source_kind: this.kind,
      observations: structuredClone(this.events),
    };
  }
  private tick() {
    if (this.closed || this.busy || document.hidden || this.video.paused) return;
    try {
      if (
        this.video.videoWidth !== this.profile.width ||
        this.video.videoHeight !== this.profile.height
      )
        throw new Error('Изменился размер видеовхода');
      const at = this.freshness!.take(Date.now());
      if (at === null) return;
      const ctx = this.full.getContext('2d', { willReadFrequently: true })!;
      ctx.drawImage(this.video, 0, 0);
      const small = this.small.getContext('2d', { willReadFrequently: true })!;
      small.drawImage(this.full, 0, 0, this.small.width, this.small.height);
      const p = small.getImageData(0, 0, this.small.width, this.small.height);
      const frame: Frame = {
        source: {
          kind: this.kind,
          epoch: this.epoch,
          calibration_id: this.profile.id,
          frame_seq: ++this.seq,
          revision: 0,
          captured_at: at,
          media_time: this.kind === 'recording' ? this.video.currentTime : null,
        },
        stable: false,
        pixels: {
          width: this.profile.width,
          height: this.profile.height,
          data: ctx.getImageData(0, 0, this.profile.width, this.profile.height).data,
        },
      };
      this.capture = { frame, small: { width: p.width, height: p.height, data: p.data } };
      this.busy = true;
      this.detector.postMessage({
        type: 'frame',
        pixels: this.capture.small,
        seq: this.seq,
        now: performance.now(),
        force: this.forced,
      });
    } catch (error) {
      this.callbacks.error(String(error));
      void this.stop();
    }
  }
  private result(message: { type: string; seq?: number; result?: Detection; message?: string }) {
    if (this.closed) return;
    if (message.type === 'error') {
      this.callbacks.error(message.message || 'Ошибка детектора');
      void this.stop();
      return;
    }
    if (!this.capture || message.seq !== this.capture.frame.source.frame_seq || !message.result)
      return;
    const d = message.result,
      frame = this.capture.frame;
    this.capture = null;
    this.busy = false;
    frame.source.revision = d.revision;
    frame.stable = d.stable;
    this.callbacks.detection(d);
    if (d.changed) {
      this.gate.change(d.revision);
      this.callbacks.invalidated('changed');
    }
    if (!d.candidate) return;
    this.forced = null;
    this.gate.offer(frame, async () => {
      this.callbacks.count(++this.requests);
      const observation = await this.recognizer.recognize(frame, this.profile);
      if (!this.closed)
        this.detector.postMessage({ type: 'release', revision: frame.source.revision });
      return observation;
    });
  }
}
