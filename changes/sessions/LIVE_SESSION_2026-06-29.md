# Live Session Notes — Jun 29, 2026

**Status:** Operator log + incident notes. **Restart dashboard** after deploy for fix below.  
**Related:** [LIVE_SESSION_2026-06-26.md](LIVE_SESSION_2026-06-26.md) (summary poll + socket push), [MANUAL_STRATEGY.md](MANUAL_STRATEGY.md)

---

## Incident — Manual Spread Kill Selected always warns “Select open/closing row(s)”

### What the operator saw

On **Today → Manual Spread**, after placing/entering a trade, checking a row in **4. Active Manual Spreads** and clicking **Kill Selected** always showed:

> Select open/closing row(s)

—even when the row was visibly selected and state was **Open**.

### Root cause

| Layer | Issue |
|-------|--------|
| **Jun 26 fix** | `refreshSummary()` every **3s** (HTTP poll) + Socket.IO push every **2s** re-render the active manual grid via `renderManual()`. |
| **Selection** | `renderManual()` replaced `tbody.innerHTML`, **destroying checkbox checked state** on every refresh. |
| **Kill handler** | `msKillSelected()` read only `.ms-row-cb:checked` from the DOM — after any poll, selection appeared empty. |

Secondary UX gap: clicking a table row (like candidate selection in step 2) did **not** toggle the checkbox, so “selecting the row” was easy to misread.

**Note:** Kill Selected is intentionally limited to `open` / `closing` rows. **Working** (`pending_fill`) orders must use **Cancel Order** — improved toast when only working rows are selected.

### Fix shipped

**Client (`dashboard/templates/index.html`):**

- `msCheckedFilenames` `Set` — persists selection across summary re-renders
- `syncMsCheckedFromDom()` / `msRowCheckboxChange()` — keep Set in sync with checkboxes
- `getMsSelectedRows()` — resolves selection from Set + `lastData.manual_trades` (state survives DOM rebuild)
- Row click toggles checkbox + `ms-active-selected` highlight (matches candidate table pattern)
- Clearer toasts when nothing selected vs working-only selection

**Operator:** Restart **dashboard** (`python dashboard/server.py`) and hard-refresh browser.

---

## Incident — Kill spread close rejected at Tasty (`Vertical DebitCredit Check`)

### What the operator saw

After **Kill Selected** on a manual spread (7290/7315 PCS, qty 5), stop monitor logged spread close orders that Tasty **rejected** immediately:

```
reject-reason: [6063] Vertical DebitCredit Check
price-effect: Credit
price: 0.2
```

Operator stopped launcher and closed manually on Tasty.

### Root cause

`brokers/tastytrade_broker.py` `place_spread_close_order()` passed a **positive** limit price. TastyTrade's `NewOrder` treats **positive = credit, negative = debit**. Closing a credit spread is a **debit** — broker sent Credit → instant reject. Stop monitor retried every ~3s poll (`_poll_spread_close` on rejected), spamming rejects until shutdown.

| Layer | Bug |
|-------|-----|
| **Broker** | `price=_round_option_price(debit_limit)` → serialized as **Credit** |
| **Expected** | `price=-_round_option_price(debit_limit)` → **Debit** |

Live log (Jun 29 ~11:00 CT): order **479571927** — legs correct (BTC short / STC long), price-effect wrong.

### Fix shipped

**`brokers/tastytrade_broker.py`:** negate spread-close limit price so Tasty receives Debit.

**Operator:** Restart **launcher** (stop monitor) after deploy. Kill should place working spread debit closes.

Test: `tests/test_tastytrade_leg_actions.py::test_spread_close_debit_requires_negative_neworder_price`.

---

## Incident — No Unpause All MEIC; Pause/Unpause Selected used wrong table

### What the operator saw

- **Pause All MEIC** existed; no way to unpause all slots at once.
- **Pause/Unpause Selected** read checkboxes on the **Tranche Grid**, but operator looked at **Today's Session Plan** where the only checkbox was **Skip**.

### Fix shipped

**Dashboard:**

- **Unpause All MEIC** button + `POST /api/unpause_all_meic`
- Session Plan **Sel** column — check rows, then Pause/Unpause Selected
- Session Plan **Paused** checkbox column (editable; save row or use bulk buttons)
- Pause/Unpause Selected now use Session Plan selection (Tranche Grid checkboxes still work as fallback)

**Operator:** Restart **dashboard** and hard-refresh.

---

## Incident — Session Plan edits overwritten / table layout broken

### What the operator saw

Changes in **Today's Session Plan** (qty, windows, credits, pause, etc.) kept reverting every few seconds. Table layout also looked wrong.

### Root cause

| Layer | Issue |
|-------|--------|
| **Poll + socket** | `renderSessionPlan()` replaced full `tbody.innerHTML` every **2–3s** — destroyed in-progress edits. |
| **CSS bug** | `planInput(!editable, winStart)` passed window time (e.g. `11:00`) as a **CSS class** → invalid `class="plan-input 11:00"` on time inputs, breaking column layout. |

### Fix shipped

- `planDraftBySlot` — captures unsaved field values; restored on re-render
- Skip session-plan re-render while an input in that table has **focus**
- Fixed `planInput()` calls (no time string as class)
- Subtle indigo outline on rows with unsaved drafts (`plan-row-draft`)
- **Save** clears draft and refreshes

**Operator:** Hard-refresh dashboard (no launcher restart needed).

---

## Open / follow-up

| Item | Status |
|------|--------|
| MEIC tranche grid checkbox selection also cleared on poll (same class of bug) | Not changed — use Session Plan **Sel** column |
| Kill on **Working** via Kill Selected (route to cancel) | Deferred — docs specify Cancel Order for working |
| Spread close retry backoff after reject (non-sign bugs) | Deferred |
