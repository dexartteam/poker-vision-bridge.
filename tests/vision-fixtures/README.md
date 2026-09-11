# Fixtures from the supplied Open Poker recording

The three JSON data files are private local inputs, excluded from Git.
The runtime site does not require them. Unpack `table-vision-recording-fixtures.zip`
into the repository root; it creates `tests/vision-fixtures/frames.json` and
`tests/vision-fixtures/cards.json`, plus `tests/vision-fixtures/templates.json`. Then run `npm run test:recording`, which fails
explicitly if any file is missing. Ordinary `npm test` reports two skipped
recording tests when private data is absent; the blank-image test still runs.
Public CI does not measure OCR accuracy on this recording. Card templates are also private and loaded through the site's «Загрузить шаблоны карт»
button. Select `tests/vision-fixtures/templates.json`; no file is uploaded to a server.
Text OCR works without this file. Card recognition remains unknown until it is loaded.

Source: user-supplied `ScreenRecording_09-11-2026 19-34-33_1.MOV`,
1206 × 2622, 52.548345 seconds. These are small UI crops from hand #4812,
not a copy of the video or a general poker dataset. No camera/network is used in tests.

`frames.json` contains manually checked values at 2, 18, 22, 26, 30, 34, 38,
42, 46 and 50 seconds. OCR regions are PNG/base64; board pixels are zlib/base64
RGBA. The board crop is x=330, y=1160, width=560, height=160. Coordinates of all
OCR regions are in `openPokerProfile()` in `web/local/contracts.ts`.

The first eight frames are LIVE; the last two are REPLAY/SHOWDOWN. Pot labels:
30, 116, 174, 232, 399, 733, 1094, 1288, then two nulls. AWARDED 1288 at 50 seconds
is a payout, not a current pot. At 18 seconds FLOP is already printed while cards
have not appeared. At 26 seconds the fourth card is still moving. At 30–38 seconds
a bet overlay hides the leftmost card. Tests must preserve these disagreements.

`cards.json` contains 14 annotated RGBA crops. Only the nine Replay crops at
50 seconds train `tests/vision-fixtures/templates.json`. Other crops and full board regions
from Live frames check different-frame regression. This is the same hand and art:
it is NOT an independent test set or an estimate of generalization accuracy.
Known faces: 6c Qd Tc 2h Kh Ah Th Kc Qc. All spades and 43 of 52 faces are absent.

Rebuild with `npx vite-node scripts/build-card-templates.ts`. Run
`npm run test:recording` to run the actual Tesseract.js 7.0.0
English model (`@tesseract.js-data/eng` 1.0.0, best_int) and the real card classifier.
Pure preprocessing is shared by the browser and Node test. Model and WASM assets
are copied from locked npm dependencies by `scripts/prepare-ocr.mjs`, not downloaded
from a CDN at runtime. Keep package licenses with distributed runtime assets.

Before claiming wider coverage, add separately labelled recordings, different
hands, unknown cards, occlusions, camera angles and lighting. Do not lower a
confidence gate merely to make one fixture pass. A score is not a calibrated
probability, and syntactically valid OCR may still be incorrect.
