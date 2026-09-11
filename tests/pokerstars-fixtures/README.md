# PokerStars classic recording checks

Unpack the separately supplied `pokerstars-training-kit.zip` into the repository.
It contains `tests/pokerstars-fixtures/frames.json`, 15 full-resolution PNG frames,
`pokerstars-symbols.json`, source annotations and the measured readouts. These
user-derived assets are ignored by Git and must not enter a Pages artifact.

Run `npm run test:pokerstars` for the real local Tesseract model, shared production
pixel preprocessing, six-seat parser and corner-card classifier. This command
fails if the private dataset is absent. Ordinary `npm test` skips this one test
without the archive; public CI still checks the pure profile/parser regressions.

For the website, select **PokerStars · классический, 6 мест**, open the original
1280×640 MP4, and import `tests/pokerstars-fixtures/pokerstars-symbols.json` with
«Загрузить шаблоны карт». Check the regions, confirm alignment and start recognition.
The template pack stays in page memory and must be reselected after a reload.

This source is a 322.168-second old mobile landscape PokerStars recording with
five complete hands. It is not an independent camera or general-client dataset.
The 15 control frames are at 0, 17, 48, 51, 77, 97, 126, 132, 173, 181, 204,
227, 247, 290 and 321 seconds. Frames at payouts and card animations deliberately
remain in the tests. Ground truth was visually checked; runtime code never looks
up values by timestamp, hand sequence or known player identity.

The pack contains all 13 rank symbols and 4 suits, extracted from 21 observed
card combinations. This does not validate all 52 cards. Independent-in-time
symbol checks on the same recording accepted 33/37 cards, with 4 abstentions and
no wrong accepted cards; 21/21 empty slots were rejected. The full integration
checks use different selected frames and are not an independent generalization
estimate. Keep matching thresholds conservative; add separately annotated
recordings before claiming broader recognition.

The integration run reads 13/15 pots, 86/87 numeric stacks, all 23 visible chip
labels (including three payouts), 59/64 face-up hero/board card slots, and 9/10
action captions. It confirms all four active-button examples and never confirms
the other 11 as hero's turn. Accepted tested values are correct; the rest are
unknown. OCR confidence is not a calibrated accuracy probability.

`chip_display` is the literal amount near a seat. Its meaning may be a wager or
a payout; `bet` stays null. The current hand number must not come from stale chat.
Dealer, exact hand boundaries, opponent hole cards, side pots, action amounts and
the full action history remain outside this snapshot recognizer.
