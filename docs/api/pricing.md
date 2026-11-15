# Supplier cost, selling price and profit (Group Ticketing and Flights)

One model for both products (`app/services/pricing.py`, `app/db/booking_pricing.py`, migration 0021).

- **Cost**: what the supplier or airline charges. **Selling**: what the customer pays. **Profit** = selling - cost.
- Kept per traveler (`booking_travelers.cost_minor`, `selling_minor`) and as booking totals (`bookings.supplier_cost_minor`,
  `total_minor`). `total_minor` is the selling total: Total, Due and every payment check use it; payments are capped at it.
- **Default selling price** = cost + the product's existing markup rule (group: `GROUP_TICKETING_MARKUP_*`, 0 when unset;
  flights: `FLIGHT_MARKUP_*`). Those rules are unchanged.
- **Setting it**: whole rupees, not below the traveler's cost, before any payment, while the booking is held and not issued.
  Super Admin, or staff with the record payments permission. Group: `selling_price_per_adult` on the hold request, or
  later `POST /v1/admin/bookings/{id}/selling-price`. Flights: `POST /v1/admin/bookings/{id}/selling-price`
  (`prices: [{passenger_type, selling_each}]`). Logged (`selling_price_set`).
- **Who sees cost and profit**: only viewers with the cost and margin permission (Super Admin; staff when granted). Everyone
  else sees the selling total only.
- **Supplier price changes** (the selling price stays; the profit changes):
  - Group, at the hold: `price_change` on the hold (selling total, `below_cost`; shown and held cost, difference and profit
    with cost access). Payments wait until `accept-held-price`, which is refused while selling < the new cost.
  - Group, at the price check and at issue: only the supplier cost is compared and updated (booking and travelers);
    the selling price, set by staff or by default, is never recalculated. 409 `supplier_cost_changed`, then issue with
    `accepted_supplier_cost`; 409 `below_cost` when the new cost is above the selling total, then also `accept_below_cost`.
    The total can't drop below what was paid here, so Due never goes negative and no refund arises.
  - Flights, at the hold: one cost source. After booking, the supplier's booking record (pnr_info) must show the
    repriced total; that total, the per-passenger costs and the below-cost check then agree. If it can't be verified
    (no record, no total, another status or currency, or a different total), nothing is saved: 502 `total_unverified`
    with the PNR, and the supplier hold lapses at its time limit.
  - Flights, at the hold: the hold goes ahead while every confirmed selling price covers the new airline cost; otherwise
    409 `price_changed` (revalidate). The hold answer carries `supplier_cost` and `profit` with cost access.
  - Flights, at ticketing: 409 `supplier_cost_changed` (with `accepted_supplier_cost`, and the costs and profit with cost
    access); issue again with `accepted_supplier_cost` to ticket at the new cost. 409 `below_cost` when the new cost is
    above the selling total.
- **Existing bookings (0021)**: a booking with a recorded supplier cost keeps it; one without gets cost = its current total
  (profit 0). Travelers get their booking's totals split evenly (remainder on the first). Columns are added nullable,
  backfilled, then made required. No total, payment, received or due amount changes.
- No receipts, printouts or customer emails exist yet; when they are built they show the selling price only, and never
  supplier names, cost or profit.
