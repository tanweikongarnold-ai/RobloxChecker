# RobloxChecker

A Discord bot for moderation teams that:

- **Spots Roblox alt accounts** by looking at a player's badges: how many they have, how many different games they came from, whether they were farmed in one sitting, and when they were earned. It also checks account age, friends, groups and profile.
- **Flags erotic and sexual accounts** by scanning usernames, display names, bios, past usernames and group names, plus whether Roblox has blocked the player's avatar image. Common tricks to dodge filters (`s3x`, `s e x`, `hornyyy`, look-alike letters, `xXSexyGirlXx`, "add me for fun", "18+", ERP slang) are still caught.
- **Lets a whole team work at once**: flagged accounts go into a shared review channel. Moderators claim a case so nobody does the same work twice, then confirm or dismiss it and leave notes. Everything is recorded in an audit log.
- **Tracks players over time**: put a player on the watchlist and the bot rescans their badges in the background. It posts when they earn new badges, change their name or cross a threshold.

> Scores are hints for moderators, not proof. The bot never bans anyone by itself. A human always makes the final call.

---

## Web version (no Discord needed)

[`web/robloxchecker.html`](web/robloxchecker.html) is a browser version of the moderation desk, published as a claude.ai page:
**https://claude.ai/artifact/ALU75BJcokkbg8QZEMbbkE**

- **Check a player**: the same alt score and NSFW filter as the bot, worked out in the browser as you type. Pages hosted on claude.ai can't contact Roblox, so you enter the player's details yourself. To save typing, paste the profile page text (or a screenshot) and Claude fills the form in for you.
- **Review queue**: a shared, live list of cases. Claim, confirm, dismiss, reopen and leave notes. Two people can't claim the same case, and you can see who else is looking at a case right now.
- **Trusted list, filter words, thresholds and team activity**, shared by everyone who opens the page.

To let your team use it, share the page from its **Share** menu. Give moderators **Contributor** access so they can work on cases; Viewers can only look. Only the owner and Editors can change thresholds.

The web queue is separate from the Discord bot's database: cases opened in one don't appear in the other.

## Quick start

You need **Python 3.10 or newer**.

1. **Create the bot.** Go to <https://discord.com/developers/applications>, click **New Application**, then open **Bot** and **Reset Token**. Copy the token. The bot doesn't need any privileged intents.
2. **Invite it.** Replace `YOUR_APP_ID` (shown under *General Information*) and open this link:
   ```
   https://discord.com/oauth2/authorize?client_id=YOUR_APP_ID&scope=bot+applications.commands&permissions=84992
   ```
   (permissions: View Channels, Send Messages, Embed Links, Read Message History)
3. **Install and configure:**
   ```bash
   git clone https://github.com/tanweikongarnold-ai/RobloxChecker.git
   cd RobloxChecker
   python -m venv .venv
   source .venv/bin/activate          # Windows: .venv\Scripts\activate
   pip install -r requirements.txt
   cp .env.example .env               # then paste your token into .env
   ```
4. **Run it:**
   ```bash
   python -m bot
   ```
   Slash commands are registered on startup. Set `DEV_GUILD_ID` in `.env` to your server's ID while testing and they show up instantly in that server.

The bot keeps everything in one SQLite file (`data/robloxchecker.db` by default). Back that file up. The badge tracker only runs while the bot is online, so host it somewhere that stays up (a VPS, Railway, a Raspberry Pi, etc.).

## Setting up your server (admins)

Anyone with **Manage Server** can configure the bot:

```
/config channels review:#alt-review log:#mod-log tracking:#badge-tracking
/config add-modrole role:@Moderators
/config show
```

| Channel | What goes there |
|---|---|
| **review** | One message per flagged account, with **Claim / Confirm / Dismiss / Add note** buttons. This is the team's shared queue. |
| **log** | One line for every action a moderator takes (claimed, confirmed, trusted, changed a setting…). |
| **tracking** | Updates for players on the watchlist. If you don't set it, these go to the review channel. |

Until you add a mod role, anyone with Moderate/Kick/Ban Members permission counts as a moderator.

## How the team works together

1. **Something gets flagged.** A moderator runs `/check SomePlayer`, or the background tracker rescans a watched player. If the alt score or NSFW score crosses the server's threshold, a **case** opens in the review channel. Each account can only have one open case at a time, so if two moderators check the same player at once you still get just one case.
2. **Someone claims it.** Press **👀 Claim** (or `/case claim`). The message turns blue and shows who's on it, so nobody duplicates work. Only the claimer, or an admin, can close a claimed case. If two people press Claim at the same moment, exactly one of them gets it.
3. **Discuss.** Press **📝 Add note** (or `/case note`). Notes appear on the case message for everyone.
4. **Decide.** Press **✅ Confirm** (it really is an alt or NSFW account) or **🗑️ Dismiss** (false alarm). Closed cases can be reopened.
5. **Stay on top of it.** `/case list only:unclaimed` shows what still needs someone. `/case list only:mine` shows your own claims. `/case stats` shows the queue size and who's handled what. `/audit` shows the full history.

The buttons keep working after the bot restarts, and anyone with a mod role can press them.

If an account keeps getting flagged but is actually fine (a friend's new account, a verified main), use **🛡️ Mark trusted** or `/trust add`. It won't be auto-flagged again in your server.

## Commands

| Command | What it does |
|---|---|
| `/check user [public]` | Full scan: alt score with reasons, NSFW matches, badges, social stats. Opens a case automatically if something crosses a threshold. Only you see the result unless `public:True`. |
| `/badges user [page]` | A player's badges and when each was earned. |
| `/case list [status] [only] [page]` | The review queue. `only:unclaimed` or `only:mine` narrow it down. |
| `/case view` · `claim` · `release` · `resolve` · `note` · `reopen` | Work a case. Case numbers autocomplete. |
| `/case open user reason` | Open a case by hand, even if nothing was auto-flagged. |
| `/case history user` | Every case your server has had for a player (spot repeat offenders). |
| `/case stats [days]` | Open/unclaimed counts and per-moderator activity. |
| `/track add user [note]` · `remove` · `list` · `history` · `now` | Watchlist. Tracked players are rescanned every `TRACK_INTERVAL_MINUTES` (default 6 hours). |
| `/trust add user [reason]` · `remove` · `list` | Accounts that should never be auto-flagged. |
| `/keywords add` · `disable` · `remove` · `list` · `test` | Tune the NSFW filter for your server. `test` shows how any text would score. |
| `/config show` · `channels` · `clear-channel` · `add-modrole` · `remove-modrole` · `thresholds` · `autoflag` | Server settings (Manage Server). |
| `/audit [moderator] [limit]` | Recent moderator actions. |

Every result includes a link to the player's Roblox profile. `/check` results have buttons to open a case, start tracking or mark the account as trusted.

## How the alt score works

Each check adds or removes points. The total is capped to 0–100, and `/check` lists every reason so moderators can judge for themselves.

| Signal | Points |
|---|---|
| Account age: under 3 days / 14 days / 30 days / 90 days / 1 year | +35 / +28 / +20 / +12 / +5 |
| Account age: 3+ years | −10 |
| Badges: none / under 5 / under 20 / under 50 | +30 / +22 / +12 / +5 |
| Badges: 50–199 / 200+ | −5 / −10 |
| All badges from only 1–2 games (badge farming) | +12 |
| 80%+ of badges from one game | +8 |
| Badges from 25+ different games | −5 |
| 80%+ of badges earned within a single 24 hours | +12 |
| Old account that only started earning badges in the last 2 weeks | +10 |
| First badge earned more than a year after the account was made | +6 |
| Badges private (can't verify) | +6 |
| No friends / under 5 friends / 50+ friends | +10 / +6 / −5 |
| 100+ / 1000+ followers | −5 / −10 |
| Not in any groups / 5+ groups | +6 / −3 |
| Empty bio / username ending in 4+ digits | +4 / +6 |
| Has paid to change username | −4 |
| Has had Roblox Premium; other Roblox achievement badges | −10; up to −12 |
| Roblox verified badge | −40 |

Default verdicts: **60+ = Likely alt** (opens a case), **35+ = Suspicious**, below that = Likely main. Change them with `/config thresholds`.

## How the NSFW filter works

The default word list is in [`data/nsfw_terms.json`](data/nsfw_terms.json). Each term has a **weight**. For each account the bot keeps the strongest match per term and adds the weights together. **40+ opens a case** by default. Strong terms flag on their own (for example "porn" or "nsfw" = 60). Weaker hints like "snap", "freaky" or "18+" only flag when they show up together.

Where a match is found changes its weight. Username and display name count 1.2×, bio and past names 1×, and group names 0.6×. If Roblox has blocked the avatar image, that adds 25.

Before matching, the text is cleaned up so common tricks don't work:

| Trick | Example | Handled by |
|---|---|---|
| Leetspeak | `s3x`, `p0rn`, `$lut` | Numbers and symbols inside words are turned back into letters (plain numbers like "7175 visits" are left alone) |
| Spacing out letters | `s e x`, `n.s.f.w` | Single letters separated by spaces or dots are joined back up |
| Stretching | `hornyyyy`, `seeexy` | Repeated letters match |
| Censoring | `s*x` | `*` can stand in for a vowel |
| Look-alike letters | Cyrillic `е`, full-width `Ｐ`, zero-width spaces | Converted to normal letters / removed |
| Joined-up words | `xXSexyGirlXx` | Split at capital letters |

To avoid false positives, matching is whole-word by default, so "Essex", "thorny" and "cucumber" don't match. The list also deliberately leaves out identity words (gay, trans, femboy…), because those aren't sexual content.

To tune it for your server:

```
/keywords test text:"add me for fun 18+"        # see how text scores
/keywords add term:"bad group name" weight:50   # add your own (letters/spaces)
/keywords disable term:daddy                    # turn off a built-in term
/keywords remove term:daddy                     # undo your change
```

## Configuration (`.env`)

| Variable | Default | |
|---|---|---|
| `DISCORD_TOKEN` | *(required)* | Bot token |
| `DEV_GUILD_ID` | empty | Sync commands instantly to this server while testing |
| `DATABASE_PATH` | `data/robloxchecker.db` | SQLite file shared by everyone |
| `TRACK_INTERVAL_MINUTES` | `360` | How often watched players are rescanned (minimum 10) |
| `MAX_BADGES` | `500` | Badges fetched per scan (100 per request) |
| `LOG_LEVEL` | `INFO` | |

## Development

```bash
pip install -r requirements-dev.txt
pytest
```

The tests use canned Roblox API responses and a temporary database, so they run offline.

```
bot/
  bot.py            startup, command sync, error handling
  roblox.py         Roblox web API client (retries, rate limits, caching)
  alt_detector.py   alt-account scoring
  content_filter.py NSFW detection
  scanner.py        fetch + analyse, shares in-flight requests between moderators
  db.py             SQLite: cases, notes, audit log, watchlist, settings
  services.py       case workflow used by both buttons and commands
  views.py          persistent buttons and modals
  embeds.py         message layouts
  cogs/             slash commands (check, cases, tracking, settings)
data/nsfw_terms.json  default filter list
tests/
```

## Limitations

- Everything here is a **heuristic**. New players really do have new accounts with few badges, and an old alt can look perfectly normal. Always review the reasons before acting.
- It only uses Roblox's **public** APIs (no login). If a player hides their inventory, their badges can't be read; the bot says so instead of counting it as "0 badges".
- Roblox rate-limits its APIs. The bot retries, caches results for two minutes, and shares one lookup between moderators checking the same player. Very large watchlists are rescanned gradually.
- The bot can't see what a player does in game or in private messages, and it doesn't look at avatar images itself (it only notices when Roblox has blocked one).
