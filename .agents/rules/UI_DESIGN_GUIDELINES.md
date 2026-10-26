# UI Design Guidelines: High-Contrast Dark Mode Typography

## Rule: Prohibition of `text-muted` on Dark Dashboard Tables

### Directive
**Under no circumstances should `text-muted` or low-contrast dark gray typography (e.g., `#6c757d`) be used for table data cells, timestamps, prices, or targets on dark backgrounds (`#141722`, `#1a1d27`, `#0e111a`).**

### Context & Rationale
In dark mode themes, Bootstrap's default `text-muted` (`#6c757d` / `#718096`) blends directly into dark slate and charcoal container backgrounds, resulting in contrast ratios below 2.5:1. This causes critical trading data (such as Open Time, Closed At, and Profit Targets / Stop Losses) to become virtually invisible on user displays.

### Mandatory Standards

1. **Table Data Cells & Values**:
   - Use `text-light` (or `#f8fafc` / `#e2e8f0`) as the baseline color for text and numeric values.
   - Example: `<td class="font-monospace small text-light fw-medium">${t.entered_at ? formatCST(t.entered_at, true) : '—'}</td>`

2. **Table Column Headers (`<th>`)**:
   - Use high-contrast slate headers with `color: #94a3b8;` (Slate-400) or `text-light`.
   - Never use `text-secondary` or `text-muted` for table headers without explicit high-contrast inline styling.

3. **Targets & Trade Parameters (PT / SL)**:
   - Use high-contrast accent colors:
     - Profit Target (PT): `text-info` (`#38bdf8`) or `text-success` (`#4ade80`).
     - Stop Loss (SL): `text-warning` (`#facc15`) or `text-danger` (`#f87171`).
   - Example: `<td class="small font-monospace text-light"><span class="text-info fw-semibold">PT: ${pt}</span> &nbsp;|&nbsp; <span class="text-warning fw-semibold">SL: ${sl}</span></td>`

4. **Empty State Rows**:
   - Empty state placeholders may use `text-muted` only if accompanied by an icon and displayed with sufficient padding, but never active data columns.
