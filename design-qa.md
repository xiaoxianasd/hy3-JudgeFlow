# Design QA

## Visual truth

- Source mockup: [`docs/assets/judgeflow-ui-redesign-target.png`](docs/assets/judgeflow-ui-redesign-target.png)
- Source dimensions: 1487 × 1058 px
- Implementation: `http://127.0.0.1:8765/`
- Implementation capture: Codex in-app browser capture (tool-visible; the browser API did not expose a filesystem path)
- Desktop viewport: 1440 × 1024 CSS px at DPR 1
- Responsive viewport: 390 × 844 CSS px at DPR 1

## State compared

The completed `coin_change` evaluation state was rendered through the production `render()` path with a temporary local fixture: code tests passed, reasoning review failed, and the first error was step 5. The fixture and comparison iframe were removed after QA. The normal empty state and live problem-loading state were also checked.

## Comparison evidence

- Full view: source mockup and implementation were displayed side by side in one 1440 × 1024 browser capture.
- Focused region: the 858 × 773 result panel was inspected directly, including verdict metrics, the reasoning timeline, code evidence, counterexample evidence, and the public-test ledger.
- Layout measurements: desktop document width 1425 px with no horizontal overflow; global navigation width 220 px. Mobile document width 375 px in a 390 px viewport with no horizontal overflow.

## Iterations

1. Fixed missing mobile navigation icons by vendoring Bootstrap Icons and correcting the brand-label selector.
2. Increased evidence density to match the mockup by adding a public-test ledger and shortening section labels.
3. Rechecked the final page after a fresh server restart; browser console contained no warnings or errors.

## Interaction checks

- Full-reasoning details expand and collapse correctly.
- Advanced settings expand and collapse correctly.
- Model, problem, search, difficulty, and API-key controls load and remain usable.
- Only public tests are shown in the evidence ledger.

## Findings

No open P0, P1, or P2 visual issues remain.

## Final Result

passed
