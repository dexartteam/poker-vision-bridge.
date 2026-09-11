/** Deliberately synthetic visual source for local detector testing; not a recognition fixture. */
export function drawDemo(canvas: HTMLCanvasElement, step: number) {
  const c = canvas.getContext('2d')!,
    w = canvas.width,
    h = canvas.height;
  c.fillStyle = '#18231f';
  c.fillRect(0, 0, w, h);
  c.fillStyle = '#304e41';
  c.beginPath();
  c.ellipse(w * 0.5, h * 0.47, w * 0.4, h * 0.34, 0, 0, Math.PI * 2);
  c.fill();
  c.strokeStyle = '#7d8973';
  c.lineWidth = 5;
  c.stroke();
  c.textAlign = 'center';
  c.fillStyle = '#baccc0';
  c.font = `${h * 0.02}px sans-serif`;
  c.fillText('SYNTHETIC TABLE · SIX SEATS', w * 0.5, h * 0.19);
  c.fillStyle = '#eef5ef';
  c.font = `500 ${h * 0.035}px sans-serif`;
  c.fillText(`POT  ${step % 2 ? '120' : '80'}`, w * 0.5, h * 0.31);
  for (let i = 0; i < 5; i++) {
    c.fillStyle = step > 1 && i < 3 ? '#f9f7ef' : '#203c30';
    c.fillRect(w * (0.31 + i * 0.08), h * 0.4, w * 0.065, h * 0.14);
    if (step > 1 && i < 3) {
      c.fillStyle = i === 1 ? '#a73535' : '#193528';
      c.font = `${h * 0.047}px serif`;
      c.fillText(['A♠', '9♥', '4♣'][i], w * (0.342 + i * 0.08), h * 0.49);
    }
  }
  const seats = [
    [0.49, 0.9],
    [0.14, 0.66],
    [0.16, 0.18],
    [0.49, 0.1],
    [0.85, 0.18],
    [0.87, 0.66],
  ];
  seats.forEach(([x, y], i) => {
    c.fillStyle = '#172620';
    c.fillRect(w * (x - 0.08), h * (y - 0.06), w * 0.16, h * 0.1);
    c.fillStyle = '#e0e8df';
    c.font = `${h * 0.022}px sans-serif`;
    c.fillText(`SEAT ${i}  ·  1 000`, w * x, h * y);
  });
  c.fillStyle = '#f9f7ef';
  c.fillRect(w * 0.41, h * 0.7, w * 0.07, h * 0.12);
  c.fillRect(w * 0.49, h * 0.7, w * 0.07, h * 0.12);
  c.fillStyle = '#163325';
  c.font = `${h * 0.04}px serif`;
  c.fillText('K♠', w * 0.445, h * 0.78);
  c.fillText('Q♣', w * 0.525, h * 0.78);
  if (step % 2) {
    c.fillStyle = '#accbae';
    c.fillRect(w * 0.66, h * 0.87, w * 0.29, h * 0.09);
    c.fillStyle = '#172820';
    c.font = `${h * 0.025}px sans-serif`;
    c.fillText('FOLD     CALL     RAISE', w * 0.805, h * 0.925);
  }
}
