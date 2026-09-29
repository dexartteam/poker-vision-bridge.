import type { Frame, LocalObservation } from '../local/contracts';

export type ReplayPhase =
  | 'idle'
  | 'seeking'
  | 'recognizing'
  | 'solving'
  | 'decision'
  | 'paused'
  | 'ended'
  | 'error'
  | 'disposed';

/** A JSON-safe, frame-bound history entry. Full pixels remain in the active operation only. */
export type ReplaySnapshot = {
  id: string;
  epoch: string;
  frameSeq: number;
  mediaTime: number;
  image?: string;
  observation: LocalObservation | null;
  result: unknown | null;
  issues: string[];
  trigger: 'hero_turn' | 'manual' | null;
};
export type ReplayState = {
  phase: ReplayPhase;
  latest: ReplaySnapshot | null;
  history: ReplaySnapshot[];
  error: string | null;
  mediaTime: number;
};
export type ReplayCaptureContext = { epoch: string; frameSeq: number; mediaTime: number };
export type ReplayPorts = {
  duration: () => number;
  currentTime: () => number;
  pause: () => void;
  /** Resolve only once the requested paused frame has decoded. */
  seek: (time: number, signal: AbortSignal) => Promise<void>;
  capture: (context: ReplayCaptureContext) => Frame | { frame: Frame; image?: string };
  recognize: (frame: Frame, signal: AbortSignal) => Promise<LocalObservation>;
  /** Diagnose first; the consumer alone owns readiness checks and solver transport. */
  handleDecision: (snapshot: ReplaySnapshot, signal: AbortSignal) => Promise<unknown>;
};
export type ReplayCallbacks = {
  change?: (state: ReplayState) => void;
  observation?: (snapshot: ReplaySnapshot) => void;
  decision?: (snapshot: ReplaySnapshot) => void;
};
export type ReplayOptions = {
  stepSeconds?: number;
  recognitionTimeoutMs?: number;
  decisionTimeoutMs?: number;
  seekTimeoutMs?: number;
  historyLimit?: number;
};

const cancelled = () =>
  Object.assign(new Error('Replay operation cancelled'), { name: 'AbortError' });
const isCancelled = (error: unknown) => error instanceof Error && error.name === 'AbortError';
const copy = <T>(value: T): T => structuredClone(value);

/** Excludes OCR timings, raw text and source timestamps so the same visible turn stops once. */
export function decisionFingerprint(observation: LocalObservation): string | null {
  if (
    observation.schema_version !== 'local-vision.observation.v2' ||
    observation.hero_turn !== true
  )
    return null;
  return JSON.stringify({
    hand: observation.hand_number,
    street: observation.ui_street,
    board: observation.board.map(({ status, value }) => [status, value]),
    hero: observation.hero_cards.map(({ status, value }) => [status, value]),
    pot: observation.pot_display.value,
    dealer: observation.dealer_seat,
    actions: [...observation.available_actions].sort(),
    seats: [...observation.seats]
      .sort((a, b) => a.seat_index - b.seat_index)
      .map((s) => [
        s.seat_index,
        s.stack,
        s.chip_display,
        s.chip_visibility,
        s.action_label,
        s.status,
      ]),
  });
}

function buttonsDisappeared(observation: LocalObservation): boolean {
  if (observation.schema_version !== 'local-vision.observation.v2') return false;
  if (observation.hero_turn === false) return true;
  // The current recognizer emits control OCR only for visibly active red buttons.
  // A failed label OCR with those keys present is uncertainty, not a completed turn.
  return (
    observation.hero_turn === null &&
    !Object.keys(observation.diagnostics.ocr).some((key) => /^control\d+$/.test(key))
  );
}

/**
 * Offline stepping: seek -> immutable capture -> OCR -> optional consumer -> next seek.
 * It never plays the video or upgrades the local partial observation to decision-ready.
 */
export class ReplayController {
  private phase: ReplayPhase = 'idle';
  private epoch = crypto.randomUUID();
  private sequence = 0;
  private generation = 0;
  private active: AbortController | null = null;
  private latest: ReplaySnapshot | null = null;
  private history: ReplaySnapshot[] = [];
  private error: string | null = null;
  private lastDecision: string | null = null;
  // An adapter that ignores AbortSignal still occupies the slot until it settles.
  private occupied: Promise<void> | null = null;
  private readonly options: Required<ReplayOptions>;

  constructor(
    private ports: ReplayPorts,
    private callbacks: ReplayCallbacks = {},
    options: ReplayOptions = {},
  ) {
    this.options = {
      stepSeconds: 0.5,
      recognitionTimeoutMs: 120000,
      decisionTimeoutMs: 120000,
      seekTimeoutMs: 10000,
      historyLimit: 200,
      ...options,
    };
    for (const [key, value] of Object.entries(this.options))
      if (!Number.isFinite(value) || value <= 0) throw new Error(`Invalid replay option: ${key}`);
    if (!Number.isInteger(this.options.historyLimit))
      throw new Error('historyLimit must be an integer');
  }

  get state(): ReplayState {
    return copy({
      phase: this.phase,
      latest: this.latest,
      history: this.history,
      error: this.error,
      mediaTime: this.ports.currentTime(),
    });
  }

  /** Scan from the current position. A decision requires explicit continue(). */
  start(): Promise<void> {
    if (this.active || this.phase === 'decision' || this.phase === 'disposed')
      return Promise.resolve();
    return this.run(this.ports.currentTime(), true, false);
  }

  /** Leave the inspected frame and continue scanning. */
  continue(): Promise<void> {
    if (this.active || this.phase === 'disposed') return Promise.resolve();
    const current = this.latest?.mediaTime ?? this.ports.currentTime();
    if (current >= this.ports.duration()) {
      this.setPhase('ended');
      return Promise.resolve();
    }
    return this.run(current + this.options.stepSeconds, true, false);
  }

  /** Examine one subsequent frame without automatically scanning further. */
  step(): Promise<void> {
    if (this.active || this.phase === 'disposed') return Promise.resolve();
    return this.run(this.ports.currentTime() + this.options.stepSeconds, false, false);
  }

  /** Retry/diagnose the current frame even when OCR did not establish hero_turn. */
  manualAnalyze(): Promise<void> {
    if (this.active || this.phase === 'disposed') return Promise.resolve();
    return this.run(this.ports.currentTime(), false, true);
  }

  /** Re-submit confirmed corrections for this exact frame without giving them a new source id. */
  async retryDecision(): Promise<void> {
    if (this.active || this.phase === 'disposed' || !this.latest?.observation) return;
    if (Math.abs(this.latest.mediaTime - this.ports.currentTime()) > 0.001) return;
    const snapshot = this.latest;
    const controller = new AbortController();
    this.active = controller;
    const token = this.generation;
    this.error = null;
    this.ports.pause();
    try {
      this.setPhase('solving');
      const result = await this.single(
        () => this.ports.handleDecision(copy(snapshot), controller.signal),
        controller.signal,
        this.options.decisionTimeoutMs,
        'Ответ солвера',
      );
      this.assertCurrent(token, controller.signal);
      this.assertMediaTime(snapshot.mediaTime);
      this.acceptResult(snapshot, result);
      this.setPhase('decision');
      this.callbacks.decision?.(copy(snapshot));
    } catch (error) {
      this.fail(error, token, controller.signal);
    } finally {
      if (this.active === controller) this.active = null;
    }
  }

  pause(): void {
    if (this.phase === 'disposed') return;
    this.invalidate();
    this.setPhase('paused');
  }

  /** A user seek invalidates the decision identity and all pending answers. */
  async seek(time: number): Promise<void> {
    if (this.phase === 'disposed') return;
    this.invalidate();
    this.epoch = crypto.randomUUID();
    this.sequence = 0;
    this.lastDecision = null;
    this.latest = null;
    this.error = null;
    const token = this.generation;
    const controller = new AbortController();
    this.active = controller;
    try {
      const target = this.target(time);
      this.setPhase('seeking');
      await this.wait(
        this.ports.seek(target, controller.signal),
        controller.signal,
        this.options.seekTimeoutMs,
        'Перемотка',
      );
      this.assertCurrent(token, controller.signal);
      this.setPhase('paused');
    } catch (error) {
      this.fail(error, token, controller.signal);
    } finally {
      if (this.active === controller) this.active = null;
    }
  }

  /** Call on source, profile or template change before using the new input. */
  reset(): void {
    if (this.phase === 'disposed') return;
    this.invalidate();
    this.epoch = crypto.randomUUID();
    this.sequence = 0;
    this.lastDecision = null;
    this.latest = null;
    this.history = [];
    this.error = null;
    this.setPhase('idle');
  }

  dispose(): void {
    if (this.phase === 'disposed') return;
    this.invalidate();
    this.setPhase('disposed');
  }

  recording() {
    return copy({
      schema_version: 'replay.recording.v1',
      source_kind: 'recording',
      epoch: this.epoch,
      step_seconds: this.options.stepSeconds,
      snapshots: this.history,
    });
  }

  private target(time: number): number {
    const duration = this.ports.duration();
    if (!Number.isFinite(duration) || duration <= 0 || !Number.isFinite(time))
      throw new Error('Загрузите видеозапись с известной длительностью');
    return Math.max(0, Math.min(time, duration));
  }

  private invalidate(): void {
    this.generation++;
    this.active?.abort();
    this.active = null;
    this.ports.pause();
  }

  private setPhase(phase: ReplayPhase): void {
    this.phase = phase;
    this.callbacks.change?.(this.state);
  }

  private assertCurrent(token: number, signal: AbortSignal): void {
    if (signal.aborted || token !== this.generation || this.phase === 'disposed') throw cancelled();
  }

  private assertMediaTime(mediaTime: number): void {
    if (Math.abs(this.ports.currentTime() - mediaTime) > 0.001)
      throw new Error('Позиция видео изменилась во время анализа кадра');
  }

  private async run(time: number, scan: boolean, manual: boolean): Promise<void> {
    const controller = new AbortController();
    const signal = controller.signal;
    this.active = controller;
    const token = this.generation;
    this.error = null;
    this.ports.pause();
    try {
      let target = this.target(time);
      while (true) {
        this.assertCurrent(token, signal);
        // The previous answer remains in history, never beside a different video position.
        this.latest = null;
        this.setPhase('seeking');
        await this.wait(
          this.ports.seek(target, signal),
          signal,
          this.options.seekTimeoutMs,
          'Перемотка',
        );
        this.assertCurrent(token, signal);
        this.ports.pause();
        const mediaTime = this.ports.currentTime();
        if (!Number.isFinite(mediaTime) || Math.abs(mediaTime - target) > 0.05)
          throw new Error('Видеопроигрыватель не остановился на запрошенном кадре');
        const context = { epoch: this.epoch, frameSeq: ++this.sequence, mediaTime };
        const capture = this.ports.capture(context);
        const { frame, image } =
          'frame' in capture ? capture : { frame: capture, image: undefined };
        const frozen = copy(frame);
        if (
          frozen.source.kind !== 'recording' ||
          frozen.source.epoch !== context.epoch ||
          frozen.source.frame_seq !== context.frameSeq ||
          frozen.source.media_time !== mediaTime
        )
          throw new Error('Захваченный кадр не принадлежит текущей видеозаписи');
        const snapshot: ReplaySnapshot = {
          id: `${context.epoch}:${context.frameSeq}`,
          ...context,
          image,
          observation: null,
          result: null,
          issues: [],
          trigger: null,
        };
        this.latest = snapshot;
        this.history.push(snapshot);
        if (this.history.length > this.options.historyLimit) this.history.shift();
        this.setPhase('recognizing');
        const observation = await this.single(
          () => this.ports.recognize(copy(frozen), signal),
          signal,
          this.options.recognitionTimeoutMs,
          'Распознавание',
        );
        this.assertCurrent(token, signal);
        this.assertMediaTime(mediaTime);
        if (
          Object.keys(frozen.source).some(
            (key) =>
              observation.source[key as keyof Frame['source']] !==
              frozen.source[key as keyof Frame['source']],
          )
        )
          throw new Error('Результат распознавания относится к другому кадру');
        if (observation.decision_ready !== false || observation.status === 'stale')
          throw new Error('Локальное наблюдение должно оставаться частичным и привязанным к кадру');
        snapshot.observation = copy(observation);
        const fingerprint = decisionFingerprint(observation);
        const newDecision = fingerprint !== null && fingerprint !== this.lastDecision;
        if (buttonsDisappeared(observation)) this.lastDecision = null;
        snapshot.trigger = manual ? 'manual' : newDecision ? 'hero_turn' : null;
        this.callbacks.observation?.(copy(snapshot));
        this.assertCurrent(token, signal);
        if (snapshot.trigger) {
          this.lastDecision = fingerprint;
          this.setPhase('solving');
          const result = await this.single(
            () => this.ports.handleDecision(copy(snapshot), signal),
            signal,
            this.options.decisionTimeoutMs,
            'Ответ солвера',
          );
          this.assertCurrent(token, signal);
          this.assertMediaTime(mediaTime);
          this.acceptResult(snapshot, result);
          this.setPhase('decision');
          this.callbacks.decision?.(copy(snapshot));
          return;
        }
        if (mediaTime >= this.ports.duration() - 0.001) {
          this.setPhase('ended');
          return;
        }
        if (!scan) {
          this.setPhase('paused');
          return;
        }
        target = this.target(mediaTime + this.options.stepSeconds);
      }
    } catch (error) {
      this.fail(error, token, signal);
    } finally {
      if (this.active === controller) this.active = null;
    }
  }

  private fail(error: unknown, token: number, signal: AbortSignal): void {
    if (isCancelled(error) || signal.aborted || token !== this.generation) return;
    this.active?.abort();
    this.error = error instanceof Error ? error.message : String(error);
    this.latest?.issues.push(this.error);
    this.ports.pause();
    this.setPhase('error');
  }

  private acceptResult(snapshot: ReplaySnapshot, result: unknown): void {
    snapshot.result = copy(result ?? null);
    snapshot.issues = [];
    if (result && typeof result === 'object' && 'issues' in result && Array.isArray(result.issues))
      snapshot.issues = result.issues.map((issue: unknown) =>
        issue && typeof issue === 'object' && 'message' in issue
          ? String(issue.message)
          : String(issue),
      );
  }

  private async single<T>(
    work: () => Promise<T>,
    signal: AbortSignal,
    timeout: number,
    label: string,
  ): Promise<T> {
    if (this.occupied)
      await this.wait(this.occupied, signal, timeout, 'Завершение предыдущей операции');
    if (signal.aborted) throw cancelled();
    const promise = Promise.resolve().then(() => {
      if (signal.aborted) throw cancelled();
      return work();
    });
    const occupied = promise.then(
      () => {},
      () => {},
    );
    this.occupied = occupied;
    void occupied.then(() => {
      if (this.occupied === occupied) this.occupied = null;
    });
    return this.wait(promise, signal, timeout, label);
  }

  private wait<T>(
    promise: Promise<T>,
    signal: AbortSignal,
    timeout: number,
    label: string,
  ): Promise<T> {
    return new Promise((resolve, reject) => {
      const cleanup = () => {
        clearTimeout(timer);
        signal.removeEventListener('abort', onAbort);
      };
      const onAbort = () => {
        cleanup();
        reject(cancelled());
      };
      const timer = setTimeout(() => {
        cleanup();
        reject(new Error(`${label}: превышено время ожидания`));
      }, timeout);
      if (signal.aborted) {
        onAbort();
      } else {
        signal.addEventListener('abort', onAbort, { once: true });
      }
      promise.then(
        (value) => {
          cleanup();
          resolve(value);
        },
        (error: unknown) => {
          cleanup();
          reject(error);
        },
      );
    });
  }
}
