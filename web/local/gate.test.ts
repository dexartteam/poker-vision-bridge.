import { describe, it, expect, vi, afterEach } from 'vitest';
import { LocalGate } from './gate';
import type { Frame, LocalObservation } from './contracts';
const frame = (seq: number, revision = 0, at = 1000): Frame => ({
  stable: true,
  source: {
    kind: 'recording',
    epoch: 'epoch',
    calibration_id: 'calibration',
    frame_seq: seq,
    revision,
    captured_at: at,
    media_time: seq,
  },
  pixels: { data: new Uint8ClampedArray(4), width: 1, height: 1 },
});
const observation = (f: Frame): LocalObservation => ({
  schema_version: 'local-vision.observation.v1',
  source: { ...f.source },
  stable: f.stable,
  status: 'partial',
  evidence: 'single_frame',
  decision_ready: false,
  hand_number: 4812,
  ui_mode: 'live',
  ui_street: 'turn',
  pot_display: { value: 399, raw: '399', unit: 'chips' },
  board: [],
  hero_cards: null,
  hero_turn: null,
  seats: null,
  pot_includes_current_bets: null,
  diagnostics: { ocr: {}, cards: [], elapsed_ms: 10 },
});
const flush = async () => {
  for (let i = 0; i < 8; i++) await Promise.resolve();
};
afterEach(() => vi.useRealTimers());
describe('local recognition queue and freshness', () => {
  it('keeps a superseded reading only as history without confirming current state', async () => {
    const applied = vi.fn(),
      outdated = vi.fn();
    let finish!: (o: LocalObservation) => void;
    const gate = new LocalGate(
      'epoch',
      'calibration',
      () => 1000,
      applied,
      vi.fn(),
      0,
      vi.fn(),
      outdated,
    );
    const a = frame(1),
      original = observation(a);
    gate.offer(
      a,
      () =>
        new Promise((resolve) => {
          finish = resolve;
        }),
    );
    gate.change(1);
    finish(original);
    await flush();
    expect(applied).not.toHaveBeenCalled();
    expect(outdated).toHaveBeenCalledExactlyOnceWith(
      expect.objectContaining({
        status: 'stale',
        hand_number: 4812,
        source: a.source,
        decision_ready: false,
      }),
      'changed',
    );
    const historical = outdated.mock.calls[0][0];
    expect(original.status).toBe('partial');
    expect(historical.source).not.toBe(original.source);
    const b = frame(2, 1);
    gate.offer(b, async () => observation(b));
    await flush();
    expect(applied).toHaveBeenCalledExactlyOnceWith(
      expect.objectContaining({
        status: 'partial',
        evidence: 'single_frame',
        source: b.source,
      }),
    );
  });
  it('shows slow OCR as expired history and still accepts the next timely frame', async () => {
    let now = 1000,
      finish!: (o: LocalObservation) => void;
    const applied = vi.fn(),
      outdated = vi.fn();
    const gate = new LocalGate(
      'epoch',
      'calibration',
      () => now,
      applied,
      vi.fn(),
      0,
      vi.fn(),
      outdated,
    );
    const a = frame(1);
    gate.offer(
      a,
      () =>
        new Promise((resolve) => {
          finish = resolve;
        }),
    );
    now = 7000;
    finish(observation(a));
    await flush();
    expect(applied).not.toHaveBeenCalled();
    expect(outdated).toHaveBeenCalledExactlyOnceWith(
      expect.objectContaining({
        status: 'stale',
        source: a.source,
      }),
      'expired',
    );
    const b = frame(2, 0, now);
    gate.offer(b, async () => observation(b));
    await flush();
    expect(applied).toHaveBeenCalledExactlyOnceWith(
      expect.objectContaining({ status: 'partial', source: b.source }),
    );
  });
  it('does not expose mismatched identity or an older sequence as history', async () => {
    const applied = vi.fn(),
      outdated = vi.fn();
    const gate = new LocalGate(
      'epoch',
      'calibration',
      () => 1000,
      applied,
      vi.fn(),
      0,
      vi.fn(),
      outdated,
    );
    const current = frame(2);
    gate.offer(current, async () => observation(current));
    await flush();
    for (const source of [
      { ...frame(3).source, epoch: 'wrong' },
      { ...frame(3).source, calibration_id: 'wrong' },
      { ...frame(3).source, captured_at: 999 },
    ]) {
      gate.offer(frame(3), async () => ({ ...observation(frame(3)), source }));
      await flush();
    }
    gate.offer(frame(1), async () => observation(frame(1)));
    await flush();
    expect(applied).toHaveBeenCalledTimes(1);
    expect(outdated).not.toHaveBeenCalled();
  });
  it('does not expose a late historical result after closing the session', async () => {
    const applied = vi.fn(),
      outdated = vi.fn();
    let finish!: (o: LocalObservation) => void;
    const gate = new LocalGate(
      'epoch',
      'calibration',
      () => 1000,
      applied,
      vi.fn(),
      0,
      vi.fn(),
      outdated,
    );
    const a = frame(1);
    gate.offer(
      a,
      () =>
        new Promise((resolve) => {
          finish = resolve;
        }),
    );
    gate.change(1);
    gate.close();
    finish(observation(a));
    await flush();
    expect(applied).not.toHaveBeenCalled();
    expect(outdated).not.toHaveBeenCalled();
  });
  it('keeps one active task, replaces pending work and rejects a superseded result', async () => {
    const applied = vi.fn(),
      started: number[] = [];
    let finish!: (v: LocalObservation) => void;
    const gate = new LocalGate('epoch', 'calibration', () => 1000, applied, vi.fn(), 0);
    const a = frame(1),
      b = frame(2, 1),
      c = frame(3, 2);
    gate.offer(a, () => {
      started.push(1);
      return new Promise((r) => {
        finish = r;
      });
    });
    gate.change(1);
    gate.offer(b, async () => {
      started.push(2);
      return observation(b);
    });
    gate.change(2);
    gate.offer(c, async () => {
      started.push(3);
      return observation(c);
    });
    expect(started).toEqual([1]);
    finish(observation(a));
    await flush();
    expect(started).toEqual([1, 3]);
    expect(applied).toHaveBeenCalledTimes(1);
    expect(applied.mock.calls[0][0].source.frame_seq).toBe(3);
  });
  it('does not publish after stop or accept a wrong epoch/calibration', async () => {
    const applied = vi.fn(),
      task = vi.fn();
    let finish!: (v: LocalObservation) => void;
    const gate = new LocalGate('epoch', 'calibration', () => 1000, applied, vi.fn(), 0);
    const a = frame(1);
    gate.offer({ ...a, source: { ...a.source, epoch: 'other' } }, task);
    gate.offer({ ...a, source: { ...a.source, calibration_id: 'other' } }, task);
    expect(task).not.toHaveBeenCalled();
    gate.offer(
      a,
      () =>
        new Promise((r) => {
          finish = r;
        }),
    );
    gate.close();
    finish(observation(a));
    await flush();
    expect(applied).not.toHaveBeenCalled();
  });
  it('rejects a late response and cannot refresh its captured time', async () => {
    let now = 1000,
      finish!: (v: LocalObservation) => void;
    const applied = vi.fn();
    const gate = new LocalGate('epoch', 'calibration', () => now, applied, vi.fn(), 0),
      a = frame(1);
    gate.offer(
      a,
      () =>
        new Promise((r) => {
          finish = r;
        }),
    );
    now = 7000;
    finish(observation(a));
    await flush();
    expect(applied).not.toHaveBeenCalled();
  });
  it('releases stale pending work so detector can request a fresh frame after slow OCR', async () => {
    let now = 1000,
      finish!: (v: LocalObservation) => void;
    const discarded = vi.fn(),
      pending = vi.fn(),
      applied = vi.fn();
    const gate = new LocalGate('epoch', 'calibration', () => now, applied, vi.fn(), 0, discarded);
    const a = frame(1),
      b = frame(2, 1, 1200);
    gate.offer(
      a,
      () =>
        new Promise((r) => {
          finish = r;
        }),
    );
    now = 1200;
    gate.change(1);
    gate.offer(b, pending);
    now = 8000;
    finish(observation(a));
    await flush();
    expect(pending).not.toHaveBeenCalled();
    expect(discarded).toHaveBeenCalledWith(b);
    const fresh = frame(3, 1, 8000);
    gate.offer(fresh, async () => observation(fresh));
    await flush();
    expect(applied).toHaveBeenCalledTimes(1);
  });
  it('requires distinct stable frames, preserves recording identity, and resets on replay', async () => {
    const applied = vi.fn();
    const gate = new LocalGate('epoch', 'calibration', () => 1000, applied, vi.fn(), 0);
    const a = frame(1),
      b = frame(2),
      c = frame(3),
      d = frame(4);
    gate.offer(a, async () => observation(a));
    await flush();
    gate.offer(a, async () => observation(a));
    await flush();
    expect(applied).toHaveBeenCalledTimes(1);
    gate.offer(b, async () => observation(b));
    await flush();
    expect(applied.mock.calls[1][0].evidence).toBe('repeated');
    expect(applied.mock.calls[1][0].source.kind).toBe('recording');
    gate.offer(c, async () => ({ ...observation(c), ui_mode: 'replay' }));
    await flush();
    gate.offer(d, async () => observation(d));
    await flush();
    expect(applied.mock.calls[3][0].evidence).toBe('single_frame');
    expect(applied.mock.calls.every(([o]) => o.status === 'partial' && !o.decision_ready)).toBe(
      true,
    );
  });
  it('does not confirm matching nulls or unstable frames', async () => {
    const applied = vi.fn(),
      gate = new LocalGate('epoch', 'calibration', () => 1000, applied, vi.fn(), 0);
    for (const seq of [1, 2]) {
      const f = frame(seq);
      gate.offer(f, async () => ({
        ...observation(f),
        pot_display: { value: null, raw: null, unit: 'chips' },
      }));
      await flush();
    }
    expect(applied.mock.calls.every(([o]) => o.evidence !== 'repeated')).toBe(true);
  });
  it('rate-limits all attempts and drops pending work when stopped', async () => {
    vi.useFakeTimers();
    vi.setSystemTime(1000);
    const applied = vi.fn(),
      task = vi.fn(),
      gate = new LocalGate('epoch', 'calibration', Date.now, applied, vi.fn());
    const a = frame(1),
      b = frame(2);
    gate.offer(a, async () => observation(a));
    await flush();
    gate.offer(b, task);
    await vi.advanceTimersByTimeAsync(999);
    expect(task).not.toHaveBeenCalled();
    gate.close();
    await vi.advanceTimersByTimeAsync(1000);
    expect(task).not.toHaveBeenCalled();
  });
});
