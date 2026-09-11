/** Drain the queued pause event before a controller attaches its stop listeners. */
export async function pauseBeforeRecognition(
  video: Pick<HTMLMediaElement, 'paused' | 'pause'> & EventTarget,
  stillCurrent: () => boolean,
): Promise<boolean> {
  if (!video.paused) {
    await new Promise<void>((resolve) => {
      video.addEventListener('pause', () => resolve(), { once: true });
      video.pause();
    });
  }
  return stillCurrent();
}
