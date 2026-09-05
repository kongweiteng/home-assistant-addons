# Codex Controller mobile/Web design QA

**Comparison Target**

- Source visual truth: `/var/folders/9j/497_wvn523v1nh0cc48n_lnr0000gn/T/codex-clipboard-afd04192-94f8-4dc7-9b65-e2f279d33bef.jpg`, `/var/folders/9j/497_wvn523v1nh0cc48n_lnr0000gn/T/codex-clipboard-b6f0bb8d-68c0-4a17-911a-f83e889616e4.png`, `/var/folders/9j/497_wvn523v1nh0cc48n_lnr0000gn/T/codex-clipboard-4e15c8d8-b550-4603-ab5b-5df3428e0536.jpg`, and `/var/folders/9j/497_wvn523v1nh0cc48n_lnr0000gn/T/codex-clipboard-11817e0f-e66e-4fd6-8711-3ac9df1d3a1b.png`. These establish the prior desktop information architecture, the real narrow-phone failure state, and the mobile new-task sheet that needed correction. The Codex Mac light UI visible in the combined evidence is the approved visual direction rather than a pixel-identical mock.
- Implementation screenshots: `/Users/kongweiteng/kwt/homeassistant/.build/codex-controller-0535-design-qa-evidence/new-task-mobile-390x700.png`, `/Users/kongweiteng/kwt/homeassistant/.build/codex-controller-0535-design-qa-evidence/new-task-mobile-cropped.png`, `/Users/kongweiteng/kwt/homeassistant/.build/codex-controller-0535-design-qa-evidence/controller-overview-desktop.png`, and `/Users/kongweiteng/kwt/homeassistant/.build/codex-controller-0535-design-qa-evidence/controller-overview-cropped.png`.
- Viewport: mobile fixture `390 x 700` CSS pixels plus wide desktop capture. The host screenshots include browser chrome; fidelity judgment uses the cropped app regions.
- State: light theme, task list behind a modal backdrop, new-task sheet open, task text focused, Runner without `create_thread_v1` so the primary action is intentionally disabled. Wide desktop evidence shows the same state and content.
- Full-view comparison evidence: `/Users/kongweiteng/kwt/homeassistant/.build/codex-controller-0535-design-qa-evidence/new-task-comparison.png` places the prior narrow-phone source, Codex Mac light reference, and current implementation together in one image.
- Focused region comparison evidence: `new-task-mobile-cropped.png` and `controller-overview-cropped.png` make typography, form spacing, focus ring, disabled state, sheet radius, and overflow readable. No further crop is needed because the task sheet is the highest-density and highest-risk mobile region.

**Findings**

- No actionable P0, P1, or P2 visual mismatch remains in the compared task-list/new-task surfaces.
- Typography: the implementation consistently uses the Apple/PingFang system stack with restrained weights, readable line height, and clear title/label/helper hierarchy. Long Chinese copy wraps without collision; labels and disabled helper text remain legible. The light treatment is visibly closer to the approved Codex Mac direction than the earlier industrial dark surface.
- Spacing and layout rhythm: the mobile sheet uses full-width controls, stable vertical gaps, a clear drag handle, large close target, and a bounded scroll container. The `390 x 700` state remains usable without horizontal overflow; the wide view preserves the three-column information architecture. Radii and borders are quiet and consistent rather than card-heavy.
- Colors and tokens: warm off-white background, white surfaces, neutral text, blue focus, and semantic green/amber/red states form a coherent light token set. Contrast and focus indication are adequate in the captured states, and the modal backdrop separates context without visually burying it.
- Image quality and asset fidelity: the workbench itself has no required product imagery, logo illustration, or decorative raster asset. No source asset was replaced by emoji, CSS art, placeholder illustration, or handcrafted SVG. The robot/avatar visible in screenshots belongs to the surrounding HA/browser environment and is not part of this implementation.
- Copy and content: labels describe the same Mac task model in plain Chinese. The unavailable-create explanation is specific and placed next to the disabled action; the draft/request-ID recovery copy supports the non-duplicating behavior without exposing implementation noise in the main flow.
- Icons and affordances: the critical controls use explicit text labels and native form affordances; close, cancel, and primary action are unambiguous. All critical mobile targets are at least 44 px and do not depend on hover.
- Responsiveness and accessibility: the sheet is a bottom sheet on narrow screens and a centered dialog on wide screens; dynamic viewport and safe-area spacing prevent the earlier bottom-navigation/input overlap. Focus is visible, dialog focus is trapped, Escape closes it on keyboard devices, and disabled state is semantically expressed.

**Open Questions**

- No product decision blocks handoff. A physical HA companion-app pass is still required for the real device keyboard, safe-area inset, browser zoom/text scaling, and public-network latency; that is runtime acceptance, not an actionable mismatch in the captured implementation.
- The Controller settings page's new overview/tools/Runner section navigation has automated markup/responsive coverage but no separate same-state visual source target in this QA set. It is outside the strict task-sheet fidelity comparison and should be included in the physical phone smoke pass.

**Patches Made Since Previous QA Pass**

- Replaced the industrial dark presentation with a Codex-like light palette and quieter borders/elevation.
- Added a `920px` single-column mobile/tablet mode, dynamic viewport height, safe-area padding, fixed composer handling, and five-item mobile navigation including connection status.
- Made the new-task surface a bounded mobile bottom sheet; reduced narrow-screen textarea height, preserved two-column cancel/primary actions where practical, and kept the content internally scrollable for short screens and keyboards.
- Added visible focus, keyboard focus trapping, Escape dismissal, explicit disabled/recovery copy, and responsive connection/status surfaces.

**Implementation Checklist**

- [x] Compare the prior phone surface, Codex Mac light direction, and implementation in one combined artifact.
- [x] Inspect the full mobile sheet and focused form region at the short `390 x 700` target.
- [x] Inspect the same state in the wide desktop layout.
- [x] Check typography, spacing, colors, image/asset fidelity, copy, icons, responsive behavior, interaction states, and accessibility.
- [x] Confirm no actionable P0/P1/P2 finding remains.
- [ ] Run the separate physical HA companion-app acceptance for keyboard/safe-area/text-scale and the overview/tools/Runner navigation.

**Follow-up Polish**

- P3: after physical-phone acceptance, consider replacing text-only bottom-navigation labels with the same official icon family used by the Codex desktop product. This is optional polish; the present labels are clearer than approximate or custom-drawn icons.

final result: passed
