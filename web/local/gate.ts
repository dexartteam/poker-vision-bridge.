import type { Frame, LocalObservation } from './contracts';
export type OutdatedReason = 'changed' | 'expired';
type Task = () => Promise<LocalObservation>;
/** One active + one replaceable pending. Captured frames are owned by each task. */
export class LocalGate {
  private pending: { frame: Frame; task: Task } | null = null;
  private busy = false;
  private closed = false;
  private revision = 0;
  private lastSeq = 0;
  private previous: { key: string; seq: number; at: number } | null = null;
  private lastStart = -Infinity;
  private wake: ReturnType<typeof setTimeout> | null = null;
  constructor(
    private epoch: string,
    private calibration: string,
    private now: () => number,
    private applied: (o: LocalObservation) => void,
    private failed: (error: unknown) => void,
    private minInterval = 1000,
    private discarded: (frame: Frame) => void = () => {},
    private outdated: (o: LocalObservation, reason: OutdatedReason) => void = () => {},
  ) {}
  change(revision: number) {
    if (revision <= this.revision) return;
    this.revision = revision;
    this.previous = null;
    this.pending = null;
  }
  offer(frame: Frame, task: Task) {
    if (
      this.closed ||
      frame.source.epoch !== this.epoch ||
      frame.source.calibration_id !== this.calibration ||
      frame.source.revision !== this.revision
    )
      return;
    this.pending = { frame, task };
    void this.pump();
  }
  close() {
    this.closed = true;
    this.pending = null;
    this.previous = null;
    if (this.wake) clearTimeout(this.wake);
  }
  private async pump() {
    if (this.closed || this.busy || !this.pending) return;
    const delay = this.minInterval - (this.now() - this.lastStart);
    if (delay > 0) {
      if (!this.wake)
        this.wake = setTimeout(() => {
          this.wake = null;
          void this.pump();
        }, delay);
      return;
    }
    const job = this.pending;
    this.pending = null;
    if (this.now() - job.frame.source.captured_at > 5000) {
      this.discarded(job.frame);
      void this.pump();
      return;
    }
    this.busy = true;
    this.lastStart = this.now();
    try {
      const o = await job.task(),
        s = job.frame.source;
      if (
        this.closed ||
        s.frame_seq <= this.lastSeq ||
        s.epoch !== this.epoch ||
        s.calibration_id !== this.calibration ||
        JSON.stringify(o.source) !== JSON.stringify(s)
      )
        return;
      if (s.revision !== this.revision || this.now() - s.captured_at > 5000) {
        // A readable historical frame is useful for diagnostics, never current state.
        this.outdated(
          { ...structuredClone(o), status: 'stale' },
          s.revision !== this.revision ? 'changed' : 'expired',
        );
        return;
      }
      this.lastSeq = s.frame_seq;
      const key = JSON.stringify({
        mode: o.ui_mode,
        hand: o.hand_number,
        street: o.ui_street,
        pot: o.pot_display.value,
        board: o.board,
      });
      const hasEvidence =
        o.ui_mode === 'live' && o.pot_display.value !== null && o.hand_number !== null;
      if (o.stable && hasEvidence) {
        if (
          this.previous?.key === key &&
          this.previous.seq !== s.frame_seq &&
          this.now() - this.previous.at <= 5000
        )
          o.evidence = 'repeated';
        this.previous = { key, seq: s.frame_seq, at: s.captured_at };
      } else this.previous = null;
      this.applied(o);
    } catch (error) {
      if (!this.closed) this.failed(error);
    } finally {
      this.busy = false;
      void this.pump();
    }
  }
}
