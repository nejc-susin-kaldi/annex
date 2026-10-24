# Archived apps

Apps that were deployed on the demo instance but are no longer part of it, kept as they ran. They are not built, tested or kept in step with `runtime/` (`scripts/sync-runtime.sh` skips this folder).

- `vibe-leads/`: a lead-capture form that hands a new client lead to a teammate by Slack or email (test mode: previews only). It was built with Claude Code before Goat Arcade, and its source existed only on the VM until GA.0 of `docs/plans/goat-arcade-demo.md` archived it here (23 Oct 2026). `party-bookings` reuses its form-and-hand-off idea.
- `venue-pulse/`: a web tool for a venue manager: her venue's week against last, idle machines and rotation candidates, read through the gateway. Archived 24 Oct 2026 (D115): a Metabase dashboard does the job ("not everything needs an app"). Replaced by the **Venue performance** dashboard.
- `weekly-takings/`: a web+job tool: last week's cash per venue as a CSV, a Monday email to finance and a run history. Archived 24 Oct 2026 (D115): a Metabase dashboard does the job ("not everything needs an app"). Replaced by the **Cash and collections** dashboard.
