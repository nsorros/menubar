# Cloud spend

What the cloud is costing, in one menu-bar item: the OpenRouter credit balance
(so a job doesn't fail at 2am because the account quietly ran dry) and AWS
spend for the Finant organization against its budgets. It was
`openrouter-credits` until AWS joined it.

The title reads `OR $7.00 ($0.57/24h) · AWS $12.30/mo` — the OpenRouter balance
and what's been spent from it in the last 24h, then AWS month-to-date — behind
one traffic-light dot for the worst state across both. OpenRouter goes amber at
or below $20 and red at or below $5; an AWS budget goes amber at 80% of its limit
and red once it's spent. Either source can fail without taking the other down:
its half of the title turns to `⚠` and the dropdown says why.

## OpenRouter

The dropdown breaks it down:

- **Remaining** — `total_credits - total_usage` for the whole account.
- **This key** — the spend attributed to the key the plugin authenticates with,
  split into today / this week / this month, plus its lifetime total. If the key
  has a spend limit set, its remaining headroom shows too.

- **Where costs happen · last 24h** — the shared balance broken down by *which
  tool* burned it, since the account is used by more than one. Each source logs
  its own per-call cost (asking OpenRouter for the authoritative `usage.cost`):
  - **Meeting recorder** — read from its local SQLite ledger
    `~/.local/state/meeting-recorder/openrouter-costs.db`, grouped by model.
  - **ant (app)** — fetched from `GET /api/admin/llm_costs?days=1`, grouped by
    feature (`meeting_prep`, `email`, `seed`, …).

  Each source is best-effort: a missing ledger or an unset ant token just drops
  that row (the other still shows), and any failure here never affects the
  balance readout above. The "Tracked total" sums only the sources that report —
  it can be less than the account's daily usage if something spends off-ledger.

  A configured-but-unreachable ant reads differently from an unconfigured one:
  it says **"ant didn't answer"** rather than pointing at the token setup below.
  The fetch retries (like the balance fetch) to ride out the network still coming
  up at login/wake — the usual reason it would otherwise vanish for one refresh.

Two endpoints back the balance itself:

- `GET https://openrouter.ai/api/v1/credits` — account-wide credits and usage.
- `GET https://openrouter.ai/api/v1/key` — the key's own spend and limit. This
  one is a bonus; if it fails the balance still renders.

The result is cached to `~/.local/state/cloud-spend/openrouter.json`, so a transient API
hiccup or a wake-from-sleep still shows the last-known numbers (greyed out, with
an "offline" note) rather than a bare warning.

### The API key

No secret lives in this repo — the key is read from your machine at runtime, in
this order:

1. `OPENROUTER_API_KEY` in the environment.
2. The macOS Keychain item **`openrouter-api-key`**.
3. The file `~/.config/openrouter/key`.

**Use the keychain.** xbar is launched by launchd with a bare environment, so it
does *not* inherit the `OPENROUTER_API_KEY` you export from your shell profile —
that path only works when xbar happens to be started from a terminal.

```sh
security add-generic-password -s openrouter-api-key -a "$USER" -w sk-or-v1-...
```

A read-only key is plenty; the plugin only ever GETs.

### The ant admin token (for the 24h cost breakdown)

The **Meeting recorder** row needs nothing — it reads the recorder's local
ledger, which fills the first time the recorder transcribes on the
cost-logging version (`mrec costs` shows the same numbers).

The **ant (app)** row needs the operator admin token, resolved the same
env → keychain → file way (absent = ant is simply left out):

1. `ANT_ADMIN_TOKEN` in the environment.
2. The macOS Keychain item **`ant-admin-token`**.
3. The file `~/.config/ant/admin-token`.

```sh
security add-generic-password -s ant-admin-token -a "$USER" -w <ANT_ADMIN_TOKEN>
```

The token is ant's `ADMIN_TOKEN` env var (Render service `ant.finant.ai`).
Override the app URL with `ANT_BASE_URL` (default `https://ant.finant.ai`) and
the window with `OPENROUTER_COST_WINDOW_HOURS` (default `24`; ant only reports a
24h window, so it drops out for other values).

## AWS

Read from the organization's management account (`finant`), which is the only
place Budgets and Cost Explorer see every member account:

- **Budgets** (`aws budgets describe-budgets`, every refresh) — actual spend
  against each budget, grouped by account: `ant-ant · today $1.20 · this month
  $30.00`, with the limits and any monthly forecast nested underneath. The
  organization-wide monthly budget is the `AWS $…/mo` figure in the title.
  Describing budgets is free; AWS itself refreshes the numbers a few times a day,
  so they trail real usage by hours (the dropdown shows when AWS last did).
- **By service · month to date** (`aws ce get-cost-and-usage`) — Cost Explorer
  charges $0.01 per request, so this runs every 6 hours (`AWS_CE_EVERY_HOURS`),
  not every refresh, and is cached in between. Its data trails by about a day.

Budgets are named `<scope>-daily` / `<scope>-monthly`; the suffix is dropped to
group them. A budget with no account filter counts as organization-wide.
Override the amber threshold with `AWS_BUDGET_WARN_PCT` (default `80`). Crossing
into amber or red notifies once per budget, like the OpenRouter balance.

It shells out to the AWS CLI (`brew install awscli`), found on `PATH` or in
`/opt/homebrew/bin` / `/usr/local/bin`. The last good answer is cached to
`~/.local/state/cloud-spend/aws.json` and shown greyed if a later call fails.

### Credentials

A dedicated read-only IAM user whose key lives in the keychain, so it never
expires on you. Create it once from the management account (the `finant` SSO
profile has the IAM rights to):

```sh
aws iam create-user --profile finant --user-name menubar-billing-readonly
aws iam put-user-policy --profile finant --user-name menubar-billing-readonly \
  --policy-name billing-read --policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Action":["ce:GetCostAndUsage","ce:GetCostForecast","budgets:ViewBudget"],"Resource":"*"}]}'
```

Then create its key straight into the keychain item **`aws-billing-readonly`**
(fish syntax; in bash/zsh use `"$(…)"`), so the secret never hits the screen:

```sh
security add-generic-password -s aws-billing-readonly -a $USER -w (aws iam create-access-key \
  --profile finant --user-name menubar-billing-readonly \
  --query 'AccessKey.[AccessKeyId,SecretAccessKey]' --output text)
```

The item holds `<AccessKeyId><tab><SecretAccessKey>`; `security` hands that back
hex-encoded because of the tab, which the plugin undoes. `AWS_BILLING_ACCESS_KEY_ID`
/ `AWS_BILLING_SECRET_ACCESS_KEY` in the environment take precedence.

Without a stored key it falls back to the SSO profile `finant`
(`AWS_BILLING_PROFILE` to change it). That works until the SSO session expires;
the dropdown then offers **AWS SSO sign-in needed — log in**, which runs
`aws sso login` in a terminal. With neither a key nor that profile, AWS is left
out of the title and the dropdown says it isn't set up.

## Install

```sh
ln -sf "$PWD/cloud-spend.10m.py" \
  "$HOME/Library/Application Support/xbar/plugins/"
```

Refresh xbar. Use the **Refresh interval** submenu to change the cadence — it
renames the symlink, leaving the committed filename alone.

Upgrading from `openrouter-credits`: remove the old symlink first
(`rm "$HOME/Library/Application Support/xbar/plugins/openrouter-credits."*.py`).
The cache and notification state move over on the first run.

## Low-balance notifications

When the OpenRouter balance crosses into low (≤$20) or critical (≤$5) it fires one
notification — once per crossing, not once per refresh. Topping up resets it, so
the next time you run down you get told again. Thresholds are overridable:

```sh
OPENROUTER_LOW=50 OPENROUTER_CRITICAL=10
```

## Uninstall

```sh
rm "$HOME/Library/Application Support/xbar/plugins/cloud-spend."*.py
rm -rf ~/.local/state/cloud-spend
rm -f ~/.local/state/menubar-notify/cloud-spend.json
security delete-generic-password -s openrouter-api-key
security delete-generic-password -s aws-billing-readonly
security delete-generic-password -s ant-admin-token   # if you stored one
```
