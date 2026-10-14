"""Builders for every embed the bot sends."""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from typing import TYPE_CHECKING, Iterable

import discord

from .alt_detector import LIKELY_ALT, SUSPICIOUS
from .content_filter import Hit, NsfwReport
from .db import Case, Note, WatchEntry
from .models import AccountData
from .scanner import ScanResult

if TYPE_CHECKING:
    from .group_scan import GroupScanReport, MemberScan

RED = discord.Colour.from_rgb(220, 53, 69)
DARK_RED = discord.Colour.from_rgb(139, 0, 0)
ORANGE = discord.Colour.from_rgb(253, 126, 20)
GREEN = discord.Colour.from_rgb(40, 167, 69)
BLUE = discord.Colour.from_rgb(13, 110, 253)
GREY = discord.Colour.from_rgb(108, 117, 125)

REASON_LABELS = {"alt": "🕵️ Alt", "nsfw": "🔞 NSFW", "manual": "📝 Manual"}
STATUS_LABELS = {"open": "🟠 Open", "confirmed": "🔴 Confirmed", "dismissed": "⚪ Dismissed"}


def ts(moment: datetime | None, style: str = "R") -> str:
    return f"<t:{int(moment.timestamp())}:{style}>" if moment else "unknown"


def clip(text: str, limit: int = 1024) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def bar(score: int) -> str:
    filled = round(score / 10)
    return "█" * filled + "░" * (10 - filled)


def mention(user_id: int | None, fallback: str = "automatic") -> str:
    return f"<@{user_id}>" if user_id else fallback


def _lines(lines: Iterable[str], limit: int = 1024) -> str:
    out: list[str] = []
    used = 0
    for line in lines:
        if not line:
            continue
        if used + len(line) + 1 > limit - 2:
            out.append("…")
            break
        out.append(line)
        used += len(line) + 1
    return "\n".join(out) or "—"


def _hit_line(weight: int, label: str, field: str, matched: str) -> str:
    shown = f"||{discord.utils.escape_markdown(matched)}||" if matched and matched != label else ""
    where = f" in {field}" if field else ""
    return f"`+{weight:>2}` **{discord.utils.escape_markdown(label)}**{where} {shown}".rstrip()


def nsfw_lines(report: NsfwReport) -> list[str]:
    return [_hit_line(h.weight, h.label, h.field, h.matched) for h in report.hits]


def scan_colour(result: ScanResult) -> discord.Colour:
    if result.nsfw.flagged:
        return DARK_RED
    if result.alt.verdict == LIKELY_ALT:
        return RED
    if result.alt.verdict == SUSPICIOUS or result.nsfw.hits:
        return ORANGE
    return GREEN


def scan_embed(result: ScanResult, *, open_case: Case | None = None) -> discord.Embed:
    account, alt, nsfw = result.account, result.alt, result.nsfw
    name = discord.utils.escape_markdown(account.username)
    title = f"{account.display_name} (@{account.username})" if account.display_name != account.username else name
    embed = discord.Embed(title=clip(title, 256), url=account.profile_url, colour=scan_colour(result))
    if account.avatar_url:
        embed.set_thumbnail(url=account.avatar_url)

    summary = [
        f"🕵️ **Alt score {alt.score}/100 · {alt.verdict}**\n`{bar(alt.score)}`",
        (
            f"🔞 **NSFW score {nsfw.score}/100 · FLAGGED**"
            if nsfw.flagged
            else f"🔞 NSFW score {nsfw.score}/100 · {'some matches, below threshold' if nsfw.hits else 'nothing found'}"
        ),
    ]
    if account.is_banned:
        summary.append("⛔ This account is **terminated** on Roblox.")
    if account.verified:
        summary.append("☑️ Roblox verified account.")
    if result.trusted:
        reason = f": {result.trusted_reason}" if result.trusted_reason else ""
        summary.append(f"🛡️ Marked **trusted** by {mention(result.trusted_by)}{reason}")
    if open_case:
        claim = f", claimed by {mention(open_case.claimed_by)}" if open_case.claimed_by else ""
        summary.append(f"📂 Open **case #{open_case.id}**{claim}")
    embed.description = "\n".join(summary)

    age = account.age_days()
    embed.add_field(
        name="Account",
        value=_lines(
            [
                f"ID `{account.user_id}`",
                f"Created {ts(account.created, 'D')} ({ts(account.created)})" if account.created else "Created: unknown",
                f"Age: **{age} days**" if age is not None else "",
                f"Past names: {', '.join(map(discord.utils.escape_markdown, account.username_history[:5]))}"
                if account.username_history
                else "",
            ]
        ),
        inline=True,
    )

    stats = alt.badge_stats
    if account.badges_hidden:
        badge_text = "🔒 Hidden (private inventory)"
    else:
        count = f"{stats.total}+" if account.badges_truncated else str(stats.total)
        badge_text = _lines(
            [
                f"**{count}** badges from **{stats.distinct_games}** games",
                f"First: {ts(stats.first_awarded, 'D')}" if stats.first_awarded else "",
                f"Latest: {ts(stats.last_awarded)}" if stats.last_awarded else "",
                f"Busiest 24h: {stats.busiest_day_share:.0%}" if stats.total >= 5 else "",
            ]
        )
    embed.add_field(name="Badges", value=badge_text, inline=True)

    def num(value: int | None) -> str:
        return "?" if value is None else f"{value:,}"

    embed.add_field(
        name="Social",
        value=(
            f"Friends: **{num(account.friends)}**\nFollowers: **{num(account.followers)}**\n"
            f"Following: **{num(account.following)}**\n"
            f"Groups: **{'?' if account.groups is None else len(account.groups)}**"
        ),
        inline=True,
    )
    embed.add_field(
        name="Why this alt score",
        value=_lines(f"`{s.points:+d}` {s.label}" for s in alt.signals[:8]),
        inline=False,
    )
    if nsfw.hits:
        embed.add_field(name="NSFW matches", value=_lines(nsfw_lines(nsfw)), inline=False)
    if account.description.strip():
        bio = discord.utils.escape_markdown(account.description.strip())
        if nsfw.hits:
            bio = f"||{bio}||"
        embed.add_field(name="About", value=clip(bio, 400), inline=False)
    embed.set_footer(text="Scores are hints for moderators, not proof. Always review before acting.")
    embed.timestamp = account.fetched_at
    return embed


def case_colour(case: Case) -> discord.Colour:
    if case.status == "confirmed":
        return RED
    if case.status == "dismissed":
        return GREY
    return BLUE if case.claimed_by else ORANGE


def case_embed(case: Case, notes: list[Note] | None = None) -> discord.Embed:
    details = case.details
    reasons = " · ".join(REASON_LABELS.get(r, r) for r in case.reasons) or "Case"
    embed = discord.Embed(
        title=clip(f"Case #{case.id} · {reasons} · {discord.utils.escape_markdown(case.roblox_name)}", 256),
        url=f"https://www.roblox.com/users/{case.roblox_id}/profile",
        colour=case_colour(case),
    )
    if details.get("avatar_url"):
        embed.set_thumbnail(url=details["avatar_url"])

    status = STATUS_LABELS.get(case.status, case.status)
    if case.is_open:
        status += f" · 👀 claimed by {mention(case.claimed_by)} {ts(case.claimed_at)}" if case.claimed_by else " · unclaimed"
    else:
        status += f" by {mention(case.resolved_by)} {ts(case.resolved_at)}"
    embed.description = status

    profile = f"https://www.roblox.com/users/{case.roblox_id}/profile"
    account_lines = [f"[{discord.utils.escape_markdown(case.roblox_name)}]({profile}) · `{case.roblox_id}`"]
    if details.get("account_age_days") is not None:
        account_lines.append(f"Account age: **{details['account_age_days']} days**")
    if "badge_count" in details:
        badges = details["badge_count"]
        account_lines.append(
            "Badges: 🔒 hidden" if badges is None else f"Badges: **{badges}{'+' if details.get('badges_truncated') else ''}**"
        )
    if details.get("is_banned"):
        account_lines.append("⛔ Terminated on Roblox")
    embed.add_field(name="Account", value=_lines(account_lines), inline=True)

    scores = []
    if case.alt_score is not None:
        scores.append(f"🕵️ Alt **{case.alt_score}**/100 ({details.get('alt_verdict', '?')})")
    if case.nsfw_score is not None:
        scores.append(f"🔞 NSFW **{case.nsfw_score}**/100")
    embed.add_field(name="Scores", value=_lines(scores), inline=True)
    embed.add_field(name="Opened", value=f"{mention(case.opened_by)}\n{ts(case.opened_at)}", inline=True)

    if details.get("manual_reason"):
        embed.add_field(name="Reason given", value=clip(details["manual_reason"], 1024), inline=False)
    if details.get("signals"):
        embed.add_field(
            name="Alt signals",
            value=_lines(f"`{points:+d}` {label}" for points, label in details["signals"][:6]),
            inline=False,
        )
    if details.get("nsfw_hits"):
        embed.add_field(
            name="NSFW matches",
            value=_lines(_hit_line(*hit) for hit in details["nsfw_hits"][:6]),
            inline=False,
        )
    if notes:
        embed.add_field(
            name=f"Notes ({case.note_count})",
            value=_lines(
                f"{mention(n.author_id)} {ts(n.created_at)}: {discord.utils.escape_markdown(clip(n.body, 200))}"
                for n in notes[-4:]
            ),
            inline=False,
        )
    embed.set_footer(text=f"Case #{case.id}")
    embed.timestamp = case.opened_at
    return embed


def case_list_embed(title: str, cases: list[Case], counts: dict[str, int] | None = None) -> discord.Embed:
    embed = discord.Embed(title=title, colour=BLUE)
    if counts:
        embed.description = (
            f"**{counts['open']}** open (**{counts['unclaimed']}** unclaimed) · "
            f"**{counts['confirmed']}** confirmed · **{counts['dismissed']}** dismissed"
        )
    lines = []
    for case in cases:
        reasons = " ".join(REASON_LABELS.get(r, r).split(" ")[0] for r in case.reasons)
        who = f"👀 {mention(case.claimed_by)}" if case.claimed_by and case.is_open else ""
        if not case.is_open:
            who = f"{STATUS_LABELS[case.status].split(' ')[0]} {mention(case.resolved_by)}"
        link = ""
        if case.review_channel_id and case.review_message_id:
            link = f" [↗](https://discord.com/channels/{case.guild_id}/{case.review_channel_id}/{case.review_message_id})"
        lines.append(
            f"**#{case.id}** {reasons} {discord.utils.escape_markdown(case.roblox_name)} · {ts(case.opened_at)} {who}{link}"
        )
    embed.add_field(name="Cases", value=_lines(lines) if lines else "Nothing here 🎉", inline=False)
    return embed


def badges_embed(account: AccountData, page: int, per_page: int = 15) -> discord.Embed:
    embed = discord.Embed(
        title=clip(f"Badges · {account.username}", 256), url=account.profile_url, colour=BLUE
    )
    if account.avatar_url:
        embed.set_thumbnail(url=account.avatar_url)
    if account.badges_hidden:
        embed.description = "🔒 This player's badges are private."
        return embed
    badges = account.badges
    if not badges:
        embed.description = "This player has no badges."
        return embed

    pages = max(1, -(-len(badges) // per_page))
    page = max(1, min(page, pages))
    chunk = badges[(page - 1) * per_page : page * per_page]
    lines = [
        f"[{discord.utils.escape_markdown(clip(b.name, 60))}](https://www.roblox.com/badges/{b.id})"
        f" · {ts(b.awarded_at, 'd') if b.awarded_at else 'date unknown'}"
        for b in chunk
    ]
    embed.description = _lines(lines, 4000)
    games = Counter(b.awarder_id for b in badges if b.awarder_id)
    total = f"{len(badges)}+" if account.badges_truncated else str(len(badges))
    embed.set_footer(text=f"Page {page}/{pages} · {total} badges from {len(games)} games · newest first")
    return embed


def watch_update_embed(
    entry: WatchEntry,
    result: ScanResult,
    new_badges: list[str],
    new_badge_total: int,
    notes: list[str],
) -> discord.Embed:
    account = result.account
    embed = discord.Embed(
        title=clip(f"📈 Tracking update · {account.username}", 256),
        url=account.profile_url,
        colour=scan_colour(result),
    )
    if account.avatar_url:
        embed.set_thumbnail(url=account.avatar_url)
    lines = list(notes)
    if new_badge_total:
        lines.append(f"🏅 **+{new_badge_total}** new badge(s) since last check (now {len(account.badges)})")
        lines += [f"• {discord.utils.escape_markdown(clip(name, 80))}" for name in new_badges[:8]]
        if new_badge_total > 8:
            lines.append(f"…and {new_badge_total - 8} more")
    embed.description = _lines(lines, 4000)
    embed.add_field(name="Alt score", value=f"{result.alt.score}/100 · {result.alt.verdict}", inline=True)
    embed.add_field(name="NSFW score", value=f"{result.nsfw.score}/100", inline=True)
    if entry.note:
        embed.add_field(name="Watch note", value=clip(entry.note, 1024), inline=False)
    embed.set_footer(text=f"Added by a moderator · Roblox ID {account.user_id}")
    return embed


def filter_test_embed(text: str, hits: list[Hit], score: int, threshold: int) -> discord.Embed:
    flagged = score >= threshold
    embed = discord.Embed(
        title="Filter test",
        colour=DARK_RED if flagged else (ORANGE if hits else GREEN),
        description=f"Score **{score}/100** · threshold {threshold} · {'**would be flagged**' if flagged else 'not flagged'}",
    )
    embed.add_field(name="Text", value=f"||{clip(discord.utils.escape_markdown(text), 1000)}||", inline=False)
    embed.add_field(
        name="Matches",
        value=_lines(_hit_line(h.weight, h.label, "", h.matched) for h in hits) if hits else "None",
        inline=False,
    )
    return embed


def _member_link(scan: "MemberScan") -> str:
    m = scan.member
    name = discord.utils.escape_markdown(m.username)
    shown = discord.utils.escape_markdown(m.display_name)
    label = f"{shown} (@{name})" if m.display_name and m.display_name != m.username else f"@{name}"
    return f"[{label}](https://www.roblox.com/users/{m.user_id}/profile)"


def _case_tag(scan: "MemberScan", open_cases: dict[int, int]) -> str:
    case_id = open_cases.get(scan.member.user_id)
    return f" · case #{case_id}" if case_id else ""


def _with_more(lines: list[str], total: int, shown: int) -> list[str]:
    if total > shown:
        lines.append(f"…and {total - shown} more")
    return lines


def group_scan_embed(report: "GroupScanReport", open_cases: dict[int, int] | None = None) -> discord.Embed:
    open_cases = open_cases or {}
    group = report.group
    nsfw, alts, suspicious = report.nsfw, report.alts, report.suspicious
    colour = DARK_RED if nsfw or report.group_nsfw.flagged else RED if alts else ORANGE if suspicious else GREEN
    embed = discord.Embed(title=clip(f"Group scan · {group.name}", 256), url=group.url, colour=colour)

    total = f" of **{group.member_count:,}**" if group.member_count else ""
    summary = [
        f"Checked the newest **{len(report.scans)}**{total} members.",
        f"🔞 **{len(nsfw)}** NSFW · 🕵️ **{len(alts)}** likely alts · ⚠️ **{len(suspicious)}** suspicious · "
        f"✅ **{report.clean_count}** look fine",
    ]
    if report.trusted:
        summary.append(f"🛡️ {len(report.trusted)} trusted account(s) skipped.")
    if report.failed:
        summary.append(f"❔ {len(report.failed)} couldn't be checked (Roblox didn't answer). Try again later.")
    if report.group_nsfw.hits:
        words = ", ".join(f"||{discord.utils.escape_markdown(h.matched or h.label)}||" for h in report.group_nsfw.hits[:4])
        verdict = "**This group itself looks NSFW**" if report.group_nsfw.flagged else "The group's name or description matched"
        summary.append(f"{verdict} ({words}).")
    embed.description = "\n".join(summary)

    if nsfw:
        lines = []
        for scan in nsfw[:10]:
            top = scan.result.nsfw.hits[0]
            match = f" ||{discord.utils.escape_markdown(top.matched)}||" if top.matched and top.matched != top.label else ""
            also = " · also alt-like" if scan.result.alt.score >= report.config.alt_threshold else ""
            lines.append(
                f"{_member_link(scan)} · NSFW **{scan.result.nsfw.score}** · "
                f"{discord.utils.escape_markdown(top.label)} in {top.field}{match}{also}{_case_tag(scan, open_cases)}"
            )
        embed.add_field(name="🔞 NSFW accounts", value=_lines(_with_more(lines, len(nsfw), 10)), inline=False)

    if alts:
        lines = []
        for scan in alts[:10]:
            account = scan.result.account
            age = account.age_days()
            badges = "private badges" if account.badges_hidden else f"{len(account.badges)}{'+' if account.badges_truncated else ''} badges"
            facts = ", ".join(x for x in (f"{age}d old" if age is not None else "", badges) if x)
            lines.append(f"{_member_link(scan)} · alt **{scan.result.alt.score}** · {facts}{_case_tag(scan, open_cases)}")
        embed.add_field(name="🕵️ Likely alts", value=_lines(_with_more(lines, len(alts), 10)), inline=False)

    if suspicious:
        names = [f"@{discord.utils.escape_markdown(s.member.username)} ({s.result.alt.score}/{s.result.nsfw.score})" for s in suspicious[:20]]
        embed.add_field(
            name="⚠️ Suspicious, below your thresholds (alt/NSFW)",
            value=clip(", ".join(_with_more(names, len(suspicious), 20)), 1024),
            inline=False,
        )
    if not (nsfw or alts or suspicious):
        embed.add_field(name="Result", value="Nobody here crossed your thresholds. 🎉", inline=False)
    embed.set_footer(text="Scores are hints, not proof. Use /check on anyone for the full breakdown.")
    return embed
