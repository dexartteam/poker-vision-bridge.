export class Camera {
  private stream: MediaStream | null = null;
  private generation = 0;
  async start(video: HTMLVideoElement, deviceId?: string): Promise<MediaTrackSettings> {
    this.stop();
    const generation = this.generation;
    if (!navigator.mediaDevices?.getUserMedia)
      throw new Error('Камера доступна через HTTPS или localhost');
    const stream = await navigator.mediaDevices.getUserMedia({
      audio: false,
      video: {
        width: { ideal: 1920 },
        height: { ideal: 1080 },
        frameRate: { ideal: 30 },
        ...(deviceId ? { deviceId: { exact: deviceId } } : {}),
      },
    });
    if (generation !== this.generation) {
      stream.getTracks().forEach((t) => t.stop());
      throw new Error('camera_start_cancelled');
    }
    this.stream = stream;
    video.srcObject = stream;
    try {
      await video.play();
    } catch (e) {
      this.stop();
      throw e;
    }
    return stream.getVideoTracks()[0].getSettings();
  }
  stop() {
    this.generation++;
    this.stream?.getTracks().forEach((t) => t.stop());
    this.stream = null;
  }
}
