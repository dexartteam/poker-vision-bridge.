import { afterEach, expect, it, vi } from 'vitest';
import type { Frame, PokerStarsObservation } from '../local/contracts';
import { ReplayController, type ReplayCaptureContext, type ReplayPorts } from './controller';

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((yes, no) => {
    resolve = yes;
    reject = no;
  });
  return { promise, resolve, reject };
}
const flush = async () => {
  for (let i = 0; i < 30; i++) await Promise.resolve();
};
const observation = (frame: Frame, hero = true): PokerStarsObservation => ({
  schema_version: 'local-vision.observation.v2',
  layout: 'pokerstars-classic',
  source: { ...frame.source },
  status: 'partial',
  evidence: 'single_frame',
  stable: true,
  decision_ready: false,
  ui_mode: 'live',
  hand_number: null,
  ui_street: 'preflop',
  pot_display: { value: 30, raw: '30', unit: 'chips' },
  pot_includes_current_bets: null,
  board: [],
  hero_cards: [
    { status: 'visible', value: 'As' },
    { status: 'visible', value: 'Kh' },
  ],
  hero_turn: hero,
  seats: [],
  dealer_seat: null,
  available_actions: hero ? ['fold', 'call', 'raise'] : [],
  action_history: { status: 'incomplete', events: [] },
  diagnostics: { ocr: {}, cards: [], elapsed_ms: 1 },
});

function setup(options: ConstructorParameters<typeof ReplayController>[2] = {}) {
  let time = 0;
  const pixels = new Uint8ClampedArray(16).fill(10);
  const ports = {
    currentTime: () => time,
    duration: () => 2,
    pause: vi.fn(),
    seek: vi.fn(async (target: number, _signal: AbortSignal) => {
      time = target;
    }),
    capture: vi.fn((c: ReplayCaptureContext) => ({
      frame: {
        source: {
          kind: 'recording' as const,
          epoch: c.epoch,
          frame_seq: c.frameSeq,
          revision: c.frameSeq,
          calibration_id: 'test-profile',
          captured_at: 1000,
          media_time: c.mediaTime,
        },
        stable: true,
        pixels: { width: 2, height: 2, data: pixels },
      },
      image: `frame-${c.mediaTime}`,
    })),
    recognize: vi.fn(async (frame: Frame, _signal: AbortSignal) => observation(frame)),
    handleDecision: vi.fn(
      async (_snapshot: unknown, _signal: AbortSignal): Promise<unknown> => ({
        status: 'missing_data',
        issues: [{ message: 'История действий неизвестна' }],
      }),
    ),
  } satisfies ReplayPorts;
  const callbacks = { change: vi.fn(), observation: vi.fn(), decision: vi.fn() };
  const controller = new ReplayController(ports, callbacks, options);
  return {
    controller,
    ports,
    callbacks,
    pixels,
    setTime: (value: number) => {
      time = value;
    },
  };
}

afterEach(() => {
  vi.useRealTimers();
});

it('holds the exact paused frame through slow OCR and solver response until manual continue', async () => {
  const { controller, ports, callbacks } = setup();
  const ocr = deferred<PokerStarsObservation>();
  const solver = deferred<unknown>();
  ports.recognize.mockImplementationOnce(() => ocr.promise);
  ports.handleDecision.mockImplementationOnce(() => solver.promise);
  const running = controller.start();
  await flush();
  expect(controller.state.phase).toBe('recognizing');
  expect(ports.seek.mock.calls.map(([time]) => time)).toEqual([0]);
  expect(ports.handleDecision).not.toHaveBeenCalled();
  const frame = ports.recognize.mock.calls[0][0];
  ocr.resolve(observation(frame));
  await flush();
  expect(controller.state.phase).toBe('solving');
  expect(ports.currentTime()).toBe(0);
  await controller.start();
  await controller.continue();
  expect(ports.recognize).toHaveBeenCalledTimes(1);
  solver.resolve({ status: 'solved', solver_result: { strategy: [{ action: 'call' }] } });
  await running;
  expect(controller.state.phase).toBe('decision');
  expect(controller.state.latest).toMatchObject({
    mediaTime: 0,
    frameSeq: 1,
    image: 'frame-0',
    trigger: 'hero_turn',
  });
  expect(callbacks.decision).toHaveBeenCalledTimes(1);
  expect(ports.seek).toHaveBeenCalledTimes(1);
  expect(ports.pause).toHaveBeenCalled();
  expect(controller.state.latest?.observation?.decision_ready).toBe(false);
});

it('suppresses duplicate hero frames then stops on another decision after buttons disappear', async () => {
  const { controller, ports } = setup();
  ports.recognize.mockImplementation(async (frame) =>
    observation(frame, frame.source.media_time !== 1),
  );
  await controller.start();
  expect(ports.currentTime()).toBe(0);
  await controller.continue();
  expect(ports.currentTime()).toBe(1.5);
  expect(ports.handleDecision).toHaveBeenCalledTimes(2);
  expect(ports.seek.mock.calls.map(([time]) => time)).toEqual([0, 0.5, 1, 1.5]);
  expect(controller.state.history.map((s) => s.trigger)).toEqual([
    'hero_turn',
    null,
    null,
    'hero_turn',
  ]);
});

it('stops again for a changed action state while hero buttons remain visible', async () => {
  const { controller, ports } = setup();
  ports.recognize.mockImplementation(async (frame) => ({
    ...observation(frame),
    available_actions: frame.source.media_time === 0 ? ['check', 'bet'] : ['fold', 'call', 'raise'],
  }));
  await controller.start();
  await controller.continue();
  expect(ports.currentTime()).toBe(0.5);
  expect(ports.handleDecision).toHaveBeenCalledTimes(2);
});

it('unknown button text does not rearm the same turn while active control evidence remains', async () => {
  const { controller, ports } = setup();
  ports.recognize.mockImplementation(async (frame) => {
    const result = observation(frame);
    if (frame.source.media_time === 0.5) {
      result.hero_turn = null;
      result.available_actions = [];
      result.diagnostics.ocr.control0 = { text: '???', confidence: 5 };
    }
    return result;
  });
  await controller.start();
  await controller.continue();
  expect(controller.state.phase).toBe('ended');
  expect(ports.handleDecision).toHaveBeenCalledTimes(1);
});

it('rearms on absence of active controls, which the current recognizer reports as null', async () => {
  const { controller, ports } = setup();
  ports.recognize.mockImplementation(async (frame) => {
    const result = observation(frame);
    if (frame.source.media_time === 0.5) {
      result.hero_turn = null;
      result.available_actions = [];
    }
    return result;
  });
  await controller.start();
  await controller.continue();
  expect(ports.currentTime()).toBe(1);
  expect(ports.handleDecision).toHaveBeenCalledTimes(2);
});

it('scan reaches the end with one sequential OCR per frame and no consumer calls for unknown turns', async () => {
  const { controller, ports } = setup();
  ports.recognize.mockImplementation(async (frame) => ({
    ...observation(frame, false),
    hero_turn: null,
  }));
  await controller.start();
  expect(controller.state.phase).toBe('ended');
  expect(ports.seek.mock.calls.map(([time]) => time)).toEqual([0, 0.5, 1, 1.5, 2]);
  expect(ports.handleDecision).not.toHaveBeenCalled();
  expect(controller.state.history.every((s) => s.observation?.decision_ready === false)).toBe(true);
});

it('manual analyze diagnoses an unknown turn and retry keeps the exact same frame identity', async () => {
  const { controller, ports } = setup();
  ports.recognize.mockImplementation(async (frame) => observation(frame, false));
  await controller.manualAnalyze();
  const first = controller.state.latest!;
  expect(first.trigger).toBe('manual');
  expect(first.issues).toEqual(['История действий неизвестна']);
  ports.handleDecision.mockResolvedValueOnce({ status: 'solved', issues: [] });
  await controller.retryDecision();
  expect(controller.state.latest?.id).toBe(first.id);
  expect(controller.state.latest?.observation?.source).toEqual(first.observation?.source);
  expect(controller.state.latest?.result).toEqual({ status: 'solved', issues: [] });
  expect(ports.recognize).toHaveBeenCalledTimes(1);
  expect(ports.handleDecision).toHaveBeenCalledTimes(2);
});

it('seek cancels a pending solver and discards its late result', async () => {
  const { controller, ports, callbacks } = setup();
  const solver = deferred<unknown>();
  ports.handleDecision.mockImplementationOnce(() => solver.promise);
  const running = controller.start();
  await flush();
  expect(controller.state.phase).toBe('solving');
  const signal = ports.handleDecision.mock.calls[0][1];
  await controller.seek(1.25);
  expect(signal.aborted).toBe(true);
  expect(controller.state).toMatchObject({ phase: 'paused', latest: null, mediaTime: 1.25 });
  solver.resolve({ status: 'solved' });
  await running;
  expect(callbacks.decision).not.toHaveBeenCalled();
  expect(controller.state.history[0].result).toBeNull();
});

it('source reset rejects late OCR and never overlaps an uncooperative in-flight recognizer', async () => {
  const { controller, ports, callbacks } = setup();
  const ocr = deferred<PokerStarsObservation>();
  ports.recognize.mockImplementationOnce(() => ocr.promise);
  const first = controller.start();
  await flush();
  const oldFrame = ports.recognize.mock.calls[0][0];
  controller.reset();
  const second = controller.start();
  await flush();
  expect(ports.recognize).toHaveBeenCalledTimes(1);
  ocr.resolve(observation(oldFrame));
  await Promise.all([first, second]);
  expect(ports.recognize).toHaveBeenCalledTimes(2);
  expect(callbacks.observation).toHaveBeenCalledTimes(1);
  expect(controller.state.history).toHaveLength(1);
  expect(controller.state.latest?.epoch).not.toBe(oldFrame.source.epoch);
});

it('timeout preserves the frame, reports an issue, and late OCR cannot start a solver', async () => {
  vi.useFakeTimers();
  const { controller, ports, callbacks } = setup({ recognitionTimeoutMs: 100 });
  const ocr = deferred<PokerStarsObservation>();
  ports.recognize.mockImplementationOnce(() => ocr.promise);
  const running = controller.start();
  await flush();
  const frame = ports.recognize.mock.calls[0][0];
  await vi.advanceTimersByTimeAsync(101);
  await running;
  expect(controller.state.phase).toBe('error');
  expect(controller.state.latest?.issues).toEqual(['Распознавание: превышено время ожидания']);
  expect(ports.recognize.mock.calls[0][1].aborted).toBe(true);
  ocr.resolve(observation(frame));
  await flush();
  expect(callbacks.observation).not.toHaveBeenCalled();
  expect(ports.handleDecision).not.toHaveBeenCalled();
});

it('solver timeout stays on the frame and does not apply a delayed response', async () => {
  vi.useFakeTimers();
  const { controller, ports, callbacks } = setup({ decisionTimeoutMs: 100 });
  const solver = deferred<unknown>();
  ports.handleDecision.mockImplementationOnce(() => solver.promise);
  const running = controller.start();
  await flush();
  await vi.advanceTimersByTimeAsync(101);
  await running;
  expect(controller.state.phase).toBe('error');
  expect(ports.currentTime()).toBe(0);
  solver.resolve({ status: 'solved' });
  await flush();
  expect(callbacks.decision).not.toHaveBeenCalled();
  expect(controller.state.latest?.result).toBeNull();
});

it('pause cancels pending seek before pixels or OCR are captured', async () => {
  const { controller, ports } = setup();
  const seek = deferred<void>();
  ports.seek.mockImplementationOnce(() => seek.promise);
  const running = controller.start();
  controller.pause();
  seek.resolve();
  await running;
  expect(controller.state.phase).toBe('paused');
  expect(ports.capture).not.toHaveBeenCalled();
  expect(ports.recognize).not.toHaveBeenCalled();
});

it('rejects an OCR answer if the player moved outside the controller during recognition', async () => {
  const { controller, ports, setTime } = setup();
  const ocr = deferred<PokerStarsObservation>();
  ports.recognize.mockImplementationOnce(() => ocr.promise);
  const running = controller.start();
  await flush();
  const frame = ports.recognize.mock.calls[0][0];
  setTime(1);
  ocr.resolve(observation(frame));
  await running;
  expect(controller.state.phase).toBe('error');
  expect(ports.handleDecision).not.toHaveBeenCalled();
  expect(controller.state.latest?.observation).toBeNull();
});

it.each(['foreign-frame', 'stale', 'decision-ready'])(
  'rejects %s local observations before the consumer',
  async (kind) => {
    const { controller, ports } = setup();
    ports.recognize.mockImplementation(async (frame) => {
      const result = observation(frame);
      if (kind === 'foreign-frame') result.source.frame_seq += 1;
      if (kind === 'stale') result.status = 'stale';
      if (kind === 'decision-ready') Object.assign(result, { decision_ready: true });
      return result;
    });
    await controller.start();
    expect(controller.state.phase).toBe('error');
    expect(ports.handleDecision).not.toHaveBeenCalled();
  },
);

it('copies captured pixels and exported snapshots so callbacks and consumers cannot corrupt history', async () => {
  const { controller, ports, pixels } = setup({ historyLimit: 2 });
  const ocr = deferred<PokerStarsObservation>();
  ports.recognize.mockImplementationOnce(() => ocr.promise);
  const running = controller.start();
  await flush();
  pixels.fill(99);
  const frame = ports.recognize.mock.calls[0][0];
  expect(frame.pixels.data[0]).toBe(10);
  const result = observation(frame);
  ocr.resolve(result);
  await running;
  result.pot_display.value = 999;
  const state = controller.state;
  state.latest!.issues.push('mutated');
  expect(controller.state.latest?.issues).not.toContain('mutated');
  expect(controller.state.latest?.observation?.pot_display.value).toBe(30);
  await controller.continue();
  expect(controller.recording().snapshots).toHaveLength(2);
  expect(controller.recording().snapshots[0].mediaTime).toBe(1.5);
});

it('dispose rejects late completion and no command can restart work', async () => {
  const { controller, ports, callbacks } = setup();
  const ocr = deferred<PokerStarsObservation>();
  ports.recognize.mockImplementationOnce(() => ocr.promise);
  const running = controller.start();
  await flush();
  const frame = ports.recognize.mock.calls[0][0];
  controller.dispose();
  ocr.resolve(observation(frame));
  await running;
  await controller.start();
  await controller.continue();
  await controller.manualAnalyze();
  expect(controller.state.phase).toBe('disposed');
  expect(ports.recognize).toHaveBeenCalledTimes(1);
  expect(callbacks.observation).not.toHaveBeenCalled();
});

it('single stepping does not scan through another frame and detects a failed seek', async () => {
  const { controller, ports } = setup();
  ports.recognize.mockImplementation(async (frame) => observation(frame, false));
  await controller.step();
  expect(controller.state.phase).toBe('paused');
  expect(ports.currentTime()).toBe(0.5);
  expect(ports.recognize).toHaveBeenCalledTimes(1);
  ports.seek.mockImplementationOnce(async () => {});
  await controller.step();
  expect(controller.state.phase).toBe('error');
  expect(ports.recognize).toHaveBeenCalledTimes(1);
});
