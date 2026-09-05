# Controller scheme 1 responsive design QA

Date: 2026-09-06, Asia/Shanghai. Candidate: Controller 0.5.36.

## Comparison target and evidence

- Source visual truth: approved scheme 1 prototype, light task-first layout. Private evidence directory: `.build/design-qa/codex-controller-responsive/` in the integration workspace. References: `scheme1-reference-desktop.png`, `scheme1-reference-mobile-list.png`, `scheme1-reference-mobile-detail.png`.
- Implementation: actual Controller HTML/JS served by a loopback-only synthetic QA server, not production HA. Captures: `scheme1-desktop.png`, `scheme1-mobile-list.png`, `scheme1-mobile-detail.png`.
- Viewports: desktop 1440 x 900; mobile 390 x 844; short-screen 390 x 500. Same light theme and task-list/active-conversation states. Reference retains a 34px prototype selector strip; synthetic task text and activity differ. Compare structure and controls, not demo chrome, exact message positions or synthetic latency.
- Full-view combined evidence: `scheme1-compare-desktop.png`, `scheme1-compare-mobile-list.png`, `scheme1-compare-mobile-detail.png` place reference and implementation side by side at equal scale.
- Focused combined evidence: `scheme1-compare-mobile-detail-header.png`, `scheme1-compare-mobile-detail-composer.png`. Controls remain readable without shrinking the full desktop comparison.
- Additional states: `scheme1-mobile-short.png`, `scheme1-mobile-new.png`, `scheme1-mobile-image-draft.png`, `scheme1-mobile-image-lightbox.png`, `scheme1-mobile-tools.png`, `scheme1-mobile-settings.png`, `scheme1-mobile-runners.png`. Screenshots remain private, not shipped with the Add-on.

## Findings and required fidelity surfaces

- No actionable visual P0/P1/P2 issue remains in the compared surfaces. Functional regression gates are separate; local QA is not production or handset acceptance.
- Fonts and typography: system Apple/PingFang/sans-serif stack, restrained headings and muted metadata preserve reference hierarchy. Chinese task titles truncate within their boundary; messages wrap. Mobile composer text is 16px. Full timestamps and capability hints intentionally add traceability and density.
- Spacing and layout: 350px desktop task column and flexible conversation reproduce scheme 1's two columns. Mobile is a list followed by full-screen conversation, with five navigation entries outside the scrolling list. Composer, receipts and conversation share bounded flex space. At 390 x 500 the input remains visible without horizontal overflow or overlap. Project choice is a dialog, not a permanent third column.
- Colors and tokens: off-white list, light conversation, quiet gray borders, dark primary action and restrained semantic colors match the light direction. Activity summaries retain existing amber semantics rather than the demo blue. Image close uses explicit dark-on-white contrast.
- Image quality and assets: official Phosphor icons match the prototype family; no handcrafted icon or decorative image substitute. Viewer uses contain and thumbnails intentionally crop. Preview shows the actual compressed upload, not a higher-quality original. A synthetic UI screenshot remained legible after compression; this does not establish quality for every photo or dense screenshot. Limits and a preview reminder are visible.
- Copy and content: concise tasks/tools/status/settings navigation. Actual connection state replaces illustrative latency. Four delivery stages distinguish acceptance from Mac confirmation. Queue, image constraints and advanced model options are intentional extensions to the simpler prototype. The watermark and sample messages exist only in the private fixture.
- Accessibility: main touch actions, image removal and close are at least 44px. Icons have explicit labels; disclosures and dialogs retain semantics. Mobile Enter preserves newlines, while desktop sending shortcuts remain. Physical keyboard, zoom and safe-area behavior require separate handset acceptance.

## Patches since preceding QA

- Replaced three-column layout with scheme 1 desktop two-column/mobile navigation.
- Separated management pages with grouped/searchable incremental tools and live status.
- Added official icons, visible lightbox close contrast and 44px removal targets.
- Corrected short-screen composer, project dialog placement and task-switch draft/receipt races.
- Replaced create-result polling with bounded host SSE receipts and reconnect recovery.
- Disambiguated execution-failure and recovery-required task filters.

## Interaction checks and boundaries

- Local fixture exercised project scope, task selection, text task creation and exact mock receipt, tools search, management navigation, image upload/preview/remove, pure-image mock send and lazy history read.
- Automated gates cover unknown receipts, idempotency, capabilities, bounded image payloads and task-switch generations. SSE is not converted to polling.
- Physical HA companion app, Mac image interpretation, same-original-task delivery and off-LAN latency remain pending. Mock receipts and screenshots cannot prove these.
- New-task form is text-only; images can be added after creation. A new-task image picker remains a functional follow-up, not a completed feature.

## Implementation checklist

- [x] Compare reference and source in combined full and focused artifacts.
- [x] Inspect typography, spacing, colors, assets, copy and responsiveness.
- [x] Exercise local navigation, creation and conversation-image interactions.
- [x] Resolve compared visual P0/P1/P2 findings.
- [ ] Physical-phone keyboard, safe-area and zoom acceptance.
- [ ] Production release/upgrade and actual off-LAN image/receipt/freshness acceptance.

## Follow-up polish

- P3: consider more compact image-limit hints and receipts after physical-phone feedback, while retaining accessible safety information.

final result: passed
