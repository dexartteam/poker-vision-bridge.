import './style.css';
import { BrowserRecognizer } from '../local/recognizer';
import {
  isPokerStars,
  validateLocalProfile,
  type Frame,
  type LocalObservation,
  type LocalProfile,
} from '../local/contracts';
import { pokerStarsProfile } from '../local/pokerstars/profile';
import { readSymbolSet, type SymbolSet } from '../local/pokerstars/symbols';
import { ReplayController, type ReplaySnapshot, type ReplayState } from './controller';

type ReplayResponse = {
  schema_version: 'replay.response.v1';
  session_id: string;
  request_id: string;
  frame: { source_epoch: string; frame_seq: number; revision: number; media_time: number };
  status:
    | 'missing_data'
    | 'invalid'
    | 'unsupported'
    | 'ready'
    | 'not_configured'
    | 'solved'
    | 'solver_failed';
  issues: { code: string; field: string; message: string; kind: string }[];
  solver: { provider: string; configured: boolean };
  execution: 'disabled';
  draft_observation: Record<string, unknown>;
  normalized_observation: Record<string, unknown> | null;
  solver_request: Record<string, unknown> | null;
  solver_result: {
    strategy?: {
      action: string;
      frequency: number;
      amount_bb?: number | string | null;
      sizing_pot?: number | string | null;
      allin?: boolean;
    }[];
    [key: string]: unknown;
  } | null;
  manual_fields: string[];
  elapsed_ms: number;
};
type DecisionTrace = { request: unknown; diagnose: ReplayResponse; response: ReplayResponse };

document.querySelector('#app')!.innerHTML = `
<main>
  <header><a class="brand" href="${import.meta.env.BASE_URL}">TABLE VISION <span>REPLAY</span></a><a href="${import.meta.env.BASE_URL}">Распознавание и калибровка ↗</a></header>
  <section class="heading"><div><p class="eyebrow">ОТЛАДКА НА ЗАПИСИ</p><h1>Кадр → проверка → решение</h1><p>Видео идёт по шагам по 0,5 секунды. На каждом шаге ждём распознавание, на вашем ходе — ответ солвера. После ответа вы продолжаете вручную.</p></div><span class="badge">POKERSTARS CLASSIC</span></section>
  <div class="workspace">
    <section class="panel">
      <div class="panel-title"><h2>Стол на записи</h2><span id="dimensions" class="hint">Видео не выбрано</span></div>
      <div class="stage"><div id="placeholder"><b>Выберите видео PokerStars</b>Кадры распознаются на этом устройстве.<br>На сервер отправляется JSON распознанных полей.</div><div id="preview" hidden><video id="video" muted playsinline preload="auto"></video><canvas id="overlay"></canvas></div></div>
      <div class="toolbar"><label class="button">Открыть видео<input id="file" type="file" accept="video/*" hidden></label><button id="start" class="primary" disabled>Начать / продолжить</button><button id="pause" disabled>Пауза</button><button id="manual" disabled>Проверить этот кадр</button></div>
      <div class="timeline"><label for="seek">Позиция записи</label><span id="position">0:00.0</span></div><input id="seek" type="range" min="0" max="1" step="0.1" value="0" disabled aria-label="Позиция записи">
      <p id="status" class="status" role="status" aria-live="polite">Выберите видео, загрузите шаблоны карт и проверьте области чтения.</p>
      <p class="hint">Это пошаговый просмотр: скорость зависит от OCR. Если ход не распознан, остановитесь и нажмите «Проверить этот кадр». При перемотке текущий результат сбрасывается.</p>
      <div class="facts"><div><span>Кадр распознан</span><strong id="frame-time">—</strong></div><div><span>Этап</span><strong id="street">—</strong></div><div><span>Банк на экране</span><strong id="pot">—</strong></div></div>
      <div class="cards-row"><div class="board-block"><span>Общие карты</span><div id="board" class="cards"></div></div><div class="board-block"><span>Свои карты</span><div id="hero" class="cards"></div></div></div>
      <p id="hero-turn" class="hint">Ход героя пока не определён.</p>
      <div class="table-scroll"><table><thead><tr><th>Место</th><th>Стек</th><th>Фишки у места</th><th>Действие в кадре</th></tr></thead><tbody id="seats"></tbody></table></div>
      <p class="hint">Сумма у игрока может быть ставкой или выплатой. Пустое поле остаётся неизвестным; история действий не восстанавливается из одного кадра.</p>
    </section>
    <section class="panel">
      <div class="panel-title"><h2>Ответ и пробелы</h2><span id="phase" class="badge">Ожидание</span></div>
      <div class="decision"><h3 id="decision-title" class="decision-title">Ждём первый кадр</h3><p id="decision-note" class="hint">Здесь появятся решение PokerAI и данные, которых не хватает для запроса.</p><div id="strategy" class="strategy"></div></div>
      <div id="server-issues" hidden><h2>Проверка сервера</h2><ul id="issues" class="issues"></ul></div>
      <div id="local-issues" hidden><h2>Что не установлено по кадру</h2><ul id="local-gaps" class="issues"></ul><p class="hint">Это видимые ограничения распознавания. Полную проверку запроса выполняет сервер.</p></div>
      <details><summary>Исходное распознавание JSON</summary><pre id="observation-json">{}</pre></details>
      <details><summary>Запрос к PokerAI</summary><pre id="solver-request-json">null</pre></details>
      <details><summary>Полный ответ проверки / солвера</summary><pre id="response-json">null</pre></details>
      <p id="provider-note" class="hint">Сервис решений ещё не проверен. Ключ PokerAI задаётся на сервере.</p>
    </section>
  </div>
  <div class="settings">
    <section class="panel">
      <div class="panel-title"><h2>Видео и подключение</h2><label class="check"><input id="show-regions" type="checkbox" checked> Области</label></div>
      <div class="toolbar"><label class="button">Импорт калибровки<input id="import-profile" type="file" accept="application/json" hidden></label><label class="button">Шаблоны карт<input id="import-cards" type="file" accept="application/json" hidden></label></div>
      <p id="profile-note" class="hint">Профиль PokerStars classic, 6 мест. Для настройки областей откройте основной экран распознавания и экспортируйте калибровку.</p><p id="templates-note" class="hint">Шаблоны не загружены: карты останутся неизвестными. Выберите pokerstars-symbols.json из набора для этой записи.</p>
      <label class="check"><input id="calibrated" type="checkbox"> Области совпадают с полями этого видео</label>
      <label class="field">Адрес сервера проверки<input id="backend" type="url" placeholder="Пусто — сервер этого сайта" autocomplete="off"></label>
      <label class="field">Токен доступа к серверу (RECEIVER_TOKEN)<input id="receiver-token" type="password" placeholder="Если сервер требует авторизацию" autocomplete="off"></label>
      <p class="hint">Для статического сайта нужен отдельно запущенный сервер с PokerAI API key и разрешённым адресом этого сайта. Токен хранится только в памяти этой вкладки.</p>
    </section>
    <section class="panel">
      <div class="panel-title"><h2>Дополнить текущий кадр</h2><span class="badge">РУЧНАЯ ПРОВЕРКА</span></div>
      <p class="hint">После проверки здесь появится черновик игровых полей. Заполните неизвестные значения по записи. Сервер сверит уже распознанные значения; пропуски и конфликтующие данные остановят запрос.</p>
      <label class="field" for="context">Игровые поля JSON — неизвестное оставляйте null</label><textarea id="context" rows="12" spellcheck="false" aria-label="Ручные игровые поля JSON"></textarea>
      <label class="check"><input id="context-confirmed" type="checkbox" disabled> Подтверждаю эти поля именно для показанного кадра</label>
      <label class="check"><input id="history-confirmed" type="checkbox" disabled> Проверил полную историю действий с начала этой раздачи</label>
      <div class="toolbar"><button id="retry" disabled>Отправить дополненный кадр</button><button id="reset-context" disabled>Вернуть черновик</button></div>
      <p id="context-note" class="hint">Ручное подтверждение сбрасывается при переходе к другому кадру.</p>
    </section>
  </div>
  <section class="panel history"><div class="panel-title"><h2>История остановок</h2><button id="download" disabled>Скачать журнал JSON</button></div><p class="hint">Журнал хранит последние 200 проверенных кадров: время, исходные поля, ручные дополнения, проверку и ответ сервиса. Скачивайте его до открытия следующего видео. Токен доступа в журнал не попадает.</p><div class="table-scroll"><table><thead><tr><th>Время записи</th><th>Причина</th><th>Результат</th><th>Ручные поля</th></tr></thead><tbody id="history"></tbody></table></div></section>
  <footer>Офлайн-разбор записи · Решения не исполняются · Неизвестные значения не подставляются</footer>
</main>`;

const el = <T extends HTMLElement>(id: string) => document.getElementById(id) as T;
const video = el<HTMLVideoElement>('video');
const overlay = el<HTMLCanvasElement>('overlay');
const sessionId = crypto.randomUUID();
let profile: LocalProfile = pokerStarsProfile();
let symbols: SymbolSet | null = null;
let sourceUrl = '';
let loaded = false;
let loading = false;
let sourceGeneration = 0;
let shownFrameId: string | null = null;
let lastResponse: ReplayResponse | null = null;
let responseFrameId: string | null = null;
let confirmedFrameId: string | null = null;
let recognizerEntry: { recognizer: BrowserRecognizer; ready: Promise<void> } | null = null;
const nullContext = {
  hand_id: null,
  dealer_seat: null,
  acting_seat: null,
  blinds: null,
  hero: null,
  pot: null,
  seats: null,
  action_history: null,
  legal_actions: null,
  turn_id: null,
  pokerai: null,
};
const actionLabels: Record<string, string> = {
  fold: 'Пас',
  check: 'Чек',
  call: 'Колл',
  bet: 'Ставка',
  raise: 'Рейз',
  all_in: 'Олл-ин',
  allin: 'Олл-ин',
  post_sb: 'Малый блайнд',
  post_bb: 'Большой блайнд',
};
const statusLabels: Record<ReplayResponse['status'], string> = {
  missing_data: 'Не хватает данных',
  invalid: 'Есть противоречия',
  unsupported: 'Ситуация не поддерживается',
  ready: 'Запрос готов',
  not_configured: 'Солвер не настроен',
  solved: 'Решение получено',
  solver_failed: 'Ошибка солвера',
};
const phaseLabels: Record<ReplayState['phase'], string> = {
  idle: 'Ожидание',
  seeking: 'Следующий кадр',
  recognizing: 'Распознаём',
  solving: 'Проверка / солвер',
  decision: 'На паузе',
  paused: 'Пауза',
  ended: 'Конец записи',
  error: 'Ошибка',
  disposed: 'Остановлено',
};
const activePhases = new Set<ReplayState['phase']>(['seeking', 'recognizing', 'solving']);
const formatTime = (t: number) => `${Math.floor(t / 60)}:${(t % 60).toFixed(1).padStart(4, '0')}`;
const json = (value: unknown) => JSON.stringify(value, null, 2);
const message = (value: unknown) => (value instanceof Error ? value.message : String(value));

function clearConfirmation() {
  confirmedFrameId = null;
  el<HTMLInputElement>('context-confirmed').checked = false;
  el<HTMLInputElement>('history-confirmed').checked = false;
}
function resetDecision() {
  lastResponse = null;
  responseFrameId = null;
  el('decision-title').textContent = 'Ждём проверку кадра';
  el('decision-note').textContent = 'Результат относится только к текущему неподвижному кадру.';
  el('strategy').replaceChildren();
  el('server-issues').hidden = true;
  el('solver-request-json').textContent = 'null';
  el('response-json').textContent = 'null';
}
function clearObservation() {
  shownFrameId = null;
  clearConfirmation();
  resetDecision();
  for (const id of ['frame-time', 'street', 'pot']) el(id).textContent = '—';
  el('observation-json').textContent = '{}';
  el('hero-turn').textContent = 'Ход героя пока не определён.';
  el('seats').replaceChildren();
  el('local-issues').hidden = true;
  renderCards([], 'board', 5);
  renderCards([], 'hero', 2);
  el<HTMLTextAreaElement>('context').value = json(nullContext);
}
function renderCards(cards: LocalObservation['board'], id: string, count: number) {
  const suits: Record<string, string> = { c: '♣', d: '♦', h: '♥', s: '♠' };
  el(id).replaceChildren(
    ...Array.from({ length: count }, (_, i) => {
      const card = cards[i];
      const item = document.createElement('span');
      item.className = 'card';
      if (card?.status === 'visible' && card.value) {
        item.textContent = card.value[0] + suits[card.value[1]];
        if ('dh'.includes(card.value[1])) item.classList.add('red');
      } else {
        item.textContent = card?.status === 'empty' ? '—' : '?';
        item.classList.add('unknown');
      }
      return item;
    }),
  );
}
function renderObservation(snapshot: ReplaySnapshot) {
  const o = snapshot.observation;
  if (!o) return;
  if (shownFrameId !== snapshot.id) {
    clearConfirmation();
    resetDecision();
    el<HTMLTextAreaElement>('context').value = json(nullContext);
  }
  shownFrameId = snapshot.id;
  el('frame-time').textContent = formatTime(snapshot.mediaTime);
  el('street').textContent = {
    preflop: 'Префлоп',
    flop: 'Флоп',
    turn: 'Тёрн',
    river: 'Ривер',
    showdown: 'Вскрытие',
    unknown: '—',
  }[o.ui_street];
  el('pot').textContent = o.pot_display.value?.toLocaleString('ru') ?? '—';
  el('observation-json').textContent = json(o);
  renderCards(o.board, 'board', 5);
  if (o.schema_version === 'local-vision.observation.v2') {
    renderCards(o.hero_cards, 'hero', 2);
    el('hero-turn').textContent =
      o.hero_turn === true
        ? `Ваш ход: ${o.available_actions.map((a) => actionLabels[a]).join(', ') || 'действия неизвестны'}. Видео остановлено для проверки.`
        : 'Ход героя не подтверждён. При необходимости нажмите «Проверить этот кадр».';
    el('seats').replaceChildren(
      ...o.seats.map((seat) =>
        row([
          `${seat.seat_index + 1} · ${seat.is_hero ? 'Вы' : (seat.name ?? 'Игрок')}`,
          seat.stack?.toLocaleString('ru') ?? (seat.status === 'all_in' ? 'Олл-ин' : '—'),
          seat.chip_display?.toLocaleString('ru') ??
            (seat.chip_visibility === 'empty' ? 'Не видны' : '—'),
          seat.action_label ? actionLabels[seat.action_label] : '—',
        ]),
      ),
    );
  }
  const gaps = [
    ...(o.hand_number === null ? ['Идентификатор раздачи не прочитан.'] : []),
    ...(o.ui_street === 'unknown' ? ['Этап раздачи не установлен.'] : []),
    ...(o.board.some((c) => c.status === 'unreadable') ? ['Часть общих карт не распознана.'] : []),
    ...(o.schema_version === 'local-vision.observation.v2' &&
    o.hero_cards.some((c) => c.status !== 'visible')
      ? ['Свои карты распознаны не полностью.']
      : []),
    ...(o.schema_version !== 'local-vision.observation.v2' || o.dealer_seat === null
      ? ['Место дилера и позиции игроков не установлены.']
      : []),
    'Блайнды не входят в текущее распознавание.',
    'Требуется отличить ставки игроков от выплат и подтвердить состав банка.',
    'Полная история действий с начала раздачи не восстановлена.',
    'Размер колла и допустимые размеры ставок не прочитаны.',
    ...(o.hero_turn !== true ? ['Не подтверждено, что сейчас ход героя.'] : []),
  ];
  el('local-gaps').replaceChildren(
    ...gaps.map((text) => {
      const li = document.createElement('li');
      li.textContent = text;
      return li;
    }),
  );
  el('local-issues').hidden = false;
}
function row(values: string[]) {
  const tr = document.createElement('tr');
  for (const value of values) {
    const td = document.createElement('td');
    td.textContent = value;
    tr.append(td);
  }
  return tr;
}
function isTrace(value: unknown): value is DecisionTrace {
  return !!value && typeof value === 'object' && 'response' in value;
}
function renderHistory(state: ReplayState) {
  el('history').replaceChildren(
    ...state.history
      .filter((s) => s.trigger !== null)
      .slice()
      .reverse()
      .map((s) => {
        const trace = isTrace(s.result) ? s.result : null;
        return row([
          formatTime(s.mediaTime),
          s.trigger === 'hero_turn' ? 'Ход героя' : 'Ручная проверка',
          trace
            ? statusLabels[trace.response.status]
            : s.issues.length
              ? s.issues.join('; ')
              : 'Обработка',
          trace?.response.manual_fields?.join(', ') || '—',
        ]);
      }),
  );
}
function renderResponse(response: ReplayResponse, snapshot: ReplaySnapshot) {
  lastResponse = response;
  responseFrameId = snapshot.id;
  el('decision-title').textContent = statusLabels[response.status];
  el('decision-note').textContent =
    response.status === 'solved'
      ? `Кадр ${formatTime(snapshot.mediaTime)}. Сравните ответ со столом; затем нажмите «Начать / продолжить».`
      : response.status === 'ready'
        ? 'Поля прошли проверку. Запрашиваем реальное решение PokerAI…'
        : 'Видео на паузе. Проверьте недостающие поля ниже и дополните текущий кадр.';
  el('server-issues').hidden = false;
  el('issues').replaceChildren(
    ...response.issues.map((issue) => {
      const li = document.createElement('li');
      li.textContent = issue.message;
      const code = document.createElement('code');
      code.textContent = `${issue.field} · ${issue.code}`;
      li.append(code);
      return li;
    }),
  );
  if (!response.issues.length) {
    const li = document.createElement('li');
    li.textContent = 'Пробелов в запросе не обнаружено.';
    el('issues').append(li);
  }
  el('solver-request-json').textContent = json(response.solver_request);
  el('response-json').textContent = json(response);
  el('provider-note').textContent =
    `${response.solver.provider} · ${response.solver.configured ? 'настроен на сервере' : 'ключ сервиса не настроен'} · ${response.elapsed_ms} мс. Игровые команды отключены.`;
  el('strategy').replaceChildren();
  for (const option of response.status === 'solved'
    ? (response.solver_result?.strategy ?? [])
    : []) {
    if (!Number.isFinite(option.frequency) || option.frequency < 0 || option.frequency > 1)
      continue;
    const item = document.createElement('div');
    item.className = 'strategy-row';
    const name = document.createElement('span');
    name.textContent = `${actionLabels[option.action] ?? option.action}${option.allin ? ' · олл-ин' : ''}${option.amount_bb != null ? ` · ${option.amount_bb} BB` : option.sizing_pot != null ? ` · ${option.sizing_pot} банка` : ''}`;
    const bar = document.createElement('span');
    bar.className = 'strategy-bar';
    const fill = document.createElement('span');
    fill.style.width = `${Math.max(0, Math.min(100, option.frequency * 100))}%`;
    bar.append(fill);
    const frequency = document.createElement('strong');
    frequency.textContent = `${(option.frequency * 100).toFixed(1)}%`;
    item.append(name, bar, frequency);
    el('strategy').append(item);
  }
  if (!el<HTMLInputElement>('context-confirmed').checked)
    el<HTMLTextAreaElement>('context').value = json(response.draft_observation);
  el('context-note').textContent =
    `Ручные поля относятся к кадру ${formatTime(snapshot.mediaTime)}. ${response.manual_fields.length ? `Использованы: ${response.manual_fields.join(', ')}.` : 'Дополнений пока нет.'}`;
}
function buttons(state: ReplayState) {
  const busy = activePhases.has(state.phase) || loading;
  const ready = loaded && el<HTMLInputElement>('calibrated').checked;
  el<HTMLButtonElement>('start').disabled = !ready || busy || state.phase === 'ended';
  el<HTMLButtonElement>('manual').disabled = !ready || busy;
  el<HTMLButtonElement>('pause').disabled = !busy || loading;
  el<HTMLInputElement>('seek').disabled = !loaded || loading;
  for (const id of [
    'file',
    'import-profile',
    'import-cards',
    'calibrated',
    'backend',
    'receiver-token',
  ])
    (el(id) as HTMLInputElement).disabled = busy;
  const frameReady = !!state.latest?.observation && shownFrameId === state.latest.id && !busy;
  el<HTMLInputElement>('context-confirmed').disabled = !frameReady;
  el<HTMLInputElement>('history-confirmed').disabled = !frameReady;
  el<HTMLTextAreaElement>('context').disabled = busy;
  el<HTMLButtonElement>('retry').disabled =
    !frameReady || !el<HTMLInputElement>('context-confirmed').checked;
  el<HTMLButtonElement>('reset-context').disabled =
    !frameReady || !lastResponse || responseFrameId !== state.latest?.id;
  el<HTMLButtonElement>('download').disabled = !state.history.length;
}
function change(state: ReplayState) {
  // The old frame must disappear before the video moves, including while the next OCR is pending.
  if (
    shownFrameId !== null &&
    (state.phase === 'seeking' || !state.latest || state.latest.id !== shownFrameId)
  )
    clearObservation();
  // A retry may use changed manual fields; a previous solution is no longer its answer.
  if (state.phase === 'solving') resetDecision();
  el('phase').textContent = phaseLabels[state.phase];
  const descriptions: Partial<Record<ReplayState['phase'], string>> = {
    recognizing: 'Распознаём неподвижный кадр. При первом запуске загружается OCR-модель…',
    seeking: 'Переходим к следующему кадру записи…',
    solving: 'Видео на паузе. Проверяем данные и ждём ответ сервиса…',
    decision: 'Кадр проверен. Сравните данные со столом, дополните пробелы или продолжите запись.',
    paused: 'Видео на паузе. Можно проверить этот кадр или продолжить.',
    ended: 'Запись закончилась. Журнал доступен для скачивания.',
  };
  if (state.error) {
    el('status').textContent = state.error;
    el('decision-title').textContent = 'Проверка не завершена';
    el('decision-note').textContent = state.error;
    el('provider-note').textContent =
      'Локальные поля показаны. Ответ сервера недоступен; решение не получено.';
  } else if (descriptions[state.phase]) el('status').textContent = descriptions[state.phase]!;
  renderHistory(state);
  buttons(state);
}

function seek(time: number, signal: AbortSignal): Promise<void> {
  video.pause();
  if (signal.aborted) return Promise.reject(new DOMException('Отменено', 'AbortError'));
  if (Math.abs(video.currentTime - time) < 0.0001 && video.readyState >= 2 && !video.seeking)
    return Promise.resolve();
  return new Promise((resolve, reject) => {
    const finish = (error?: unknown) => {
      video.removeEventListener('seeked', done);
      video.removeEventListener('error', failed);
      signal.removeEventListener('abort', cancelled);
      if (error) reject(error);
      else resolve();
    };
    const done = () =>
      video.readyState >= 2 && !video.seeking && Math.abs(video.currentTime - time) < 0.001
        ? finish()
        : finish(new Error('Кадр не декодирован'));
    const failed = () => finish(new Error('Не удалось открыть кадр записи'));
    const cancelled = () => finish(new DOMException('Отменено', 'AbortError'));
    video.addEventListener('seeked', done, { once: true });
    video.addEventListener('error', failed, { once: true });
    signal.addEventListener('abort', cancelled, { once: true });
    if (Math.abs(video.currentTime - time) >= 0.0001 || !video.seeking) video.currentTime = time;
  });
}
async function closeRecognizer() {
  const old = recognizerEntry;
  recognizerEntry = null;
  if (old) await old.recognizer.close();
}
async function recognize(frame: Frame, signal: AbortSignal): Promise<LocalObservation> {
  if (!recognizerEntry) {
    const recognizer = new BrowserRecognizer([], symbols);
    recognizerEntry = { recognizer, ready: recognizer.start() };
  }
  const entry = recognizerEntry;
  const abort = () => {
    if (recognizerEntry === entry) recognizerEntry = null;
    void entry.recognizer.close();
  };
  signal.addEventListener('abort', abort, { once: true });
  try {
    if (signal.aborted) throw new DOMException('Отменено', 'AbortError');
    await entry.ready;
    if (signal.aborted) throw new DOMException('Отменено', 'AbortError');
    return await entry.recognizer.recognize(frame, structuredClone(profile));
  } finally {
    signal.removeEventListener('abort', abort);
  }
}
function capture(context: { epoch: string; frameSeq: number; mediaTime: number }) {
  if (!video.paused || video.seeking || video.readyState < 2)
    throw new Error('Нужен декодированный неподвижный кадр');
  const canvas = document.createElement('canvas');
  canvas.width = video.videoWidth;
  canvas.height = video.videoHeight;
  const ctx = canvas.getContext('2d', { willReadFrequently: true })!;
  ctx.drawImage(video, 0, 0);
  const pixels = ctx.getImageData(0, 0, canvas.width, canvas.height);
  const frame: Frame = {
    source: {
      kind: 'recording',
      epoch: context.epoch,
      calibration_id: profile.id,
      frame_seq: context.frameSeq,
      revision: context.frameSeq,
      captured_at: Date.now(),
      media_time: context.mediaTime,
    },
    stable: true,
    pixels: { width: canvas.width, height: canvas.height, data: pixels.data },
  };
  return { frame };
}
async function request(
  action: 'diagnose' | 'solve',
  snapshot: ReplaySnapshot,
  signal: AbortSignal,
  context: unknown,
) {
  const backend = el<HTMLInputElement>('backend').value.trim().replace(/\/+$/, '');
  if (backend) {
    const url = new URL(backend);
    if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password)
      throw new Error('Укажите HTTP(S)-адрес сервера без пароля в URL');
  }
  const requestId = crypto.randomUUID();
  const body = {
    schema_version: 'replay.request.v1',
    session_id: sessionId,
    request_id: requestId,
    action,
    observation: snapshot.observation,
    ...(context ? { confirmed_context: context } : {}),
  };
  const token = el<HTMLInputElement>('receiver-token').value.trim();
  let response: Response;
  try {
    response = await fetch(`${backend}/v1/replay/analyze`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
      },
      body: json(body),
      signal,
      credentials: 'omit',
    });
  } catch (error) {
    if (signal.aborted) throw error;
    throw new Error(
      'Сервер проверки недоступен. Проверьте его адрес, запуск и CORS. Распознавание кадра показано; решения солвера нет.',
    );
  }
  const raw = await response.text();
  if (!response.ok) throw new Error(`Сервер: HTTP ${response.status}. ${raw.slice(0, 500)}`);
  let result: ReplayResponse;
  try {
    result = JSON.parse(raw) as ReplayResponse;
  } catch {
    throw new Error('Сервер вернул не JSON. Проверьте адрес сервера проверки.');
  }
  const source = snapshot.observation!.source;
  if (
    result.schema_version !== 'replay.response.v1' ||
    result.session_id !== sessionId ||
    result.request_id !== requestId ||
    result.frame?.source_epoch !== source.epoch ||
    result.frame?.frame_seq !== source.frame_seq ||
    result.frame?.revision !== source.revision ||
    result.frame?.media_time !== source.media_time
  )
    throw new Error('Ответ сервера относится к другому кадру или версии протокола');
  return { body, result };
}
async function handleDecision(
  snapshot: ReplaySnapshot,
  signal: AbortSignal,
): Promise<DecisionTrace> {
  if (snapshot.observation?.schema_version !== 'local-vision.observation.v2')
    throw new Error('Для этого режима нужен профиль PokerStars classic');
  const source = snapshot.observation.source;
  let context: unknown = undefined;
  if (el<HTMLInputElement>('context-confirmed').checked) {
    if (confirmedFrameId !== snapshot.id)
      throw new Error('Ручные поля относятся к другому кадру. Подтвердите текущий кадр заново.');
    let values: unknown;
    try {
      values = JSON.parse(el<HTMLTextAreaElement>('context').value);
    } catch {
      throw new Error('Исправьте JSON ручных полей перед отправкой.');
    }
    if (!values || typeof values !== 'object' || Array.isArray(values))
      throw new Error('Ручные поля должны быть JSON-объектом.');
    context = {
      provenance: 'manual',
      source_epoch: source.epoch,
      frame_seq: source.frame_seq,
      revision: source.revision,
      media_time: source.media_time,
      values,
      history_confirmed: el<HTMLInputElement>('history-confirmed').checked,
    };
  }
  const diagnostic = await request('diagnose', snapshot, signal, context);
  if (signal.aborted) throw new DOMException('Отменено', 'AbortError');
  renderResponse(diagnostic.result, snapshot);
  if (diagnostic.result.status !== 'ready')
    return { request: diagnostic.body, diagnose: diagnostic.result, response: diagnostic.result };
  const solved = await request('solve', snapshot, signal, context);
  if (signal.aborted) throw new DOMException('Отменено', 'AbortError');
  renderResponse(solved.result, snapshot);
  return { request: solved.body, diagnose: diagnostic.result, response: solved.result };
}
const controller = new ReplayController(
  {
    duration: () => video.duration,
    currentTime: () => video.currentTime,
    pause: () => video.pause(),
    seek,
    capture,
    recognize,
    handleDecision,
  },
  {
    change,
    observation: renderObservation,
    decision: (snapshot) => {
      if (isTrace(snapshot.result)) renderResponse(snapshot.result.response, snapshot);
    },
  },
  { stepSeconds: 0.5, decisionTimeoutMs: 120000 },
);

function drawOverlay() {
  const ctx = overlay.getContext('2d')!;
  ctx.clearRect(0, 0, overlay.width, overlay.height);
  if (!el<HTMLInputElement>('show-regions').checked) return;
  ctx.strokeStyle = '#c6e6a6';
  ctx.lineWidth = Math.max(1, overlay.width / 700);
  for (const [name, rect] of Object.entries(profile.regions)) {
    if (name === 'chat') continue;
    ctx.strokeRect(
      rect.x * overlay.width,
      rect.y * overlay.height,
      rect.w * overlay.width,
      rect.h * overlay.height,
    );
  }
}
function updateTime() {
  el<HTMLInputElement>('seek').value = String(video.currentTime || 0);
  el('position').textContent =
    `${formatTime(video.currentTime || 0)} / ${formatTime(Number.isFinite(video.duration) ? video.duration : 0)}`;
}
async function guard(action: () => void | Promise<void>) {
  try {
    await action();
  } catch (error) {
    el('status').textContent = message(error);
  }
}
el<HTMLInputElement>('file').onchange = () =>
  void guard(async () => {
    const file = el<HTMLInputElement>('file').files?.[0];
    if (!file) return;
    loading = true;
    loaded = false;
    const generation = ++sourceGeneration;
    controller.reset();
    await closeRecognizer();
    clearObservation();
    buttons(controller.state);
    video.pause();
    video.removeAttribute('src');
    if (sourceUrl) URL.revokeObjectURL(sourceUrl);
    sourceUrl = URL.createObjectURL(file);
    video.src = sourceUrl;
    try {
      await new Promise<void>((resolve, reject) => {
        const cleanup = () => {
          clearTimeout(timeout);
          video.removeEventListener('loadeddata', ready);
          video.removeEventListener('error', failed);
        };
        const ready = () => {
          cleanup();
          resolve();
        };
        const failed = () => {
          cleanup();
          reject(new Error('Браузер не смог открыть видео. Используйте MP4 (H.264).'));
        };
        const timeout = setTimeout(failed, 20000);
        video.addEventListener('loadeddata', ready, { once: true });
        video.addEventListener('error', failed, { once: true });
        video.load();
      });
      if (generation !== sourceGeneration) return;
      if (!Number.isFinite(video.duration) || video.duration <= 0)
        throw new Error('Длительность записи недоступна');
      loaded = true;
      profile = pokerStarsProfile(video.videoWidth, video.videoHeight);
      overlay.width = video.videoWidth;
      overlay.height = video.videoHeight;
      el('placeholder').hidden = true;
      el('preview').hidden = false;
      el('dimensions').textContent = `${video.videoWidth} × ${video.videoHeight}`;
      el<HTMLInputElement>('seek').max = String(video.duration);
      el<HTMLInputElement>('calibrated').checked = false;
      el('status').textContent =
        'Видео открыто. Проверьте области, подтвердите их совпадение и начните просмотр.';
      drawOverlay();
      updateTime();
    } finally {
      loading = false;
      buttons(controller.state);
    }
  });
el('start').onclick = () =>
  void guard(() => {
    clearConfirmation();
    return controller.state.latest?.observation ? controller.continue() : controller.start();
  });
el('pause').onclick = () => {
  controller.pause();
};
el('manual').onclick = () =>
  void guard(() => {
    clearConfirmation();
    return controller.manualAnalyze();
  });
el('retry').onclick = () => void guard(() => controller.retryDecision());
el<HTMLInputElement>('seek').oninput = () =>
  void guard(async () => {
    clearObservation();
    await controller.seek(Number(el<HTMLInputElement>('seek').value));
    updateTime();
  });
video.ontimeupdate = updateTime;
video.onplay = () => video.pause();
video.onerror = () => {
  if (loaded) {
    controller.pause();
    el('status').textContent = 'Ошибка декодирования видео';
  }
};
el('show-regions').onchange = drawOverlay;
el('calibrated').onchange = () => buttons(controller.state);
el<HTMLInputElement>('import-profile').onchange = () =>
  void guard(async () => {
    const file = el<HTMLInputElement>('import-profile').files?.[0];
    if (!file) return;
    if (file.size > 100000) throw new Error('Файл калибровки слишком большой');
    const imported = validateLocalProfile(JSON.parse(await file.text()));
    if (!isPokerStars(imported)) throw new Error('Нужна калибровка PokerStars classic');
    if (loaded && (imported.width !== video.videoWidth || imported.height !== video.videoHeight))
      throw new Error('Размер калибровки не совпадает с видео');
    controller.reset();
    await closeRecognizer();
    profile = imported;
    profile.id = crypto.randomUUID();
    clearObservation();
    el<HTMLInputElement>('calibrated').checked = false;
    el('profile-note').textContent =
      `Калибровка загружена: ${file.name}. Проверьте рамки на видео.`;
    drawOverlay();
    buttons(controller.state);
  });
el<HTMLInputElement>('import-cards').onchange = () =>
  void guard(async () => {
    const file = el<HTMLInputElement>('import-cards').files?.[0];
    if (!file) return;
    if (file.size > 500000) throw new Error('Файл шаблонов слишком большой');
    const next = readSymbolSet(JSON.parse(await file.text()));
    controller.reset();
    await closeRecognizer();
    symbols = next;
    clearObservation();
    el('templates-note').textContent = `Шаблоны PokerStars загружены: ${file.name}.`;
    buttons(controller.state);
  });
el('context').oninput = () => {
  clearConfirmation();
  buttons(controller.state);
};
el('context-confirmed').onchange = () => {
  confirmedFrameId = el<HTMLInputElement>('context-confirmed').checked
    ? (controller.state.latest?.id ?? null)
    : null;
  buttons(controller.state);
};
el('reset-context').onclick = () => {
  if (lastResponse && responseFrameId === controller.state.latest?.id)
    el<HTMLTextAreaElement>('context').value = json(lastResponse.draft_observation);
  clearConfirmation();
  buttons(controller.state);
};
el('download').onclick = () => {
  const data = {
    ...controller.recording(),
    exported_at: new Date().toISOString(),
    source_name: el<HTMLInputElement>('file').files?.[0]?.name ?? null,
  };
  const href = URL.createObjectURL(new Blob([json(data)], { type: 'application/json' }));
  const link = document.createElement('a');
  link.href = href;
  link.download = 'poker-replay-diagnostics.json';
  link.click();
  setTimeout(() => URL.revokeObjectURL(href), 1000);
};
document.addEventListener('visibilitychange', () => {
  if (document.hidden) controller.pause();
});
window.addEventListener('pagehide', () => {
  controller.dispose();
  void closeRecognizer();
  if (sourceUrl) URL.revokeObjectURL(sourceUrl);
});
clearObservation();
buttons(controller.state);
