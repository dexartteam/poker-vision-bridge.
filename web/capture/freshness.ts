/** A repeated display buffer is not a new camera observation. */
export class VideoFreshness {
  private serial = 0;
  private used = 0;
  private at = Date.now();
  private callback = 0;
  private closed = false;
  constructor(private video: HTMLVideoElement) {
    if (!video.requestVideoFrameCallback)
      throw new Error('Для наблюдения обновите браузер: нужен requestVideoFrameCallback');
    const next = () => {
      if (this.closed) return;
      this.callback = video.requestVideoFrameCallback(() => {
        this.serial++;
        this.at = Date.now();
        next();
      });
    };
    next();
  }
  take(now: number): number | null {
    if (now - this.at > 2000) throw new Error('Видеопоток остановился. Перезапустите наблюдение.');
    if (this.serial === this.used) return null;
    this.used = this.serial;
    return this.at;
  }
  close() {
    this.closed = true;
    this.video.cancelVideoFrameCallback(this.callback);
  }
}
