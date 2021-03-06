import aiohttp
import collections
import contextlib
import datetime
import discord
import functools
import inspect
import json
import os
import platform
import psutil
import re
import sys

from contextlib import redirect_stdout
from discord.ext import commands
from io import StringIO
from itertools import chain, islice, starmap
from math import log10
from more_itertools import sliced
from operator import attrgetter, methodcaller

from .utils import cache, disambiguate
from .utils.colours import url_color, user_color
from .utils.context_managers import redirect_exception, temp_message
from .utils.converter import BotCommand, union
from .utils.errors import InvalidUserArgument, ResultsNotFound
from .utils.formats import *
from .utils.misc import group_strings, str_join, nice_time, ordinal
from .utils.paginator import BaseReactionPaginator, ListPaginator, page
from .utils.subprocesses import run_subprocess

from core.cog import Cog


try:
    import pkg_resources
except ImportError:
    # TODO: Get the version AND commit number without pkg_resources
    DISCORD_PY_LIB = 'discord.py {discord.__version__}'
else:
    DISCORD_PY_LIB = str(pkg_resources.get_distribution('discord.py'))
    del pkg_resources


async def _mee6_stats(session, member):
    async with session.get(f"https://mee6.xyz/levels/{member.guild.id}?json=1&limit=-1") as r:
        levels = await r.json(content_type=None)
    for idx, user_stats in enumerate(levels['players'], start=1):
        if user_stats.get("id") == str(member.id):
            user_stats["rank"] = idx
            return user_stats
    raise ResultsNotFound(f"{member} does not have a mee6 level. :frowning:")


@cache.cache(maxsize=None)
async def _role_creator(role):
    """Returns the user who created the role.

    This is accomplished by polling the audit log, which means this can return
    None if role was created a long time ago.
    """
    # I could use a DB for this but it would be hard.

    # Integration roles don't have an audit-log entry when they're created.
    if role.managed:
        assert len(role.members) == 1, f"{role} is an integration role but somehow isn't a bot role"
        return role.members[0]

    # @everyone role, created when the server was created.
    # This doesn't account for transferring ownership but for all intents and
    # purposes this should be good enough.
    if role.is_default():
        return role.guild.owner

    delta = datetime.datetime.utcnow() - role.created_at
    # Audit log entries are deleted after 90 days, so we can guarantee that
    # there is no user to be found here.
    if delta.days >= 90:
        return None

    try:
        entry = await role.guild.audit_logs(action=discord.AuditLogAction.role_create).get(target=role)
    except discord.Forbidden:
        return "None: couldn't view the \naudit log"

    # Just in case.
    if entry is None:
        return entry
    return entry.user


_status_colors = {
    discord.Status.online    : discord.Colour.green(),
    discord.Status.idle      : discord.Colour.orange(),
    discord.Status.dnd       : discord.Colour.red(),
    discord.Status.offline   : discord.Colour.default(),
    discord.Status.invisible : discord.Colour.default(),
}


def _normal_member_status_format(_, statuses):
    return '\n'.join(starmap('{1} {0}'.format, statuses.items()))

def _status_with_emojis(self, statuses):
    c = self.context.bot.emoji_config
    return '\n'.join(f'{getattr(c, "bot_tag" if k == "Bots" else k.lower())} {v}' for k, v in statuses.items())


def default_last_n(n=50):
    return lambda: collections.deque(maxlen=n)

class ServerPages(BaseReactionPaginator):
    _formatter = _normal_member_status_format

    async def server_color(self):
        try:
            result = self._colour
        except AttributeError:
            result = 0
            url = self.guild.icon_url
            if url:
                result = self._colour = await url_color(url)
        return result

    @property
    def guild(self):
        return self.context.guild

    @page('\N{INFORMATION SOURCE}')
    async def default_(self):
        server = self.guild

        highest_role = server.role_hierarchy[0]
        description = f"Owned by {server.owner}"
        features = '\n'.join(server.features) or 'None'
        counts = (f'{len(getattr(server, thing))} {thing.title()}' 
                  for thing in ('roles', 'emojis'))
        channels = (f'{len(getattr(server, thing))} {thing.replace("_channels", " ").title()}' 
                   for thing in ('categories', 'text_channels', 'voice_channels'))

        statuses = collections.OrderedDict.fromkeys(['Online', 'Idle', 'Dnd', 'Offline'], 0)
        statuses.update(collections.Counter(m.status.name.title() for m in server.members if not m.bot))
        statuses['DND'] = statuses.pop('Dnd')
        statuses.move_to_end('Offline')
        statuses['Bots'] = sum(m.bot for m in server.members)
        member_stats = self._formatter(statuses)

        explicit_filter = str(server.explicit_content_filter).title().replace('_', ' ')

        embed = (discord.Embed(description=description, timestamp=server.created_at)
                 .set_author(name=server.name)
                 .add_field(name="Highest Role", value=highest_role)
                 .add_field(name="Region", value=str(server.region).title())
                 .add_field(name="Verification Level", value=server.verification_level.name.title())
                 .add_field(name="Explicit Content Filter", value=explicit_filter)
                 .add_field(name="Special Features", value=features)
                 .add_field(name='Counts', value='\n'.join(counts))
                 .add_field(name=pluralize(Channel=len(server.channels)), value='\n'.join(channels))
                 # Members doesn't have to be pluralized because we can guarantee that there
                 # will be at least two members in the server.
                 # - The bot can't be the only person in the server, because that would imply
                 #   that the bot owns the server, which is no longer possible.
                 # - If the bot doesn't own the server, then the owner must be there,
                 #   which means there is more than one person in the server.
                 .add_field(name=f'{len(server.members)} Members', value=member_stats)
                 .set_footer(text=f'ID: {server.id} | Created')
                 )

        icon = server.icon_url_as(format='png')
        if icon:
            embed.set_thumbnail(url=icon)
            embed.colour = await self.server_color()
        return embed

    async def default(self):
        """Shows this page (basic information about this server)"""
        embed = await self.default_()
        value = 'Confused? Click the \N{WHITE QUESTION MARK ORNAMENT} button for help.'
        return embed.add_field(name='\u200b', value=value, inline=False)

    @page('\N{CAMERA}')
    async def icon(self):
        """Shows the server's icon"""
        server = self.guild
        icon = (discord.Embed(title=f"{server}'s icon")
               .set_footer(text=f"ID: {server.id}"))

        icon_url = server.icon_url_as(format='png')
        if icon_url:
            icon.set_image(url=icon_url)
            icon.colour = await url_color(icon_url)
        else:
            icon.description = "This server has no icon :("

        return icon

    @page('\N{THINKING FACE}')
    async def emojis(self):
        """Shows the server's emojis"""
        guild = self.guild
        emojis = guild.emojis
        description = '\n'.join(group_strings(map(str, guild.emojis), 10)) if emojis else 'There are no emojis :('

        return (discord.Embed(colour=await self.server_color(), description=description)
               .set_author(name=f"{guild}'s custom emojis")
               .set_footer(text=f'{len(emojis)} emojis')
               )

    @page('\N{WHITE QUESTION MARK ORNAMENT}')
    def help_page(self):
        """Shows this page"""
        return (discord.Embed(description=self.reaction_help)
               .set_author(name='Welcome to the help thing!')
               )

def _parse_channel(channel, prefix, predicate):
    formatted = f'{prefix}{escape_markdown(str(channel))}'
    return f'**{formatted}**' if predicate(channel) else formatted

class ChannelPaginator(ListPaginator):
    def __init__(self, ctx):
        permissions_in = ctx.author.permissions_in

        _channel_parsers = {
            discord.TextChannel: functools.partial(_parse_channel, prefix='#', predicate=lambda c: permissions_in(c).read_messages),
            discord.VoiceChannel: functools.partial(_parse_channel, prefix='', predicate=lambda c: permissions_in(c).connect),
        }

        entries = [
            (category, [_channel_parsers[c.__class__](c) for c in entries])
            for category, channels in ctx.guild.by_category()
            for entries in sliced(channels, 10)
        ]

        super().__init__(ctx, entries, lines_per_page=1)

    def _create_embed(self, idx, page):
        category, channels = page[0]

        header = f'Channels in category {category}' if category else "Channels with no category..."
        category_id = category.id if category else None
        description = '\n'.join(channels) if channels else "There are no channels here..."

        return (discord.Embed(description=description, colour=self.colour)
                .set_author(name=header)
                .set_footer(text=f'Page {idx + 1}/{len(self)} | Category ID: {category_id}')
                )

DISCORD_BOTS_ID = 110373943822540800


class Meta(Cog):
    """Info related commands"""

    def __init__(self, bot):
        self.bot = bot
        self.process = psutil.Process()
        # When this cog is reloaded the on_ready won't be called again.
        # But if the bot isn't ready the guild won't be in the cache, creating
        # a false negative.
        if bot.is_ready():
            self._init_emojis()

    def has_config_emojis(self):
        attributes = ('online', 'idle', 'dnd', 'offline', 'streaming', 'bot_tag')
        return all(map(self.bot.emoji_config.__getattribute__, attributes))

    def _init_emojis(self):
        if self.has_config_emojis():
            ServerPages._formatter = _status_with_emojis

    async def on_ready(self):
        self._init_emojis()

    @commands.command()
    @commands.guild_only()
    async def uinfo(self, ctx, *, user: discord.Member=None):
        """Gets some basic userful info because why not"""
        if user is None:
            user = ctx.author
        fmt = ("    Name: {0.name}\n"
               "      ID: {0.id}\n"
               " Hashtag: {0.discriminator}\n"
               "Nickname: {0.display_name}\n"
               " Created: {0.created_at}\n"
               "  Joined: {0.joined_at}\n"
               "   Roles: {1}\n"
               "  Status: {0.status}\n"
               )
        roles = str_join(', ', reversed(user.roles[1:]))
        await ctx.send("```\n{}\n```".format(fmt.format(user, roles)))

    def _user_embed(self, member):
        avatar_url = member.avatar_url

        is_streaming = member.game and member.game.type == 1
        status = 'bot_tag' if member.bot else member.status.value
        icon = getattr(self.bot.emoji_config, status)
        if icon:
            colour = member.colour
        else:
            icon = discord.Embed.Empty
            colour = 0x593695 if is_streaming else _status_colors[member.status]

        if not member.game:
            playing = 'Not playing anything...'
        elif is_streaming:
            playing = f'Streaming [**{member.game}**]({member.game.url})'
        else:
            playing = f"Playing **{member.game}**"

        roles = sorted(member.roles, reverse=True)[:-1]  # last role is @everyone

        return  (discord.Embed(colour=colour, description=playing)
                .set_thumbnail(url=avatar_url)
                .set_author(name=str(member), icon_url=icon.url)
                .add_field(name="Display Name", value=member.display_name)
                .add_field(name="Created at", value=nice_time(member.created_at))
                .add_field(name=f"Joined server at", value=nice_time(member.joined_at))
                .add_field(name=f"Avatar link", value=f'[Click Here!](avatar_url)')
                .add_field(name=f"Roles - {len(roles)}", value=', '.join([role.mention for role in roles]) or "-no roles-", inline=False)
                .set_footer(text=f"ID: {member.id}")
                )

    @commands.command()
    async def about(self, ctx):
        """Shows some info about the bot."""
        bot = ctx.bot
        description = 'This page contains some basic but useful info.'
        useful_links = (
            f'[Click here to go to the support server!]({bot.support_invite})\n'
            f'[Click me to invite me to your server!]({bot.invite_url})\n'
            "[Check the code out here (it's fire!)](https://github.com/Ikusaba-san/Chiaki-Nanami)\n"
        )

        embed = (discord.Embed(colour=bot.colour)
                 .set_thumbnail(url=bot.user.avatar_url)
                 .set_author(name=str(bot.user))
                 .add_field(name='Creator', value=bot.creator)
                 .add_field(name='Servers', value=bot.guild_count)
                 .add_field(name='Python', value=platform.python_version())
                 .add_field(name='Library', value=DISCORD_PY_LIB)
                 .add_field(name='Useful links', value=useful_links, inline=False)
                 .set_footer(text=f'Chiaki is on Version {bot.__version__} <3')
                 )
        await ctx.send(embed=embed)

    @commands.group()
    async def info(self, ctx):
        """Super-command for all info-related commands"""
        if ctx.invoked_subcommand is None:
            if not ctx.subcommand_passed:
                return await ctx.invoke(self.about)

            subcommands = '\n'.join(sorted(map(f'`{ctx.prefix}{{0}}`'.format, ctx.command.commands)))
            description = f'Possible commands...\n\n{subcommands}'

            embed = (discord.Embed(colour=0xFF0000, description=description)
                     .set_author(name=f"{ctx.command} {ctx.subcommand_passed} isn't a command")
                     )
            await ctx.send(embed=embed)

    @info.command(name='user')
    @commands.guild_only()
    async def info_user(self, ctx, *, member: disambiguate.DisambiguateMember=None):
        """Gets some userful info because why not"""
        if member is None:
            member = ctx.author
        await ctx.send(embed=self._user_embed(member))

    @info.command(name='mee6')
    @commands.guild_only()
    async def info_mee6(self, ctx, *, member: disambiguate.DisambiguateMember=None):
        """Equivalent to `{prefix}rank`"""
        await ctx.invoke(self.rank, member=member)

    @commands.command()
    @commands.guild_only()
    async def userinfo(self, ctx, *, member: disambiguate.DisambiguateMember=None):
        """Gets some userful info because why not"""
        await ctx.invoke(self.info_user, member=member)

    @commands.command()
    @commands.guild_only()
    async def rank(self, ctx, *, member: disambiguate.DisambiguateMember=None):
        """Gets mee6 info... if it exists"""
        if member is None:
            member = ctx.author
        avatar_url = member.avatar_url

        no_mee6_in_server = "No stats found. You don't have mee6 in this server... I think."
        with redirect_exception((json.JSONDecodeError, no_mee6_in_server)):
            async with ctx.typing(), temp_message(ctx, "Fetching data, please wait...") as message:
                stats = await _mee6_stats(ctx.bot.session, member)

        description = f"Currently sitting at {stats['rank']}!"
        xp_progress = "{xp}/{lvl_xp} ({xp_percent}%)".format(**stats)
        xp_remaining = stats['lvl_xp'] - stats['xp']
        colour = await user_color(member)

        mee6_embed = (discord.Embed(colour=colour, description=description)
                     .set_author(name=member.display_name, icon_url=avatar_url)
                     .set_thumbnail(url=avatar_url)
                     .add_field(name="Level", value=stats['lvl'])
                     .add_field(name="Total XP", value=stats['total_xp'])
                     .add_field(name="Level XP",  value=xp_progress)
                     .add_field(name="XP Remaining to next level",  value=xp_remaining)
                     .set_footer(text=f"ID: {member.id}")
                     )

        await ctx.send(embed=mee6_embed)

    @info.command(name='role')
    async def info_role(self, ctx, *, role: disambiguate.DisambiguateRole):
        """Shows information about a particular role."""
        server = ctx.guild

        def bool_as_answer(b):
            return "YNeos"[not b::2]

        member_amount = len(role.members)
        if role.is_default():
            ping_notice = "And congrats on the ping. I don't have any popcorn sadly."
            members_name = "Members"
            members_value = (f"Everyone. Use `{ctx.prefix}members` to see all the members.\n"
                             f"{ping_notice * ctx.message.mention_everyone}")
        elif member_amount > 20:
            members_name = "Members"
            members_value = f"{member_amount} (use {ctx.prefix}inrole '{role}' to figure out who's in that role)"
        else:
            members_name = f"Members ({member_amount})"
            members_value = str_join(", ", role.members) or '-no one is in this role :(-'

        hex_role_color = str(role.colour).upper()
        permissions = role.permissions.value
        str_position = ordinal(role.position + 1)
        nice_created_at = nice_time(role.created_at)
        description = f"Just chilling as {server}'s {str_position} role"
        footer = f"Created at: {nice_created_at} | ID: {role.id}"
        creator = await _role_creator(role) or "None -- role is too old."

        # I think there's a way to make a solid color thumbnail, idk though
        role_embed = (discord.Embed(title=role.name, colour=role.colour, description=description)
                     .add_field(name='Created by', value=creator)
                     .add_field(name="Colour", value=hex_role_color)
                     .add_field(name="Permissions", value=permissions)
                     .add_field(name="Mentionable?", value=bool_as_answer(role.mentionable))
                     .add_field(name="Displayed separately?", value=bool_as_answer(role.hoist))
                     .add_field(name="Integration role?", value=bool_as_answer(role.managed))
                     .add_field(name=members_name, value=members_value, inline=False)
                     .set_footer(text=footer)
                     )

        await ctx.send(embed=role_embed)

    @staticmethod
    def text_channel_embed(channel):
        topic = '\n'.join(group_strings(channel.topic, 70)) if channel.topic else discord.Embed.Empty
        member_count = len(channel.members)
        empty_overwrites = sum(ow.is_empty() for _, ow in channel.overwrites)
        overwrite_message = f'{len(channel.overwrites)} ({empty_overwrites} empty)'

        return (discord.Embed(description=topic, timestamp=channel.created_at)
               .set_author(name=f'#{channel.name}')
               .add_field(name='ID', value=channel.id)
               .add_field(name='Position', value=channel.position)
               .add_field(name='Members', value=len(channel.members))
               .add_field(name='Permission Overwrites', value=overwrite_message)
               .set_footer(text='Created')
               )

    @staticmethod
    def voice_channel_embed(channel):
        empty_overwrites = sum(ow.is_empty() for _, ow in channel.overwrites)
        overwrite_message = f'{len(channel.overwrites)} ({empty_overwrites} empty)'

        return (discord.Embed(timestamp=channel.created_at)
               .set_author(name=channel.name)
               .add_field(name='ID', value=channel.id)
               .add_field(name='Position', value=channel.position)
               .add_field(name='Bitrate', value=channel.bitrate)
               .add_field(name='Max Members', value=channel.user_limit or '\N{INFINITY}')
               .add_field(name='Permission Overwrites', value=overwrite_message)
               .set_footer(text='Created')
               )

    @info.command(name='channel')
    async def info_channel(self, ctx, channel: union(discord.TextChannel, discord.VoiceChannel)=None):
        """Shows info about a voice or text channel."""
        if channel is None:
            channel = ctx.channel
        embed_type = 'text_channel_embed' if isinstance(channel, discord.TextChannel) else 'voice_channel_embed'
        channel_embed = getattr(self, embed_type)(channel)
        channel_embed.colour = self.bot.colour

        await ctx.send(embed=channel_embed)

    @info.command(name='server', aliases=['guild'])
    @commands.guild_only()
    async def info_server(self, ctx):
        """Shows info about a server"""
        await ServerPages(ctx).interact()

    @commands.command(aliases=['chnls'])
    async def channels(self, ctx):
        """Shows all the channels in the server, grouped by their category.

        Channels you can access -- being able to read messages
        for text channels, and being able to connect on your own
        for voice channels -- are **bolded.**

        Note that text channels are prefixed with `#`, while voice
        channels have no prefix.
        """
        pages = ChannelPaginator(ctx)
        await pages.interact()

    @commands.command()
    async def members(self, ctx):
        """Shows all the members of the server, sorted by their top role, then by join date"""
        # TODO: Status
        members = [str(m) for m in sorted(ctx.guild.members, key=attrgetter("top_role", "joined_at"), reverse=True)]
        pages = ListPaginator(ctx, members, title=f'Members in {ctx.guild} ({len(members)})')
        await pages.interact()

    @commands.command()
    async def roles(self, ctx, member: disambiguate.DisambiguateMember=None):
        """Shows all the roles that a member has. Roles in bold are the ones you have.

        If a member isn't provided, it defaults to all the roles in the server.
        The number to the left of the role name is the number of members who have that role.
        """
        roles = ctx.guild.role_hierarchy[:-1] if member is None else sorted(member.roles, reverse=True)[:-1]
        padding = int(log10(max(map(len, (role.members for role in roles))))) + 1

        author_roles = ctx.author.roles
        get_name = functools.partial(bold_name, predicate=lambda r: r in author_roles)
        hierarchy = [f"`{len(role.members) :<{padding}}\u200b` {get_name(role)}" for role in roles]
        pages = ListPaginator(ctx, hierarchy, title=f'Roles in {ctx.guild} ({len(hierarchy)})')
        await pages.interact()

    @commands.command()
    async def emojis(self, ctx):
        """Shows all the emojis in the server."""

        if not ctx.guild.emojis:
            return await ctx.send("This server doesn't have any custom emojis. :'(")

        emojis = map('{0} = {0.name} ({0.id})'.format, ctx.guild.emojis)
        pages = ListPaginator(ctx, emojis, title=f'Emojis in {ctx.guild}')
        await pages.interact()

    @commands.command(name='githubsource', aliases=['ghsource'])
    async def github_source(self, ctx, *, command: BotCommand = None):
        """Displays the github link for the source code of a particular command.

        Keep in mind that although this is less spammy. It may be inaccurate or
        even in a completely different place if last-minute changes were applied
        to the code and weren't pushed to GitHub.

        If you truly want the most accurate code, use `{prefix}source`.
        """
        source_url = f'https://github.com/Ikusaba-san/Chiaki-Nanami/tree/dev'
        if command is None: 
            return await ctx.send(source_url)

        src = command.callback.__code__
        lines, firstlineno = inspect.getsourcelines(src)
        lastline = firstlineno + len(lines) - 1
        # We don't use the built-in commands so we can eliminate this branch
        location = os.path.relpath(src.co_filename).replace('\\', '/')

        url = f'<{source_url}/{location}#L{firstlineno}-L{lastline}>'
        await ctx.send(url)

    @commands.command()
    @commands.cooldown(rate=2, per=5, type=commands.BucketType.user)
    async def source(self, ctx, *, command: BotCommand):
        """Displays the source code for a particular command.

        There is a per-user, 2 times per 5 seconds cooldown in order to prevent spam.
        """
        paginator = commands.Paginator(prefix='```py')
        for line in inspect.getsourcelines(command.callback)[0]:
            # inspect.getsourcelines returns the lines with the newlines at the
            # end. However, the paginator will add it's own newlines when joining
            # up the lines. We don't want to have double lines. So we have to
            # strip off the ends.
            #
            # Also, because we prefix each page with a code block (```), we need
            # to make sure that other triple-backticks don't prematurely end the
            # block.
            paginator.add_line(line.rstrip().replace('`', '\u200b`'))

        for p in paginator.pages:
            await ctx.send(p)

    # Credits to Reina
    @staticmethod
    async def _get_github_url():
        url, _ = await run_subprocess('git remote get-url origin')
        return url.strip()[:-4]  # remove .git\n

    async def _get_recent_commits(self, *, limit=None):
        url = await self._get_github_url()
        cmd = f'git log --pretty=format:"[`%h`]({url}/commit/%H) <%s> (%cr)"'
        if limit is not None:
            cmd += f' -{limit}'

        return (await run_subprocess(cmd))[0]

    @commands.command()
    async def commits(self, ctx, limit=10):
        """Shows the latest changes made to the bot.

        The default is the latest 10 changes.
        """
        changes = await self._get_recent_commits(limit=limit)

        def truncate_sub(m):
            return truncate(m[1], 47, "...")

        # By default git show doesn't truncate the commit messages.
        # %<(N,trunc) truncates them but it also pads messages that are
        # shorter than N columns, which is NOT what we want.
        #
        # One attempt was to use sed as shown here:
        # https://stackoverflow.com/a/24604658
        #
        # However since we're attempting to make this a cross-platform bot,
        # we can't use sed as it's not available in Windows and there's no
        # equivalent of it, causing it to fail. As a result, we're forced to
        # use regex.
        #
        # We know two things for sure about the commit line:
        # 1. The hash hyper link goes by the format of [`{hash}`]({commit_url})
        # 2. The relative committer time is wrapped in parentheses, i.e. ({delta})
        #
        # We use a regex solution to fish out the commit message, which
        # is wrapped in <> from the function above since we know for sure
        # neither the hash or commiter date will have <> in them.
        #
        # Not sure what the performance backlash is since it's regex,
        # but from naive timings it doesn't look like it takes too long.
        # (only 3 ms, which isn't that much compared to HTTP requests.)

        lines = (
            re.sub(r'<(.*)>', truncate_sub, change)
            for change in changes.splitlines()
        )

        pages = ListPaginator(ctx, lines, title='Latest Changes', lines_per_page=10)
        await pages.interact()

    @staticmethod
    async def _inrole(ctx, *roles, members, final='and'):
        joined_roles = human_join(map(str, roles), final=final)
        truncated_title = truncate(f'Members in {pluralize(role=len(roles))} {joined_roles}', 256, '...')

        total_color = map(sum, zip(*(role.colour.to_rgb() for role in roles)))
        average_color = discord.Colour.from_rgb(*map(round, (c / len(roles) for c in total_color)))

        if members:
            entries = sorted(map(str, members))
            # Make the author's name bold (assuming they have that role).
            # We have to do it after the list was built, otherwise the author's name
            # would be at the top.
            with contextlib.suppress(ValueError):
                index = entries.index(str(ctx.author))
                entries[index] = f'**{entries[index]}**'
        else:
            entries = ('There are no members :(', )

        pages = ListPaginator(ctx, entries, colour=average_color, title=truncated_title)
        await pages.interact()

    @commands.command()
    @commands.guild_only()
    async def inrole(self, ctx, *, role: disambiguate.DisambiguateRole):
        """Checks which members have a given role.
        If you have the role, your name will be in **bold**.

        Only one role can be specified. For multiple roles, use `{prefix}inanyrole`
        or `{prefix}inallrole`.
        """
        await self._inrole(ctx, role, members=role.members)

    @commands.command()
    @commands.guild_only()
    async def inanyrole(self, ctx, *roles: disambiguate.DisambiguateRole):
        """Checks which members have any of the given role(s).
        If you have the role, your name will be in **bold**.

        If you don't want to mention a role and there's a space in the role name,
        you must put the role in quotes
        """
        await self._inrole(ctx, *roles, members=set(chain.from_iterable(map(attrgetter('members'), roles))),
                           final='or')

    @commands.command()
    @commands.guild_only()
    async def inallrole(self, ctx, *roles: disambiguate.DisambiguateRole):
        """Checks which members have all of the given role(s).
        If you have the role, your name will be in **bold**.

        If you don't want to mention a role and there's a space in the role name,
        you must put that role in quotes
        """
        role_members = (role.members for role in roles)
        await self._inrole(ctx, *roles, members=set(next(role_members)).intersection(*role_members))

    @commands.command()
    @commands.guild_only()
    async def permroles(self, ctx, *, perm: str):
        """
        Checks which roles have a particular permission

        The permission is case insensitive.
        """
        perm_attr = perm.replace(' ', '_').lower()
        roles = filter(attrgetter(f'permissions.{perm_attr}'), ctx.guild.role_hierarchy)
        title = f"Roles in {ctx.guild} that have {perm.replace('_', ' ').title()}"

        author_roles = ctx.author.roles
        get_name = functools.partial(bold_name, predicate=lambda r: r in author_roles)
        entries = map(get_name, roles)

        pages = ListPaginator(ctx, entries, title=title)
        await pages.interact()

    @staticmethod
    async def _display_permissions(ctx, thing, permissions, extra=''):
        if isinstance(thing, discord.Member) and thing == ctx.guild.owner:
            diffs = '+ All (Server Owner)'
        elif permissions.administrator and not isinstance(thing, discord.Role):
            diffs = '+ All (Administrator Permission)'
        else:
            diffs = '\n'.join([f"{'-+'[value]} {attr.title().replace('_', ' ')}" for attr, value in permissions])
        str_perms = f'```diff\n{diffs}```'

        value = permissions.value
        perm_embed = (discord.Embed(colour=thing.colour, description=str_perms)
                     .set_author(name=f'Permissions for {thing} {extra}')
                     .set_footer(text=f'Value: {value} | Binary: {bin(value)[2:]}')
                     )
        await ctx.send(embed=perm_embed)

    @commands.command(aliases=['perms'])
    @commands.guild_only()
    async def permissions(self, ctx, *, member_or_role: disambiguate.union(discord.Member, discord.Role)=None):
        """Shows either a member's Permissions, or a role's Permissions.

        ```diff
        + Permissions you have will be shown like this.
        - Permissions you don't have will be shown like this.
        ```
        """
        if member_or_role is None:
            member_or_role = ctx.author
        permissions = getattr(member_or_role, 'permissions', None) or member_or_role.guild_permissions
        await self._display_permissions(ctx, member_or_role, permissions)

    @commands.command(aliases=['permsin'])
    @commands.guild_only()
    async def permissionsin(self, ctx, *, member: disambiguate.DisambiguateMember=None):
        """Shows a member's Permissions *in the channel*.

        ```diff
        + Permissions you have will be shown like this.
        - Permissions you don't have will be shown like this.
        ```
        """
        if member is None:
            member = ctx.author
        await self._display_permissions(ctx, member, ctx.channel.permissions_for(member), extra=f'in #{ctx.channel}')

    @commands.command(aliases=['av'])
    async def avatar(self, ctx, *, user: disambiguate.DisambiguateMember=None):
        """Shows a member's avatar. 

        If no user is specified I show your avatar.
        """

        if user is None:
            user = ctx.author
        avatar_url = user.avatar_url_as(static_format='png')
        colour = await user_color(user)
        nick = getattr(user, 'nick', None)
        description = f"*(Also known as \"{nick}\")*" * bool(nick)

        av_embed = (discord.Embed(colour=colour, description=description)
                   .set_author(name=f"{user}'s Avatar", icon_url=avatar_url, url=avatar_url)
                   #.add_field(name="Link", value=f"[Click me for avatar!]({avatar_url})")
                   .set_image(url=avatar_url)
                   .set_footer(text=f"ID: {user.id}")
                   )
        await ctx.send(embed=av_embed)

    # @commands.command(disabled=True, usage=['pow', 'os.system'], aliases=['pyh'])
    async def pyhelp(self, ctx, thing):
        """Gives you the help string for a builtin python function.
        (or any sort of function, for that matter)
        """
        # Someone told me a "lib" already does this. Is that true? If so, what lib is it?
        # TODO: Only get the docstring
        with StringIO() as output, redirect_stdout(output):
            help(thing)
            help_lines = output.getvalue().splitlines()
            await iterable_limit_say(help_lines, ctx=ctx)


def setup(bot):
    if not hasattr(bot, 'command_leaderboard'):
        bot.command_leaderboard = collections.Counter()
    bot.add_cog(Meta(bot))


