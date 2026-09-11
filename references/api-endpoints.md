# LPAgent API — Full Endpoint Reference

Recon date: 2026-09-11 (deep dive, 57 JS chunks, 6.8 MB)

## Base

- Primary: `https://api.lpagent.xyz/api` (no Cloudflare challenge)
- Mirror: `https://api.lpagent.io/api` (CF Managed Challenge on direct curl — use .xyz)
- Headers (all requests): `chain: ROBINHOOD` (also SOL/BASE), `Origin: https://app.lpagent.io`, `Referer: https://app.lpagent.io/`
- Auth: most GET endpoints unauthenticated. Authenticated ones return `{"error":"Missing auth token"}` (Privy JWT via cookie session).
- Rate limit: 30 rapid requests all 200; sustained 1 req/2.5s fine. No 429 observed.
- Chain: ROBINHOOD = Robinhood Chain, chainId 4663 (0x1237), block time ~90ms.
- NFPM contracts: V3 `0x73991a25c818bf1f1128deaab1492d45638de0d3`, V4 `0x58daec3116aae6d93017baaea7749052e8a04fa7`

## LP Positions (unauthenticated)

| Method | Endpoint | Params | Notes |
|---|---|---|---|
| GET | `/v1/lp-bot/lp-positions/opening/{owner}` | `platform=uniswap_v3,uniswap_v4` (optional) | Open positions, full detail (pairName, currentValue, pnl, fees, ticks, dpr) |
| GET | `/v1/lp-bot/lp-positions/{owner}` | — | Position summary (returns `data: null` if none) |
| GET | `/v1/lp-bot/lp-positions/historical/{owner}` | `page,pageSize,order_by(createdAt|closeAt),sort_order,pool,pnl_threshold,pnl_native_threshold,platform` | Closed positions w/ ageHour, inputValue, outputValue, collectedFee, pnl, closeAt, tokenInfo, priceRange |
| GET | `/v1/lp-bot/lp-positions/overview/{owner}` | `protocol=uniswap_v3,uniswap_v4` (REQUIRED for EVM) | Aggregates: total_inflow/outflow/fee/pnl per range (ALL/7D/1M/3M/1Y/YTD), total_lp, win_lp, win_rate, closed_lp, apr, roi |
| GET | `/v1/lp-bot/lp-positions/revenue/{owner}` | `period=day, range=7D, protocol=...` | Daily revenue buckets |
| GET | `/v1/lp-bot/lp-logs` | `position={tokenId}` | Tx history per position (increase/decrease/collectFee w/ amounts+prices) |

## Pools (unauthenticated)

| Method | Endpoint | Params | Notes |
|---|---|---|---|
| GET | `/v1/pools/discover` | filters, pagination | Pool list |
| GET | `/v1/pools/{poolId}/info` | — | Pool info (EVM pool IDs OK) |
| GET | `/v1/pools/{poolId}/top-lpers` | `page,pageSize,order_by,sort_order` | Leaderboard w/ owner, pnl, fee, apr, roi, win_rate |
| GET | `/v1/pools/{poolId}/positions` | `page,pageSize,status=Open,owner,platform` | Positions in pool |
| GET | `/v1/pools/{poolId}/onchain-stats` | — | On-chain pool stats |
| GET | `/v1/pools/{poolId}/robinhood-detail` | header chain:ROBINHOOD | Robinhood pool detail |
| POST | `/v1/pools/{poolId}/add-preview` | stratergy, inputSOL, percentX/amountX/amountY, fromBinId, toBinId, mode | Preview add |
| POST | `/v1/pools/{poolId}/add-tx` | same as add-preview | Build add tx |
| POST | `/v1/pools/{poolId}/zap-in` | body | Zap in (RH) |
| POST | `/v1/pools/{poolId}/decrease/execute` | body, header chain | Decrease |
| POST | `/v1/pools/landing-add-tx` | — | Landing page add |

## Position actions (POST, auth not enforced on tx builders — sign client-side)

`/v1/position/create`, `/v1/position/increase-tx`, `/v1/position/decrease-tx`, `/v1/position/decrease-quotes`, `/v1/position/claim-fee` `{id}`, `/v1/position/compound` `{positionId,owner}`, `/v1/position/save`, `/v1/position/price-pin` (GET, needs `id` or `tokenId`), `/v1/position/landing-*` variants.

## Token endpoints (unauthenticated)

| Method | Endpoint | Notes |
|---|---|---|
| GET | `/v1/token/prices` | `{"SOL":..,"ROBINHOOD":..,"BASE":..}` native prices |
| GET | `/v1/token/balance?owner={addr}` | Token balances + logos |
| GET | `/v1/token/search?address={ca}&chain=ROBINHOOD` | Token info by address |
| GET | `/v1/token/audit?ca={ca}` | Token audit (Solana-oriented: "Not a Solana mint" for EVM) |
| GET | `/v1/token/robinhood/list` | Robinhood token list |
| POST | `/v1/token/swap` | Swap quote (tokenIn/tokenOut w/ decimals) |
| POST | `/v1/token/robinhood/swap`, `/v1/token/robinhood/swap/quote` | Robinhood swap |
| GET | `/v1/swap/limit-order?inputMint=&outputMint=&amount=` | Limit order status |
| POST | `/v1/swap/limit-order/create`, `/execute` | Limit order ops |

## Smart LP / copy trading

| Method | Endpoint | Auth | Notes |
|---|---|---|---|
| GET | `/v1/smart-lp` | no | Smart LP accounts w/ stats |
| GET | `/v1/schedule/follow?chain=` | 401 | Followed wallets |
| PUT | `/v1/schedule/follow` | 401 | Update follow settings |
| GET/PUT | `/v1/schedule/follow/{owner}?chain=` | GET 401 | Per-wallet follow |
| GET | `/v1/schedule/positions/{id}/logs` | 401 | Copy logs |
| GET | `/v1/schedule/copy-analysis-pnl?period=&range=` | auth | Copy PnL |
| GET | `/v1/copy-access` | 401 | Copy access status |
| POST | `/v1/copy-access/redeem` | — | Redeem access code |

## Airdrops / claim (unauthenticated)

| Method | Endpoint | Notes |
|---|---|---|
| GET | `/v1/met-airdrop/{wallet}` | MET airdrop eligibility/amount (mint METvsvVRapdj9cFLzq4Tr43xK4tAjQfwX76z3n6mWQL) |
| POST | `/v1/met-airdrop/claim` `{wallet}` | Claim |
| GET | `/v1/claim-sol/{wallet}` | Claimable SOL status |
| POST | `/v1/claim-sol/build-tx`, `/execute`, `/landing` | SOL claim ops |
| POST | `/v1/waitlist` `{wallet,discord,twitter_url,email}` | Waitlist signup |
| GET | `/v1/referral/{wallet}` | Referral info (404 Wallet not found if none) |
| POST | `/v1/referral` `{wallet,ref_code}` | Bind referral code |

## Account (Privy auth required)

`/v1/me` (401 without), `/v1/me/settings` PUT, `/v1/me/onboarding` POST, `/v1/me/blacklist-token` GET/POST/DELETE, `/v1/me/circuit-breaker` GET + `/reset` POST, `/v1/me/push-subscriptions` GET/POST/DELETE + `/test`, `/v1/me/wallet/reconcile` POST.

## Chat / conversations

`/v1/chat/{id}` POST (roomId, message, metadata), `/v1/conversations/{id}` GET/PATCH/DELETE, `/v1/users/conversations` GET.

## Other

- `x-lp-portfolio-preview: 1` header: guest portfolio view flag from `?portfolioPreview=1`
- WebSocket `wss://ws.lpagent.io/ws`: price subscriptions only (`{"type":"subscribe:prices","assets":[...]}`) — NOT position events
- `rt.lpagent.io`: realtime service `{"status":"ok","service":"realtime"}`
- Docs: docs.lpagent.io
- Explorer: robinscan.io (Robinhood Chain)
